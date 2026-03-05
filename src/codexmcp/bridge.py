"""AppServerBridge：管理与 codex app-server 进程的双向 JSON-RPC 通信。

单例模式，整个 MCP Server 生命周期内维护一个 app-server 进程。
每个 thread 通过 thread/start 的 cwd 参数指定独立工作目录。

核心职责：
- 管理 app-server 长驻子进程的启动、健康检查、重连、关闭
- 维护 JSON-RPC 请求 ID 到 Future 的映射，实现请求-响应关联
- 从 stdout 读取通知，分发到对应 thread 的 EventCollector
- 提供 rpc_call()（统一入口，含重试）和 send_notification() 异步接口
- 处理服务端请求（审批等）

协议说明：
app-server 使用 JSON-RPC 2.0 lite 协议，
故意省略 "jsonrpc": "2.0" 字段（见 codex-rs jsonrpc_lite.rs）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any

from codexmcp.collector import EventCollector
from codexmcp.errors import (
    BACKPRESSURE_ERROR_CODE,
    HANDSHAKE_TIMEOUT,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    AppServerError,
    AppServerNotReady,
)
from codexmcp.process import build_app_server_cmd

logger = logging.getLogger(__name__)


class AppServerBridge:
    """管理与 codex app-server 进程的双向 JSON-RPC 通信。

    单例模式，整个 MCP Server 生命周期内维护一个 app-server 进程。
    每个 thread 通过 thread/start 的 cwd 参数指定独立工作目录。
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_collectors: dict[str, EventCollector] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._initialized: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._non_json_count: int = 0  # 非 JSON 行计数器（异常检测）

        # 启动配置（可通过 configure() 设置）
        self._config: dict[str, str] = {}
        self._profile: str = ""
        self._yolo_global: bool = False

    def configure(
        self,
        *,
        config: dict[str, str] | None = None,
        profile: str = "",
        yolo: bool = False,
    ) -> None:
        """配置 app-server 启动参数。

        Args:
            config: --config key=value 参数字典
            profile: --profile 参数
            yolo: 全局 --yolo 模式
        """
        if config is not None:
            self._config = config
        self._profile = profile
        self._yolo_global = yolo

    # === 生命周期管理 ===

    async def ensure_ready(self) -> None:
        """确保 app-server 就绪，必要时重启。

        通过 asyncio.Lock 保证并发安全，
        依次检查进程健康和初始化状态。
        """
        async with self._lock:
            if not self._health_check() or not self._initialized:
                await self._cleanup()
                await self._start_process()

    def _health_check(self) -> bool:
        """检查 app-server 进程是否存活。"""
        if self._process is None:
            return False
        if self._process.returncode is not None:
            return False  # 进程已退出
        return True

    async def _start_process(self) -> None:
        """启动 app-server 子进程并完成握手。"""
        cmd, extra_path = build_app_server_cmd(
            config=self._config if self._config else None,
            profile=self._profile,
            yolo=self._yolo_global,
        )
        logger.info(f"启动 app-server: {' '.join(cmd)}")

        # 构建子进程环境变量（仅当需要追加 PATH 时显式传入）
        env = None
        if extra_path:
            env = os.environ.copy()
            env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
            logger.info(f"子进程 PATH 已追加: {extra_path}")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **({"env": env} if env is not None else {}),
        )
        logger.info(f"app-server 进程已启动 PID={self._process.pid}")

        # 启动读取循环
        self._read_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # 完成握手（失败时清理孤儿进程和 Task）
        try:
            await self._handshake()
        except Exception:
            logger.error("app-server 握手失败，清理孤儿进程...")
            await self._cleanup()
            raise

    async def _handshake(self) -> None:
        """完成 app-server 协议握手。

        按照 v2 协议，先发送 initialize 请求获取服务端信息，
        然后发送 initialized 通知确认握手完成。

        使用独立的 HANDSHAKE_TIMEOUT（30s）而非通用 REQUEST_TIMEOUT（300s），
        让新进程无响应时快速失败，避免重连场景长时间挂起。
        """
        try:
            result = await asyncio.wait_for(
                self.rpc_call(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codexmcp",
                            "title": "CodexMCP Bridge",
                            "version": "2.0.0",
                        },
                        "capabilities": {
                            "experimentalApi": False,
                        },
                    },
                ),
                timeout=HANDSHAKE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"app-server 握手超时（{HANDSHAKE_TIMEOUT}s），进程可能无法正常启动"
            ) from None
        logger.info(f"握手完成: {result}")

        await self.send_notification("initialized", {})
        self._initialized = True

    async def shutdown(self) -> None:
        """优雅关闭 app-server。"""
        logger.info("开始关闭 app-server...")
        await self._graceful_shutdown()

    async def _graceful_shutdown(self) -> None:
        """尝试优雅关闭 app-server 进程。"""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
                logger.info("app-server 已优雅关闭")
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
                logger.warning("app-server 被强制终止")
        self._initialized = False

    async def _cleanup(self) -> None:
        """终止旧进程，清理状态。"""
        # 取消读取任务（无论 task 是否已完成都重置引用）
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        self._read_task = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

        # 终止进程
        if self._process and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()

        # 清理状态
        self._process = None
        self._initialized = False
        self._pending_requests.clear()
        self._non_json_count = 0

    # === JSON-RPC 通信 ===

    async def rpc_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """统一的 RPC 调用入口，内含重试、超时、日志。

        所有工具层的协议调用都应通过此方法，
        不直接使用 _send_request_raw。

        背压处理：当 app-server 返回 -32001 时，
        执行指数退避重试（最多 MAX_RETRIES 次）。

        Args:
            method: JSON-RPC 方法名
            params: 参数字典

        Returns:
            响应结果字典

        Raises:
            AppServerError: JSON-RPC 错误（重试耗尽后）
            TimeoutError: 请求超时
            AppServerNotReady: 进程未就绪
        """
        for attempt in range(MAX_RETRIES):
            try:
                return await self._send_request_raw(method, params)
            except AppServerError as e:
                if e.code == BACKPRESSURE_ERROR_CODE and attempt < MAX_RETRIES - 1:
                    delay = (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"背压重试 {method} (attempt {attempt + 1}), 延迟 {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        # 不应到达此处，但类型检查需要
        raise AppServerError({"code": -1, "message": "MAX_RETRIES exhausted"})

    async def _send_request_raw(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应（内部方法）。

        通过递增的 id 字段将请求与响应配对。
        注意：不发送 "jsonrpc": "2.0" 字段（JSON-RPC 2.0 lite）。

        Args:
            method: JSON-RPC 方法名
            params: 参数字典

        Returns:
            响应结果字典（result 部分）

        Raises:
            AppServerNotReady: stdin 不可用
            AppServerError: 服务端返回错误
            TimeoutError: 请求超时
        """
        if not self._process or not self._process.stdin:
            raise AppServerNotReady("app-server 进程未就绪")

        self._request_id += 1
        request_id = self._request_id

        msg: dict[str, Any] = {"method": method, "id": request_id, "params": params}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future

        # 写入 stdin (JSONL: 一行一个 JSON)
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()
        logger.debug(f"[请求] {method} (id={request_id})")

        # 等待响应（带超时）
        try:
            result = await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(
                f"请求 {method} (id={request_id}) 超时 ({REQUEST_TIMEOUT}s)"
            )
        finally:
            self._pending_requests.pop(request_id, None)

        # 检查错误响应
        if "error" in result:
            raise AppServerError(result["error"])

        return result.get("result", {})

    async def send_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        """发送通知消息（无 id，不期望响应）。

        Args:
            method: JSON-RPC 方法名
            params: 参数字典
        """
        if not self._process or not self._process.stdin:
            raise AppServerNotReady("app-server 进程未就绪")

        msg: dict[str, Any] = {"method": method, "params": params}
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()
        logger.debug(f"[通知] {method}")

    async def send_response(
        self, request_id: int, result: dict[str, Any]
    ) -> None:
        """响应服务端请求（审批等）。

        Args:
            request_id: 原始请求的 ID
            result: 响应结果字典
        """
        if not self._process or not self._process.stdin:
            raise AppServerNotReady("app-server 进程未就绪")

        msg: dict[str, Any] = {"id": request_id, "result": result}
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()
        logger.debug(f"[响应] server_request id={request_id}")

    # === 读取循环 ===

    async def _read_loop(self) -> None:
        """持续从 stdout 读取 JSON-RPC 消息并分发。

        消息分类规则：
        - 有 id + 无 method → 响应消息（匹配 pending request）
        - 有 method + 无 id → 通知消息（分发到 EventCollector）
        - 有 method + 有 id → 服务端请求（审批等）
        """
        assert self._process and self._process.stdout
        lines_read = 0
        json_msgs = 0
        start_time = time.time()
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    # 诊断：记录进程退出状态和统计信息
                    elapsed = time.time() - start_time
                    rc = self._process.returncode
                    pid = self._process.pid
                    logger.warning(
                        f"app-server stdout 已关闭 "
                        f"(pid={pid}, returncode={rc}, "
                        f"存活={elapsed:.1f}s, "
                        f"读取行数={lines_read}, JSON消息={json_msgs})"
                    )
                    break

                lines_read += 1
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                    json_msgs += 1
                    self._non_json_count = 0  # 重置计数器
                except json.JSONDecodeError:
                    # 非 JSON 行：记录告警，连续异常超阈值则报错
                    self._non_json_count += 1
                    logger.warning(f"[非JSON行] {line_str[:200]}")
                    if self._non_json_count > 10:
                        logger.error(
                            "连续出现大量非JSON输出，app-server 进程可能异常"
                        )
                    continue

                if "id" in msg and "method" not in msg:
                    # 响应消息：匹配 pending request
                    self._resolve_response(msg)
                elif "method" in msg and "id" not in msg:
                    # 通知消息：分发到事件收集器
                    self._dispatch_notification(
                        msg["method"], msg.get("params", {})
                    )
                elif "method" in msg and "id" in msg:
                    # 服务端请求（审批请求等）：交给审批处理器
                    await self._handle_server_request(msg)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"读取循环异常: {e}")
        finally:
            # 关键：进程断连时统一 fail 所有等待中的请求
            pending_count = len(self._pending_requests)
            collector_count = sum(
                1 for c in self._event_collectors.values()
                if not c.is_completed()
            )
            logger.warning(
                f"_read_loop 退出，清理中 "
                f"(pending_requests={pending_count}, "
                f"active_collectors={collector_count})"
            )
            self._fail_all_pending(
                AppServerNotReady("app-server 进程已断开")
            )
            self._initialized = False

    async def _read_stderr(self) -> None:
        """异步读取 stderr 输出并记录为 warning 日志。"""
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.warning(f"[app-server stderr] {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"stderr 读取异常: {e}")

    def _dispatch_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        """将通知分发到对应 thread 的 EventCollector。

        从 params 中提取 thread_id（支持顶层和嵌套结构），
        然后转发给对应的 EventCollector。

        Args:
            method: JSON-RPC 方法名
            params: 事件参数字典
        """
        # 从 params 中提取 thread_id
        thread_id = params.get("threadId")

        # 部分事件的 thread_id 在嵌套结构中
        if not thread_id:
            turn = params.get("turn", {})
            thread_id = turn.get("threadId")

        if not thread_id:
            logger.debug(f"无法关联通知到 thread: {method}")
            return

        collector = self._event_collectors.get(thread_id)
        if collector:
            collector.append_event(method, params)

    def _resolve_response(self, msg: dict[str, Any]) -> None:
        """匹配响应消息到对应的 pending Future。

        Args:
            msg: 包含 id 的响应消息
        """
        req_id = msg.get("id")
        if req_id is not None and req_id in self._pending_requests:
            future = self._pending_requests[req_id]
            if not future.done():
                future.set_result(msg)

    def _fail_all_pending(self, exc: Exception) -> None:
        """进程断连时，统一 fail 所有等待中的 Future。

        避免 300s 超时悬挂。同时给所有未完成的 collector
        注入断连事件并释放 barrier。

        Args:
            exc: 要设置给 Future 的异常
        """
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()

        # 同时给所有未完成的 collector 注入断连事件
        for collector in self._event_collectors.values():
            if not collector.is_completed():
                collector.append_event(
                    "bridge/disconnected",
                    {
                        "error": str(exc),
                        "timestamp": time.time(),
                    },
                )
                collector.turn_error = {"message": str(exc)}
                collector.turn_completed.set()  # 释放所有等待的 barrier

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """处理 app-server 发来的服务端请求（如审批请求）。

        审批策略：
        - yolo 模式（collector.auto_approve=True）：自动批准
        - 非 yolo 模式：暂存到 collector.pending_approvals，
          等待 codex_approve 工具处理
        - 找不到关联 collector：默认拒绝

        Args:
            msg: 包含 method、id、params 的服务端请求
        """
        method = msg["method"]
        request_id = msg["id"]
        params = msg.get("params", {})

        logger.info(f"收到服务端请求: {method} (id={request_id})")

        # 尝试找到关联的 collector
        thread_id = params.get("threadId")
        if not thread_id:
            # 审批请求可能不直接携带 threadId，尝试从当前活跃 collector 中匹配
            for tid, c in self._event_collectors.items():
                if not c.is_completed():
                    thread_id = tid
                    break

        collector = self._event_collectors.get(thread_id) if thread_id else None

        if collector and collector.auto_approve:
            # yolo 模式：自动批准
            await self.send_response(request_id, {"approved": True})
            collector.append_event("approvalRequest/auto-approved", params)
            logger.info(f"审批请求 {request_id} 已自动批准 (yolo)")
        elif collector:
            # 非 yolo 模式：暂存审批请求，等待 codex_approve 工具处理
            collector.pending_approvals[request_id] = params
            collector.append_event(
                "approvalRequest/pending",
                {
                    "request_id": request_id,
                    **params,
                },
            )
            logger.info(f"审批请求 {request_id} 已暂存，等待手动处理")
        else:
            # 找不到关联 collector：默认拒绝
            await self.send_response(
                request_id,
                {
                    "approved": False,
                    "reason": "No active thread found for approval request",
                },
            )
            logger.warning(f"审批请求 {request_id} 被拒绝：无关联 thread")

    # === Collector 管理 ===

    def get_or_create_collector(self, thread_id: str) -> EventCollector:
        """获取或创建指定 thread 的 EventCollector。

        含惰性清理逻辑：当 collector 数量超过 MAX_THREADS 时，
        优先清理已完成且超过 TTL 的 collector。

        Args:
            thread_id: thread 唯一标识符

        Returns:
            对应的 EventCollector 实例
        """
        if thread_id not in self._event_collectors:
            self._lazy_cleanup()
            self._event_collectors[thread_id] = EventCollector(thread_id)
            logger.debug(f"创建 EventCollector: {thread_id}")
        return self._event_collectors[thread_id]

    def get_collector(self, thread_id: str) -> EventCollector | None:
        """安全获取 EventCollector（不创建）。

        Args:
            thread_id: thread 唯一标识符

        Returns:
            EventCollector 实例，不存在则返回 None
        """
        collector = self._event_collectors.get(thread_id)
        if collector:
            collector.touch()
        return collector

    def rebind_collector(self, old_id: str, new_id: str) -> None:
        """将 placeholder collector 重新绑定到真实 thread_id。

        当 thread/start 返回真实 thread_id 后，
        将预创建的 placeholder collector 迁移过去。

        Args:
            old_id: placeholder ID（如 __pending_xxxx）
            new_id: 真实的 thread_id
        """
        if old_id in self._event_collectors:
            collector = self._event_collectors.pop(old_id)
            collector.thread_id = new_id
            self._event_collectors[new_id] = collector
            logger.debug(f"Collector 重绑定: {old_id} → {new_id}")

    def remove_collector(self, thread_id: str) -> None:
        """移除指定 thread 的 EventCollector。

        Args:
            thread_id: 要移除的 thread 标识符
        """
        if thread_id in self._event_collectors:
            del self._event_collectors[thread_id]
            logger.debug(f"移除 EventCollector: {thread_id}")

    async def interrupt_turn(
        self, thread_id: str, turn_id: str | None
    ) -> dict[str, Any]:
        """中断正在进行的 turn。

        Args:
            thread_id: thread 标识符
            turn_id: 要中断的 turn ID

        Returns:
            中断请求的响应结果
        """
        if not turn_id:
            return {"error": "无 turn_id，无法中断"}

        return await self.rpc_call(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )

    def _lazy_cleanup(self) -> None:
        """惰性清理过期的 EventCollector。

        当 collector 数量超过 MAX_THREADS 时，
        清理已完成且超过 CLEANUP_TTL 的 collector。
        """
        if len(self._event_collectors) < EventCollector.MAX_THREADS:
            return

        now = time.time()
        expired = [
            tid
            for tid, c in self._event_collectors.items()
            if c.is_completed()
            and (now - c.last_access_time) > EventCollector.CLEANUP_TTL
        ]
        for tid in expired:
            del self._event_collectors[tid]
            logger.debug(f"清理过期 Collector: {tid}")

        if not expired:
            logger.warning(
                f"Collector 数量达到上限 ({len(self._event_collectors)})，"
                f"但无过期项可清理"
            )


# === 模块级单例 ===

_bridge: AppServerBridge | None = None


def get_bridge() -> AppServerBridge:
    """获取 AppServerBridge 单例实例。

    懒初始化：首次调用时创建实例。

    Returns:
        全局唯一的 AppServerBridge 实例
    """
    global _bridge
    if _bridge is None:
        _bridge = AppServerBridge()
    return _bridge
