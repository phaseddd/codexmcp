"""CodexMCP 的 FastMCP 服务器实现（v2 — app-server 架构）。

通过 MCP 协议桥接 Claude Code 和 Codex App Server v2 API，
提供阻塞模式、轮询模式、中断和审批能力。

6 个 MCP 工具：
- codex         — 阻塞模式（向后兼容），等待 turn 完成后返回紧凑结果
- codex_start   — 非阻塞模式，启动后立即返回 thread_id
- codex_status  — 增量查询，按 cursor 返回 Item 级快照
- codex_result  — 获取已完成任务的最终聚合结果
- codex_interrupt — 中断正在进行的 turn
- codex_approve — 响应审批请求（批准/拒绝）
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Literal
from uuid import uuid4

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult

from codexmcp.bridge import get_bridge
from codexmcp.errors import TURN_TOTAL_TIMEOUT
from codexmcp.output import (
    build_call_tool_result,
    build_error_result,
    build_result_content,
    build_result_structured,
    build_status_content,
    build_status_structured,
)

logger = logging.getLogger(__name__)

# 初始化 FastMCP 服务器实例
mcp = FastMCP("Codex MCP Server-from phaseddd")


# === 协议格式辅助函数 ===

# sandboxPolicy 字符串到 v2 协议对象的映射
_SANDBOX_POLICY_MAP: Dict[str, Dict[str, str]] = {
    "read-only": {"type": "readOnly"},
    "workspace-write": {"type": "workspaceWrite"},
    "danger-full-access": {"type": "dangerFullAccess"},
}


def _build_status_payload(
    collector: Any,
    *,
    cursor: int = 0,
    raw_events: bool = False,
    detail: Literal["compact", "verbose"] = "compact",
) -> Dict[str, Any]:
    """统一构造状态查询结构化返回体。"""
    return build_status_structured(
        collector,
        cursor=cursor,
        raw_events=raw_events,
        detail=detail,
    )


def _render_status_result(
    collector: Any,
    *,
    cursor: int = 0,
    raw_events: bool = False,
    detail: Literal["compact", "verbose"] = "compact",
) -> CallToolResult:
    structured = _build_status_payload(
        collector,
        cursor=cursor,
        raw_events=raw_events,
        detail=detail,
    )
    return build_call_tool_result(
        build_status_content(structured),
        structured,
    )


def _render_result_result(
    collector: Any,
    *,
    detail: Literal["compact", "full", "raw"] = "compact",
    include_raw_events: bool = False,
    extra: dict[str, Any] | None = None,
) -> CallToolResult:
    structured = build_result_structured(
        collector,
        detail=detail,
        include_raw_events=include_raw_events,
    )
    if extra:
        structured.update(extra)
    return build_call_tool_result(
        build_result_content(structured),
        structured,
    )


def _build_user_input(prompt: str, images: list[Path] | None = None) -> list[Dict[str, Any]]:
    """将 prompt 字符串和可选图片列表转换为 v2 协议的 UserInput 数组。

    v2 协议要求 turn/start 的 input 字段为 UserInput 对象数组，
    每个对象需要 type 字段标识类型。

    Args:
        prompt: 用户输入的文本 prompt
        images: 可选的本地图片路径列表

    Returns:
        符合 v2 协议的 UserInput 数组
    """
    items: list[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if images:
        for img in images:
            items.append({"type": "localImage", "path": str(img)})
    return items


def _build_turn_params(
    thread_id: str,
    prompt: str,
    sandbox: str,
    *,
    images: list[Path] | None = None,
    yolo: bool = False,
) -> Dict[str, Any]:
    """构建 turn/start 的请求参数。

    将 MCP 工具的用户友好参数转换为 v2 协议的精确格式。
    模型由用户本地 Codex 配置决定，不在 per-turn 级别覆盖。

    Args:
        thread_id: 目标 thread ID
        prompt: 原始 prompt 字符串（JSON 转义由 bridge.rpc_call 内的 json.dumps 自动处理）
        sandbox: 沙箱策略字符串（read-only / workspace-write / danger-full-access）
        images: 可选的图片列表
        yolo: 是否启用自动审批

    Returns:
        符合 v2 协议的 turn/start 参数字典
    """
    params: Dict[str, Any] = {
        "threadId": thread_id,
        "input": _build_user_input(prompt, images),
        "sandboxPolicy": _SANDBOX_POLICY_MAP.get(sandbox, {"type": "readOnly"}),
    }

    # yolo 模式：设置 approvalPolicy 为 "never"，turn 级别跳过所有审批
    if yolo:
        params["approvalPolicy"] = "never"

    return params


# === MCP 工具定义 ===


@mcp.tool(
    name="codex",
    structured_output=False,  # 显式返回 CallToolResult，避免普通 dict 被自动转为 JSON 文本
    description="""
    通过 Codex App Server v2 协议执行 AI 辅助编码任务（阻塞模式）。

    发送 prompt 给 Codex，等待任务完成后返回聚合结果。
    支持会话恢复（通过 SESSION_ID）和沙箱隔离策略。

    **核心能力：**
        - **Prompt 驱动执行：** 向 Codex 发送任务指令，逐步完成编码工作。
        - **工作区隔离：** 在指定目录内操作，支持三级沙箱安全策略。
        - **会话持久化：** 通过 SESSION_ID 恢复之前的对话上下文。
        - **事件聚合：** 自动收集 agent 消息、命令执行、文件变更、推理过程等完整事件流。

    **使用建议：**
        - 确保 `cd` 路径存在且可访问。
        - 大多数场景推荐使用 "read-only" 沙箱以避免意外修改。
        - 设置 `return_all_messages=True` 可获取详细结果（生命周期事件 + 命令执行 + 文件变更 + 推理过程）。
        - 长时间任务建议使用 codex_start 非阻塞模式。
    """,
    meta={"version": "2.0.0", "author": "phaseddd"},
)
async def codex(
    PROMPT: str,
    cd: Path,
    ctx: Context,
    sandbox: Literal[
        "read-only", "workspace-write", "danger-full-access"
    ] = "read-only",
    SESSION_ID: str = "",
    yolo: bool = False,
    return_all_messages: bool = False,
    image: list[Path] = [],
) -> CallToolResult:
    """阻塞模式：发送 prompt，等待 turn 完成，返回聚合结果。

    流程：
    1. ensure_ready → 确保 app-server 就绪
    2. 预创建 placeholder collector（防止通知早于响应）
    3. thread/start 或 thread/resume
    4. rebind collector 到真实 thread_id
    5. turn/start 启动任务
    6. barrier 等待 + 总超时 30min + ctx.report_progress 预埋
    7. 检查 turn_error → 返回聚合结果
    """
    bridge = get_bridge()
    await bridge.ensure_ready()

    # 路径预校验：在调用 app-server 前快速失败
    if not cd.exists():
        return build_error_result(f"工作目录不存在: {cd}")

    # prompt 无需平台专用转义：json.dumps 会自动处理 JSON 转义

    # 快速失败：检查已断连的会话
    if SESSION_ID:
        existing = bridge.get_collector(SESSION_ID)
        if existing and existing.transport_disconnected:
            return build_error_result(
                (
                    f"会话已丢失（{existing.disconnect_reason}），"
                    "进程重启后无法恢复。请不带 SESSION_ID 启动新会话。"
                ),
                thread_id=SESSION_ID,
            )

    # 1. 预创建 collector（防止通知早于响应到达时丢失）
    placeholder_id = f"__pending_{uuid4().hex[:8]}"
    pre_collector = bridge.get_or_create_collector(placeholder_id)
    pre_collector.auto_approve = yolo  # yolo 模式下自动批准审批请求

    try:
        # 2. 创建或恢复 thread
        if SESSION_ID:
            thread_result = await bridge.rpc_call(
                "thread/resume",
                {
                    "threadId": SESSION_ID,
                    "cwd": str(cd),
                },
            )
        else:
            thread_params: Dict[str, Any] = {"cwd": str(cd)}
            thread_result = await bridge.rpc_call("thread/start", thread_params)

        # 3. 将预创建的 collector 绑定到真实 thread_id
        thread_id = thread_result["thread"]["id"]
        bridge.rebind_collector(placeholder_id, thread_id)
        collector = bridge.get_or_create_collector(thread_id)
        collector.reset_for_new_turn()
        collector.auto_approve = yolo

        # 4. 发送 turn
        turn_params = _build_turn_params(
            thread_id, PROMPT, sandbox,
            images=image if image else None,
            yolo=yolo,
        )

        turn_result = await bridge.rpc_call("turn/start", turn_params)
        collector.current_turn_id = turn_result.get("turn", {}).get("id")

        # 5. 等待 turn 完成（Barrier 模式）
        start_time = time.time()
        progress_counter = 0
        while not collector.turn_completed.is_set():
            # 检查总超时
            if time.time() - start_time > TURN_TOTAL_TIMEOUT:
                await bridge.interrupt_turn(thread_id, collector.current_turn_id)
                partial = build_result_structured(
                    collector,
                    detail="compact",
                )
                return build_error_result(
                    f"Turn 执行超时（{TURN_TOTAL_TIMEOUT // 60} 分钟）",
                    thread_id=thread_id,
                    details={
                        "status": partial["status"],
                        "transport": partial["transport"],
                        "diagnostics": partial["diagnostics"],
                        "partial_result": partial["final_result"],
                    },
                )

            try:
                await asyncio.wait_for(
                    collector.turn_completed.wait(), timeout=5.0
                )
            except asyncio.TimeoutError:
                # 预埋 progress 报告（当前静默，未来 Claude Code 支持时生效）
                progress_counter += 1
                try:
                    await ctx.report_progress(
                        progress=progress_counter,
                        total=0,
                    )
                except Exception:
                    pass  # 静默忽略 progress 报告错误

        # 6. 检查 turn 错误
        if collector.turn_error:
            partial = build_result_structured(
                collector,
                detail="compact",
            )
            return build_error_result(
                collector.turn_error.get("message", "Turn 执行失败"),
                thread_id=thread_id,
                details={
                    "status": partial["status"],
                    "transport": partial["transport"],
                    "diagnostics": partial["diagnostics"],
                    "partial_result": partial["final_result"],
                    "error_details": collector.turn_error,
                },
            )

        # 7. 聚合返回
        extra: dict[str, Any] = {}
        if collector.completion_state == "transport_lost":
            extra["message"] = "app-server 传输已断开，返回当前已聚合结果。"
        if return_all_messages:
            status_payload = _build_status_payload(
                collector,
                cursor=0,
                raw_events=False,
                detail="verbose",
            )
            extra["status_snapshot"] = status_payload
            extra["changed_items"] = status_payload["changed_items"]
            extra["lifecycle_events"] = status_payload["lifecycle_events"]
            extra["diagnostic_events"] = status_payload["diagnostic_events"]
            extra["pending_approvals"] = status_payload["pending_approvals"]
            if "new_events" in status_payload:
                extra["raw_events"] = status_payload["new_events"]

        # 事件截断标记
        if collector.truncated:
            extra["truncated"] = True

        result = _render_result_result(
            collector,
            detail="compact",
            extra=extra,
        )

        return result

    except Exception as e:
        # 清理 placeholder（如果还存在）
        bridge.remove_collector(placeholder_id)
        logger.error(f"codex 工具执行失败: {e}")
        return build_error_result(str(e))


@mcp.tool(
    name="codex_start",
    structured_output=False,  # 普通小型 dict 返回保持非结构化模式，避免自动推导输出模型
    description="""
    启动非阻塞 Codex 会话，立即返回 thread_id。

    适用于长时间运行的任务，支持增量查看进度。
    使用 codex_status(thread_id) 轮询进度，
    使用 codex_interrupt(thread_id) 中断任务，
    使用 codex_approve(thread_id, request_id) 响应审批请求。
    """,
    meta={"version": "2.0.0", "author": "phaseddd"},
)
async def codex_start(
    PROMPT: str,
    cd: Path,
    sandbox: Literal[
        "read-only", "workspace-write", "danger-full-access"
    ] = "read-only",
    SESSION_ID: str = "",
    yolo: bool = False,
    image: list[Path] = [],
) -> Dict[str, Any]:
    """非阻塞模式：启动 Codex 任务，立即返回 thread_id。"""
    bridge = get_bridge()
    await bridge.ensure_ready()

    # 路径预校验：在调用 app-server 前快速失败
    if not cd.exists():
        return {
            "success": False,
            "error": f"工作目录不存在: {cd}",
        }

    # prompt 无需平台专用转义：json.dumps 会自动处理 JSON 转义

    # 快速失败：检查已断连的会话
    if SESSION_ID:
        existing = bridge.get_collector(SESSION_ID)
        if existing and existing.transport_disconnected:
            return {
                "success": False,
                "error": (
                    f"会话已丢失（{existing.disconnect_reason}），"
                    "进程重启后无法恢复。请不带 SESSION_ID 启动新会话。"
                ),
                "SESSION_ID": SESSION_ID,
            }

    # 预创建 collector
    placeholder_id = f"__pending_{uuid4().hex[:8]}"
    pre_collector = bridge.get_or_create_collector(placeholder_id)
    pre_collector.auto_approve = yolo

    try:
        # 创建或恢复 thread
        if SESSION_ID:
            thread_result = await bridge.rpc_call(
                "thread/resume",
                {
                    "threadId": SESSION_ID,
                    "cwd": str(cd),
                },
            )
        else:
            thread_params: Dict[str, Any] = {"cwd": str(cd)}
            thread_result = await bridge.rpc_call("thread/start", thread_params)

        # 绑定到真实 thread_id
        thread_id = thread_result["thread"]["id"]
        bridge.rebind_collector(placeholder_id, thread_id)
        collector = bridge.get_or_create_collector(thread_id)
        collector.reset_for_new_turn()
        collector.auto_approve = yolo

        # 发送 turn（不等待完成）
        turn_params = _build_turn_params(
            thread_id, PROMPT, sandbox,
            images=image if image else None,
            yolo=yolo,
        )

        turn_result = await bridge.rpc_call("turn/start", turn_params)
        collector.current_turn_id = turn_result.get("turn", {}).get("id")

        return {
            "success": True,
            "thread_id": thread_id,
            "SESSION_ID": thread_id,  # 向后兼容别名
            "status": "running",
            "message": "任务已启动。使用 codex_status(thread_id) 查看进度。",
        }

    except Exception as e:
        bridge.remove_collector(placeholder_id)
        logger.error(f"codex_start 执行失败: {e}")
        return {"success": False, "error": str(e)}


@mcp.tool(
    name="codex_status",
    structured_output=False,  # 显式返回 CallToolResult，避免普通 dict 被自动转为 JSON 文本
    description="""
    查询正在运行的 Codex 任务的增量状态。

    首次调用传 cursor=0 获取全部快照。
    后续调用传入返回的 next_cursor 值，仅获取新增变化。

    默认返回 Item 级快照（changed_items / lifecycle_events / diagnostic_events）。
    如需排障，可传 raw_events=true 保留原始 new_events。
    完成态只返回紧凑 final_result 摘要；完整最终结果请使用 codex_result。
    当 pending_approvals 非空时，使用 codex_approve 响应审批请求。
    """,
    meta={"version": "2.0.0", "author": "phaseddd"},
)
async def codex_status(
    thread_id: str,
    cursor: int = 0,
    raw_events: bool = False,
    detail: Literal["compact", "verbose"] = "compact",
) -> CallToolResult:
    """查询 Codex 任务的增量状态。"""
    bridge = get_bridge()
    collector = bridge.get_collector(thread_id)

    if collector is None:
        return build_error_result(
            f"未找到 thread_id: {thread_id}",
            thread_id=thread_id,
        )

    return _render_status_result(
        collector,
        cursor=cursor,
        raw_events=raw_events,
        detail=detail,
    )


@mcp.tool(
    name="codex_interrupt",
    structured_output=False,  # 普通小型 dict 返回保持非结构化模式，避免自动推导输出模型
    description="""
    中断正在运行的 Codex 任务。

    向 app-server 发送 turn/interrupt 请求以停止当前 turn。
    """,
    meta={"version": "2.0.0", "author": "phaseddd"},
)
async def codex_interrupt(
    thread_id: str,
) -> Dict[str, Any]:
    """中断正在进行的 Codex 任务。"""
    bridge = get_bridge()
    collector = bridge.get_collector(thread_id)

    if collector is None:
        return {"success": False, "error": f"未找到 thread_id: {thread_id}"}

    if collector.is_completed():
        return {"success": True, "message": "任务已经完成，无需中断。"}

    turn_id = collector.get_current_turn_id()
    if turn_id:
        await bridge.interrupt_turn(thread_id, turn_id)

    return {
        "success": True,
        "message": "中断请求已发送。",
        "events_collected": len(collector.events),
    }


@mcp.tool(
    name="codex_result",
    structured_output=False,  # 显式返回 CallToolResult，避免普通 dict 被自动转为 JSON 文本
    description="""
    获取已完成 Codex 任务的最终聚合结果。

    `detail="compact"` 返回适合阅读的紧凑结果，
    `detail="full"` 返回完整可读结果，
    `detail="raw"` 返回不做 ANSI 清洗的原始完整结果。
    任务仍在运行时会返回错误，请先使用 codex_status 查看进度。
    """,
    meta={"version": "2.0.0", "author": "phaseddd"},
)
async def codex_result(
    thread_id: str,
    detail: Literal["compact", "full", "raw"] = "compact",
    include_raw_events: bool = False,
) -> CallToolResult:
    """获取已完成任务的最终结果。"""
    bridge = get_bridge()
    collector = bridge.get_collector(thread_id)

    if collector is None:
        return build_error_result(
            f"未找到 thread_id: {thread_id}",
            thread_id=thread_id,
        )

    if not collector.is_completed():
        return build_error_result(
            (
                "thread 尚未完成（当前状态："
                f"{collector.completion_state}），请先用 codex_status 确认完成后再调用 codex_result。"
            ),
            thread_id=thread_id,
            details={"status": collector.completion_state},
        )

    if collector.turn_error:
        partial = build_result_structured(
            collector,
            detail="compact",
            include_raw_events=include_raw_events,
        )
        return build_error_result(
            collector.turn_error.get("message", "Turn 执行失败"),
            thread_id=thread_id,
            details={
                "status": partial["status"],
                "transport": partial["transport"],
                "diagnostics": partial["diagnostics"],
                "partial_result": partial["final_result"],
                "error_details": collector.turn_error,
            },
        )

    extra: dict[str, Any] = {}
    if collector.completion_state == "transport_lost":
        extra["message"] = "app-server 传输已断开，返回当前已聚合结果。"

    return _render_result_result(
        collector,
        detail=detail,
        include_raw_events=include_raw_events,
        extra=extra,
    )


@mcp.tool(
    name="codex_approve",
    structured_output=False,  # 普通小型 dict 返回保持非结构化模式，避免自动推导输出模型
    description="""
    响应 Codex 的审批请求。

    当 codex_status 返回 pending_approvals 时，使用此工具批准或拒绝操作。
    yolo 模式下审批会自动处理，无需手动调用。
    """,
    meta={"version": "2.0.0", "author": "phaseddd"},
)
async def codex_approve(
    thread_id: str,
    request_id: int,
    approve: bool = True,
    reason: str = "",
) -> Dict[str, Any]:
    """响应 Codex 的审批请求。"""
    bridge = get_bridge()
    collector = bridge.get_collector(thread_id)

    if collector is None:
        return {"success": False, "error": f"未找到 thread_id: {thread_id}"}

    # 检查审批请求是否存在
    if request_id not in collector.pending_approvals:
        return {"success": False, "error": f"未找到审批请求: {request_id}"}

    # 发送审批响应给 app-server
    if approve:
        await bridge.send_response(request_id, {"approved": True})
        collector.append_event(
            "approvalRequest/approved",
            {
                "request_id": request_id,
                **collector.pending_approvals[request_id],
            },
        )
    else:
        await bridge.send_response(
            request_id,
            {
                "approved": False,
                "reason": reason or "Declined by user via codexmcp",
            },
        )
        collector.append_event(
            "approvalRequest/declined",
            {
                "request_id": request_id,
                "reason": reason,
                **collector.pending_approvals[request_id],
            },
        )

    # 清理已处理的审批请求
    del collector.pending_approvals[request_id]

    return {
        "success": True,
        "message": f"审批请求 {request_id} 已{'批准' if approve else '拒绝'}。",
        "remaining_approvals": len(collector.pending_approvals),
    }


# === 服务器启动 ===


def run() -> None:
    """通过 stdio 传输启动 MCP 服务器。

    包含优雅关闭逻辑：退出时自动关闭 app-server 进程。
    """
    from codexmcp.bridge import get_bridge

    bridge = get_bridge()

    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    finally:
        # 优雅关闭 app-server
        if bridge._process and bridge._process.returncode is None:
            bridge._process.terminate()
            try:
                bridge._process.wait()
            except Exception:
                bridge._process.kill()
        # 兜底退出
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
