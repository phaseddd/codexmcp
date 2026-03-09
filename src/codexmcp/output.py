"""输出投影层：把 collector 原始状态转换为 MCP 可读结果。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Literal, TypedDict

from mcp.types import CallToolResult, TextContent

ViewMode = Literal[
    "status_compact",
    "status_verbose",
    "result_compact",
    "result_full",
]

MAX_COMMAND_OUTPUT_PREVIEW = 2000
MAX_FILE_DIFF_PREVIEW = 2000
MAX_REASONING_TEXT_PREVIEW = 1500
MAX_STATUS_MESSAGE_PREVIEW = 4000

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class PreviewText(TypedDict):
    text: str
    truncated: bool
    original_len: int
    ansi_stripped: bool


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_ansi(text: str) -> str:
    """移除终端 ANSI 转义序列，避免污染展示文本。"""

    return _ANSI_RE.sub("", text)


def preview_text(
    text: str,
    *,
    limit: int,
    keep_head_tail: bool = True,
    strip_ansi_codes: bool = False,
) -> PreviewText:
    """将长文本裁成预览片段，同时保留截断元信息。"""

    normalized = _normalize_newlines(text)
    ansi_stripped = False
    if strip_ansi_codes:
        stripped = strip_ansi(normalized)
        ansi_stripped = stripped != normalized
        normalized = stripped

    original_len = len(normalized)
    if original_len <= limit:
        return {
            "text": normalized,
            "truncated": False,
            "original_len": original_len,
            "ansi_stripped": ansi_stripped,
        }

    if not keep_head_tail or limit < 80:
        truncated_text = normalized[:limit]
    else:
        head = limit // 2
        tail = limit - head
        hidden_len = original_len - limit
        truncated_text = (
            normalized[:head]
            + f"\n\n... [已截断 {hidden_len} 字符] ...\n\n"
            + normalized[-tail:]
        )

    return {
        "text": truncated_text,
        "truncated": True,
        "original_len": original_len,
        "ansi_stripped": ansi_stripped,
    }


def build_call_tool_result(
    content_blocks: list[str | TextContent],
    structured_content: dict[str, Any] | None = None,
    *,
    is_error: bool = False,
) -> CallToolResult:
    """统一构造 CallToolResult。"""

    normalized_blocks: list[TextContent] = []
    for block in content_blocks:
        if isinstance(block, TextContent):
            normalized_blocks.append(block)
        else:
            normalized_blocks.append(
                TextContent(type="text", text=_normalize_newlines(block))
            )

    return CallToolResult(
        content=normalized_blocks,
        structuredContent=structured_content,
        isError=is_error,
    )


def build_error_result(
    error: str,
    *,
    thread_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> CallToolResult:
    """构造统一的错误结果，避免错误路径退回 dict。"""

    structured: dict[str, Any] = {"success": False, "error": error}
    if thread_id:
        structured["thread_id"] = thread_id
        structured["SESSION_ID"] = thread_id
    if details:
        structured.update(details)

    content = [f"**错误**\n{error}"]
    return build_call_tool_result(content, structured, is_error=True)


def _add_preview_metadata(
    target: dict[str, Any],
    field_name: str,
    preview: PreviewText,
) -> None:
    target[field_name] = preview["text"]
    if preview["truncated"]:
        target[f"{field_name}_truncated"] = True
        target[f"{field_name}_original_len"] = preview["original_len"]
    if preview["ansi_stripped"]:
        target[f"{field_name}_ansi_stripped"] = True


def _preview_plain_field(
    target: dict[str, Any],
    field_name: str,
    *,
    limit: int,
    strip_ansi_codes: bool = False,
) -> None:
    value = target.get(field_name)
    if not isinstance(value, str):
        return
    preview = preview_text(
        value,
        limit=limit,
        strip_ansi_codes=strip_ansi_codes,
    )
    _add_preview_metadata(target, field_name, preview)


def _preview_reasoning_mapping(mapping: dict[str, Any]) -> None:
    if isinstance(mapping.get("summary"), str):
        mapping["summary"] = _normalize_newlines(mapping["summary"])
    if isinstance(mapping.get("text"), str):
        preview = preview_text(
            mapping["text"],
            limit=MAX_REASONING_TEXT_PREVIEW,
        )
        _add_preview_metadata(mapping, "text", preview)


def _project_changed_item(item: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(item)
    item_type = snapshot.get("type")

    if item_type == "commandExecution":
        _preview_plain_field(
            snapshot,
            "delta",
            limit=MAX_COMMAND_OUTPUT_PREVIEW,
            strip_ansi_codes=True,
        )
        _preview_plain_field(
            snapshot,
            "content",
            limit=MAX_COMMAND_OUTPUT_PREVIEW,
            strip_ansi_codes=True,
        )
    elif item_type == "fileChange":
        _preview_plain_field(snapshot, "delta", limit=MAX_FILE_DIFF_PREVIEW)
        _preview_plain_field(snapshot, "content", limit=MAX_FILE_DIFF_PREVIEW)
    elif item_type == "reasoning":
        delta = snapshot.get("delta")
        if isinstance(delta, dict):
            _preview_reasoning_mapping(delta)
        content = snapshot.get("content")
        if isinstance(content, dict):
            _preview_reasoning_mapping(content)
    else:
        _preview_plain_field(
            snapshot,
            "delta",
            limit=MAX_STATUS_MESSAGE_PREVIEW,
        )
        _preview_plain_field(
            snapshot,
            "content",
            limit=MAX_STATUS_MESSAGE_PREVIEW,
        )

    return snapshot


def _project_raw_event(event: dict[str, Any]) -> dict[str, Any]:
    projected = dict(event)
    params = projected.get("params")
    if not isinstance(params, dict):
        return projected

    params = dict(params)
    method = str(projected.get("method", ""))
    delta = params.get("delta")
    if isinstance(delta, str):
        if method == "item/commandExecution/outputDelta":
            preview = preview_text(
                delta,
                limit=MAX_COMMAND_OUTPUT_PREVIEW,
                strip_ansi_codes=True,
            )
        elif method == "item/fileChange/outputDelta":
            preview = preview_text(delta, limit=MAX_FILE_DIFF_PREVIEW)
        else:
            preview = preview_text(delta, limit=MAX_STATUS_MESSAGE_PREVIEW)
        params["delta"] = preview["text"]
        if preview["truncated"]:
            params["delta_truncated"] = True
            params["delta_original_len"] = preview["original_len"]
        if preview["ansi_stripped"]:
            params["delta_ansi_stripped"] = True

    projected["params"] = params
    return projected


def _project_command_execution(
    execution: dict[str, Any],
    *,
    detail: Literal["compact", "full"],
) -> dict[str, Any]:
    projected = dict(execution)
    if detail == "compact":
        _preview_plain_field(
            projected,
            "output",
            limit=MAX_COMMAND_OUTPUT_PREVIEW,
            strip_ansi_codes=True,
        )
    else:
        output = projected.get("output")
        if isinstance(output, str):
            preview = preview_text(
                output,
                limit=len(output),
                strip_ansi_codes=True,
            )
            _add_preview_metadata(projected, "output", preview)
    return projected


def _project_file_change(
    file_change: dict[str, Any],
    *,
    detail: Literal["compact", "full"],
) -> dict[str, Any]:
    projected = dict(file_change)
    if "diff_summary" in projected:
        limit = (
            MAX_FILE_DIFF_PREVIEW
            if detail == "compact"
            else max(len(str(projected["diff_summary"])), 1)
        )
        _preview_plain_field(projected, "diff_summary", limit=limit)
    return projected


def _project_reasoning_segment(
    reasoning: dict[str, Any],
    *,
    detail: Literal["compact", "full"],
) -> dict[str, Any]:
    projected = dict(reasoning)
    if detail == "compact" and isinstance(projected.get("text"), str):
        preview = preview_text(
            projected["text"],
            limit=MAX_REASONING_TEXT_PREVIEW,
        )
        _add_preview_metadata(projected, "text", preview)
    elif detail == "full" and isinstance(projected.get("text"), str):
        projected["text"] = _normalize_newlines(projected["text"])
    if isinstance(projected.get("summary"), str):
        projected["summary"] = _normalize_newlines(projected["summary"])
    return projected


def _build_transport_payload(collector: Any) -> dict[str, Any]:
    return {
        "connected": not collector.transport_disconnected,
        "disconnected": collector.transport_disconnected,
        "process_exit_code": collector.process_exit_code,
        "disconnect_reason": collector.disconnect_reason,
    }


def _build_diagnostics_payload(collector: Any) -> dict[str, Any]:
    return {
        "last_event_method": collector.last_event_method,
        "event_count": collector._next_event_index,
        "active_items": collector._build_active_items(),
    }


def _build_result_counts(final_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_count": len(final_result["agent_message_items"]),
        "command_count": len(final_result["command_executions"]),
        "file_change_count": len(final_result["file_changes"]),
        "reasoning_count": len(final_result["reasoning_segments"]),
        "token_usage": final_result["token_usage"],
        "event_count": final_result["event_count"],
    }


def build_status_structured(
    collector: Any,
    *,
    cursor: int = 0,
    raw_events: bool = False,
    detail: Literal["compact", "verbose"] = "compact",
) -> dict[str, Any]:
    """构造 codex_status 的结构化返回体。"""

    incremental = collector.read_incremental(
        since_index=cursor,
        raw_events=raw_events,
        view=detail,
    )
    structured: dict[str, Any] = {
        "success": True,
        "thread_id": collector.thread_id,
        "cursor": cursor,
        **incremental,
    }
    structured["changed_items"] = [
        _project_changed_item(item)
        for item in structured["changed_items"]
    ]
    if raw_events and "new_events" in structured:
        structured["new_events"] = [
            _project_raw_event(event)
            for event in structured["new_events"]
        ]

    if structured["completed"]:
        structured["final_result"] = _build_result_counts(
            collector.get_aggregated_result()
        )

    return structured


def build_status_content(structured: dict[str, Any]) -> list[str]:
    """构造 codex_status 的人类可读 Markdown 摘要。"""

    changed_items = structured.get("changed_items", [])
    type_counter = Counter(
        str(item.get("type", "unknown")) for item in changed_items
    )
    pending_approvals = structured.get("pending_approvals", [])

    lines = [
        "**状态**",
        str(structured.get("status", "unknown")),
        "",
        "**线程**",
        str(structured.get("thread_id", "")),
        "",
        "**变化概览**",
    ]

    if type_counter:
        for item_type, count in sorted(type_counter.items()):
            lines.append(f"- {item_type}: {count}")
    else:
        lines.append("- 本次轮询无新增变化")

    if pending_approvals:
        lines.extend(
            [
                "",
                "**待审批**",
                f"- {len(pending_approvals)} 个请求待处理",
            ]
        )

    if structured.get("completed"):
        lines.extend(
            [
                "",
                "**下一步**",
                f'使用 `codex_result(thread_id="{structured["thread_id"]}")` 查看最终结果。',
            ]
        )

    return ["\n".join(lines)]


def build_result_structured(
    collector: Any,
    *,
    detail: Literal["compact", "full"] = "compact",
    include_raw_events: bool = False,
) -> dict[str, Any]:
    """构造 codex_result / codex 的结构化结果。"""

    aggregated = collector.get_aggregated_result()
    final_result = {
        "agent_messages_text": aggregated["agent_messages_text"],
        "agent_message_items": aggregated["agent_message_items"],
        "command_executions": [
            _project_command_execution(item, detail=detail)
            for item in aggregated["command_executions"]
        ],
        "file_changes": [
            _project_file_change(item, detail=detail)
            for item in aggregated["file_changes"]
        ],
        "reasoning_segments": [
            _project_reasoning_segment(item, detail=detail)
            for item in aggregated["reasoning_segments"]
        ],
        "token_usage": aggregated["token_usage"],
        "event_count": aggregated["event_count"],
        "message_count": len(aggregated["agent_message_items"]),
        "command_count": len(aggregated["command_executions"]),
        "file_change_count": len(aggregated["file_changes"]),
        "reasoning_count": len(aggregated["reasoning_segments"]),
    }

    structured: dict[str, Any] = {
        "success": True,
        "thread_id": collector.thread_id,
        "SESSION_ID": collector.thread_id,
        "completed": collector.is_completed(),
        "has_error": collector.turn_error is not None,
        "status": collector.completion_state,
        "transport": _build_transport_payload(collector),
        "diagnostics": _build_diagnostics_payload(collector),
        "final_result": final_result,
    }
    if include_raw_events:
        raw_status = collector.read_incremental(
            since_index=0,
            raw_events=True,
            view="verbose",
        )
        structured["raw_events"] = raw_status.get("new_events", [])
    return structured


def build_result_content(structured: dict[str, Any]) -> list[str]:
    """构造 codex / codex_result 的人类可读 Markdown 内容。"""

    final_result = structured["final_result"]
    agent_text = final_result.get("agent_messages_text") or "_没有可展示的代理消息输出_"
    summary_lines = [
        "**执行摘要**",
        f'- 状态：{structured.get("status", "unknown")}',
        f'- 消息：{final_result["message_count"]}',
        f'- 命令：{final_result["command_count"]}',
        f'- 文件变更：{final_result["file_change_count"]}',
        f'- 推理片段：{final_result["reasoning_count"]}',
    ]
    if structured["transport"]["disconnected"]:
        summary_lines.append(
            f'- 传输状态：已断开（{structured["transport"]["disconnect_reason"]}）'
        )
    return [agent_text, "\n".join(summary_lines)]
