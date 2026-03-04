"""事件收集器：按 thread 收集事件流，支持增量读取和聚合结果。

核心类 EventCollector 负责：
- 按 thread_id 收集所有 Item 生命周期事件
- 跟踪 Item 状态机：started → streaming → completed
- 支持游标式增量读取（轮询模式核心）
- 检测 turn 完成状态并通过 asyncio.Event 释放 barrier
- 生成人类可读事件摘要
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from codexmcp.models import CollectedEvent, ItemState


class EventCollector:
    """为单个 thread 收集和管理事件流。

    每个 thread 对应一个 EventCollector 实例，
    所有事件按接收顺序存入有序列表，
    通过游标机制实现增量读取。

    Attributes:
        thread_id: 关联的 thread 唯一标识符
        events: 按时间顺序排列的事件列表
        items: Item ID 到状态的映射字典
        turn_completed: asyncio.Event barrier，turn 完成时释放
        turn_result: turn/completed 的参数（含 tokenUsage 等）
        turn_error: turn/error 的错误信息
        current_turn_id: 当前 turn 的 ID（用于中断操作）
        truncated: 事件是否因超限被截断
        last_access_time: 最后访问时间（TTL 清理用）
        pending_approvals: 待处理的审批请求（request_id → params）
        auto_approve: yolo 模式标志
    """

    # 资源治理安全阀
    MAX_EVENTS_PER_THREAD = 10000  # 单个 thread 最多存储事件数
    MAX_THREADS = 50  # 全局最多同时跟踪 thread 数（在 bridge 中检查）
    CLEANUP_TTL = 1800  # 30 分钟未访问的已完成 collector 自动清理

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.events: list[CollectedEvent] = []
        self.items: dict[str, ItemState] = {}
        self.turn_completed: asyncio.Event = asyncio.Event()
        self.turn_result: dict[str, Any] | None = None
        self.turn_error: dict[str, Any] | None = None
        self.current_turn_id: str | None = None
        self.truncated: bool = False
        self.last_access_time: float = time.time()
        self.pending_approvals: dict[int, dict[str, Any]] = {}
        self.auto_approve: bool = False
        self._token_usage: dict[str, Any] = {}  # thread/tokenUsage/updated 事件缓存

    def append_event(self, method: str, params: dict[str, Any]) -> None:
        """追加事件，更新 Item 状态机。

        按照 Item 生命周期处理事件：
        item/started → item/*/delta (N次) → item/completed

        同时检测 turn 级别事件（started/completed/error）
        并在完成或出错时释放 barrier。

        安全阀：事件数超限时截断最早的一半。

        Args:
            method: JSON-RPC 方法名（如 "item/agentMessage/delta"）
            params: 事件参数字典
        """
        # 0. 内存安全阀：事件数超限时截断最早的一半
        if len(self.events) >= self.MAX_EVENTS_PER_THREAD:
            half = len(self.events) // 2
            self.events = self.events[half:]
            self.truncated = True

        # 1. 创建事件记录
        event = CollectedEvent(
            index=len(self.events),
            timestamp=time.time(),
            method=method,
            params=params,
            summary=self._make_summary(method, params),
        )
        self.events.append(event)

        # 2. 更新 Item 状态机
        if method == "item/started":
            item = params.get("item", {})
            item_id = item.get("id", "")
            if item_id:
                self.items[item_id] = ItemState(
                    item_id=item_id,
                    item_type=item.get("type", "unknown"),
                    status="started",
                    content_buffer="",
                    metadata=item,
                )

        elif "/delta" in method:
            item_id = params.get("itemId", "")
            if item_id in self.items:
                self.items[item_id].status = "streaming"
                # 区分 reasoning 的 summaryDelta 和 textDelta
                if method == "item/reasoning/summaryDelta":
                    self.items[item_id].reasoning_summary += params.get("delta", "")
                elif method == "item/reasoning/textDelta":
                    self.items[item_id].reasoning_text += params.get("delta", "")
                else:
                    self.items[item_id].content_buffer += params.get("delta", "")

        elif method == "item/completed":
            item = params.get("item", {})
            item_id = item.get("id", "")
            if item_id in self.items:
                self.items[item_id].status = "completed"

        # 3. 捕获 token 使用统计（v2 协议流式通知，可能多次更新）
        elif method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage", {})
            if token_usage:
                self._token_usage = token_usage

        # 4. 检测 turn 完成
        elif method == "turn/completed":
            self.turn_result = params
            self.turn_completed.set()  # 释放 barrier

        # 5. 检测 turn 错误（也必须释放 barrier，否则阻塞模式永远等待）
        elif method == "turn/error":
            self.turn_error = params
            self.turn_completed.set()  # 释放 barrier（即使是错误也要释放）

        # 6. 记录 turn 开始（保存 turn_id）
        elif method == "turn/started":
            turn = params.get("turn", {})
            self.current_turn_id = turn.get("id")

    def read_incremental(self, since_index: int = 0) -> dict[str, Any]:
        """从指定索引开始读取增量事件。

        这是轮询模式的核心接口：调用方记住上次返回的 next_cursor，
        下次请求时传入，只获取新增部分。

        Args:
            since_index: 起始索引（含），首次调用传 0 获取全部事件

        Returns:
            包含增量事件和状态信息的字典
        """
        self.touch()

        # 边界校验：防止负数触发 Python 负索引语义
        since_index = max(0, min(since_index, len(self.events)))

        new_events = self.events[since_index:]
        return {
            "thread_id": self.thread_id,
            "completed": self.turn_completed.is_set(),
            "has_error": self.turn_error is not None,
            "truncated": self.truncated,
            "next_cursor": len(self.events),
            "new_events": [
                {
                    "index": e.index,
                    "method": e.method,
                    "summary": e.summary,
                    "params": e.params,
                }
                for e in new_events
            ],
            "active_items": {
                k: {"type": v.item_type, "status": v.status}
                for k, v in self.items.items()
                if v.status != "completed"
            },
            "pending_approvals": [
                {"request_id": rid, "params": p}
                for rid, p in self.pending_approvals.items()
            ],
        }

    def get_aggregated_result(self) -> dict[str, Any]:
        """聚合所有事件为最终结果（阻塞模式用）。

        遍历所有 Item，按类型提取内容：
        - agentMessage → agent_messages 文本
        - commandExecution → 命令/输出/退出码
        - fileChange → 路径/diff
        - reasoning → 推理摘要

        Returns:
            包含聚合结果的字典
        """
        agent_messages = ""
        command_executions: list[dict[str, Any]] = []
        file_changes: list[dict[str, Any]] = []
        reasoning_segments: list[dict[str, Any]] = []

        for item in self.items.values():
            if item.item_type == "agentMessage":
                agent_messages += item.content_buffer
            elif item.item_type == "commandExecution":
                command_executions.append(
                    {
                        "command": item.metadata.get("command", []),
                        "output": item.content_buffer,
                        "exit_code": item.metadata.get("exitCode"),
                    }
                )
            elif item.item_type == "fileChange":
                file_changes.append(
                    {
                        "path": item.metadata.get("path", ""),
                        "diff": item.content_buffer,
                    }
                )
            elif item.item_type == "reasoning":
                reasoning_segments.append(
                    {
                        "summary": item.reasoning_summary,
                    }
                )

        # 提取 token_usage（优先级：流式通知 > turn/completed 顶层 > turn 嵌套）
        token_usage: dict[str, Any] = {}
        if self._token_usage:
            # 优先使用 thread/tokenUsage/updated 流式事件（最可靠、字段最全）
            token_usage = self._token_usage
        elif self.turn_result:
            # 回退到 turn/completed 的 tokenUsage（顶层或嵌套在 turn 对象内）
            token_usage = (
                self.turn_result.get("tokenUsage", {})
                or self.turn_result.get("turn", {}).get("tokenUsage", {})
                or self.turn_result.get("usage", {})
            )

        return {
            "agent_messages": agent_messages,
            "command_executions": command_executions,
            "file_changes": file_changes,
            "reasoning_segments": reasoning_segments,
            "token_usage": token_usage,
            "event_count": len(self.events),
        }

    def reset_for_new_turn(self) -> None:
        """重置 turn 相关状态，准备接收新 turn 的事件。

        创建新的 asyncio.Event 实例（而非 .clear()），
        确保不会影响其他协程对旧 Event 的引用。
        """
        self.turn_completed = asyncio.Event()
        self.turn_result = None
        self.turn_error = None
        self.current_turn_id = None
        self.pending_approvals.clear()
        self._token_usage = {}

    def is_completed(self) -> bool:
        """检查 turn 是否已完成（包括完成和出错两种情况）。"""
        return self.turn_completed.is_set()

    def get_current_turn_id(self) -> str | None:
        """获取当前 turn 的 ID（用于中断操作）。"""
        return self.current_turn_id

    def touch(self) -> None:
        """更新最后访问时间（TTL 清理用）。"""
        self.last_access_time = time.time()

    def _make_summary(self, method: str, params: dict[str, Any]) -> str:
        """为事件生成简洁的人类可读摘要。

        Args:
            method: JSON-RPC 方法名
            params: 事件参数字典

        Returns:
            人类可读的摘要字符串
        """
        match method:
            case "turn/started":
                return "[Turn 开始]"
            case "turn/completed":
                return "[Turn 完成]"
            case "turn/error":
                msg = params.get("message", "未知错误")
                return f"[Turn 错误] {msg[:80]}"
            case "thread/tokenUsage/updated":
                total = params.get("tokenUsage", {}).get("total", {})
                inp = total.get("inputTokens", "?")
                out = total.get("outputTokens", "?")
                return f"[Token 统计] input={inp} output={out}"
            case "item/started":
                item = params.get("item", {})
                t = item.get("type", "unknown")
                if t == "commandExecution":
                    cmd = item.get("command", [])
                    return f"[执行命令] {' '.join(cmd)}"
                elif t == "agentMessage":
                    return "[代理消息开始]"
                elif t == "fileChange":
                    return f"[文件变更] {item.get('path', '')}"
                elif t == "reasoning":
                    return "[推理开始]"
                return f"[Item 开始] {t}"
            case "item/agentMessage/delta":
                delta = params.get("delta", "")
                return f"[消息] {delta[:80]}..." if len(delta) > 80 else f"[消息] {delta}"
            case "item/commandExecution/outputDelta":
                delta = params.get("delta", "")
                return f"[命令输出] {delta[:80]}..."
            case "item/fileChange/outputDelta":
                return "[文件变更 diff]"
            case "item/reasoning/summaryDelta":
                delta = params.get("delta", "")
                return f"[推理] {delta[:80]}..." if len(delta) > 80 else f"[推理] {delta}"
            case "item/reasoning/textDelta":
                delta = params.get("delta", "")
                return f"[推理文本] {delta[:80]}..." if len(delta) > 80 else f"[推理文本] {delta}"
            case "item/completed":
                return "[Item 完成]"
            case "bridge/disconnected":
                return "[Bridge 断连]"
            case _:
                # 审批事件或未知事件
                if "approvalRequest" in method:
                    return f"[审批] {method.split('/')[-1]}"
                return f"[{method}]"
