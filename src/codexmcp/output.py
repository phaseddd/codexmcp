"""输出投影层：把 collector 原始状态转换为 MCP 可读结果。"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from typing import Any, Literal, TypedDict

from mcp.types import CallToolResult, TextContent

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
    """统一构造 CallToolResult。

    不使用 structuredContent 字段：Claude Code 在渲染时优先展示
    structuredContent 的 JSON 序列化结果，导致 content 中的 Markdown
    文本被忽略、换行符被 JSON 转义为字面量 \\n。
    结构化数据经瘦身后以紧凑 JSON 文本块追加到 content 末尾。
    """

    normalized_blocks: list[TextContent] = []
    for block in content_blocks:
        if isinstance(block, TextContent):
            normalized_blocks.append(block)
        else:
            normalized_blocks.append(
                TextContent(type="text", text=_normalize_newlines(block))
            )

    # 结构化数据经瘦身后作为紧凑 JSON 文本块追加
    if structured_content is not None:
        # 记录器暂存：在 slim 前保存原始结构化数据
        from codexmcp.recorder import stash_pre_slim

        stash_pre_slim(structured_content)
        slimmed = _slim_for_json_block(structured_content)
        normalized_blocks.append(
            TextContent(
                type="text",
                text=json.dumps(slimmed, ensure_ascii=False),
            )
        )

    return CallToolResult(
        content=normalized_blocks,
        structuredContent=None,
        isError=is_error,
    )


def _build_result_counts_payload(final_result: dict[str, Any]) -> dict[str, int]:
    """提取最终结果计数，兼容明细列表和已聚合计数字段。"""

    def _resolve_count(count_key: str, items_key: str) -> int:
        count = final_result.get(count_key)
        if isinstance(count, int):
            return count
        items = final_result.get(items_key)
        if isinstance(items, list):
            return len(items)
        return 0

    return {
        "message_count": _resolve_count(
            "message_count", "agent_message_items"
        ),
        "command_count": _resolve_count(
            "command_count", "command_executions"
        ),
        "file_change_count": _resolve_count(
            "file_change_count", "file_changes"
        ),
        "reasoning_count": _resolve_count(
            "reasoning_count", "reasoning_segments"
        ),
    }


def _slim_changed_item_for_json(item: dict[str, Any]) -> dict[str, Any]:
    """移除 changed_items 中仅用于 Markdown 展示的大文本字段。"""

    slimmed = copy.deepcopy(item)
    for field_name in (
        "delta",
        "delta_truncated",
        "delta_original_len",
        "delta_ansi_stripped",
        "content",
        "content_truncated",
        "content_original_len",
        "content_ansi_stripped",
    ):
        slimmed.pop(field_name, None)
    return slimmed


def _slim_raw_event_for_json(event: dict[str, Any]) -> dict[str, Any]:
    """移除 raw/new events 中会破坏换行渲染的大字段。"""

    slimmed = copy.deepcopy(event)
    params = slimmed.get("params")
    if isinstance(params, dict):
        for field_name in (
            "delta",
            "delta_truncated",
            "delta_original_len",
            "delta_ansi_stripped",
            "content",
            "content_truncated",
            "content_original_len",
            "content_ansi_stripped",
        ):
            params.pop(field_name, None)
    return slimmed


def _slim_result_items(final_result: dict[str, Any]) -> dict[str, Any]:
    """保留 final_result 明细结构但剥离已迁往 Markdown 的大文本字段。

    command、exit_code、path、kind 等机读元数据保留，
    output、diff、text 等多行文本由 Markdown TextContent 块承载。
    """

    slimmed = copy.deepcopy(final_result)

    # agent_messages_text 已在 Markdown 第一块展示
    slimmed.pop("agent_messages_text", None)

    # agent_message_items：剥离文本内容，保留 id 等元数据
    for msg in slimmed.get("agent_message_items", []):
        if isinstance(msg, dict):
            for key in ("content", "text"):
                msg.pop(key, None)

    # command_executions：保留 command/exit_code/duration_ms，剥离 output
    for cmd in slimmed.get("command_executions", []):
        if isinstance(cmd, dict):
            for key in (
                "output",
                "output_truncated",
                "output_original_len",
                "output_ansi_stripped",
            ):
                cmd.pop(key, None)

    # file_changes：保留 path/kind 结构，剥离 diff 文本
    for fc in slimmed.get("file_changes", []):
        if isinstance(fc, dict):
            for key in (
                "diff_summary",
                "diff_summary_truncated",
                "diff_summary_original_len",
            ):
                fc.pop(key, None)
            changes = fc.get("changes")
            if isinstance(changes, list):
                for change in changes:
                    if isinstance(change, dict):
                        change.pop("diff", None)

    # reasoning_segments：保留 summary，剥离 text
    for rs in slimmed.get("reasoning_segments", []):
        if isinstance(rs, dict):
            for key in ("text", "text_truncated", "text_original_len"):
                rs.pop(key, None)

    return slimmed


def _slim_for_json_block(data: dict[str, Any]) -> dict[str, Any]:
    """统一剥离结构化数据中与 Markdown 块重复的大文本字段。

    保留列表结构及机读元数据（command、exit_code、path、kind 等），
    仅剥离 output、diff、text 等多行文本字段。
    json.dumps 会把换行符转义为字面量 \\n，这些内容已由
    Markdown TextContent 块承载，无需在 JSON 中重复。
    """

    slimmed = copy.deepcopy(data)

    # 处理 final_result（codex / codex_result 的最终结果）
    final_result = slimmed.get("final_result")
    if isinstance(final_result, dict):
        slimmed["final_result"] = _slim_result_items(final_result)

    # 处理 partial_result（超时/错误时的部分结果）
    partial_result = slimmed.get("partial_result")
    if isinstance(partial_result, dict):
        slimmed["partial_result"] = _slim_result_items(partial_result)

    # 处理 changed_items（codex_status 的增量变化）
    changed_items = slimmed.get("changed_items")
    if isinstance(changed_items, list):
        slimmed["changed_items"] = [
            _slim_changed_item_for_json(item)
            if isinstance(item, dict)
            else item
            for item in changed_items
        ]

    # 处理 raw_events / new_events
    for field_name in ("new_events", "raw_events"):
        events = slimmed.get(field_name)
        if isinstance(events, list):
            slimmed[field_name] = [
                _slim_raw_event_for_json(event)
                if isinstance(event, dict)
                else event
                for event in events
            ]

    # 递归处理嵌套 status_snapshot
    nested = slimmed.get("status_snapshot")
    if isinstance(nested, dict):
        slimmed["status_snapshot"] = _slim_for_json_block(nested)

    return slimmed


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
) -> dict[str, Any]:
    """投影命令执行条目：ANSI 清洗 + 截断预览。"""

    projected = dict(execution)
    _preview_plain_field(
        projected,
        "output",
        limit=MAX_COMMAND_OUTPUT_PREVIEW,
        strip_ansi_codes=True,
    )
    return projected


def _project_file_change(
    file_change: dict[str, Any],
) -> dict[str, Any]:
    """投影文件变更条目：截断预览。"""

    projected = dict(file_change)
    if "diff_summary" in projected:
        _preview_plain_field(
            projected,
            "diff_summary",
            limit=MAX_FILE_DIFF_PREVIEW,
        )
    return projected


def _project_reasoning_segment(
    reasoning: dict[str, Any],
) -> dict[str, Any]:
    """投影推理片段：截断预览 + 换行规范化。"""

    projected = dict(reasoning)
    if isinstance(projected.get("text"), str):
        preview = preview_text(
            projected["text"],
            limit=MAX_REASONING_TEXT_PREVIEW,
        )
        _add_preview_metadata(projected, "text", preview)
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
        aggregated = collector.get_aggregated_result()
        structured["final_result"] = {
            **_build_result_counts_payload(aggregated),
            "token_usage": aggregated["token_usage"],
            "event_count": aggregated["event_count"],
        }

    return structured


def _non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalize_newlines(value)
    if not normalized.strip():
        return None
    return normalized


def _format_inline_code(text: str) -> str:
    fence_length = max(
        1,
        max(
            (len(match.group(0)) for match in re.finditer(r"`+", text)),
            default=0,
        )
        + 1,
    )
    fence = "`" * fence_length
    if text.startswith("`") or text.endswith("`"):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def _wrap_fenced_block(text: str, language: str = "") -> str:
    normalized = _normalize_newlines(text)
    fence_length = max(
        3,
        max(
            (len(match.group(0)) for match in re.finditer(r"`+", normalized)),
            default=0,
        )
        + 1,
    )
    fence = "`" * fence_length
    opener = f"{fence}{language}" if language else fence
    return f"{opener}\n{normalized}\n{fence}"


def _wrap_quote_block(text: str) -> str:
    normalized = _normalize_newlines(text)
    return "\n".join(
        f"> {line}" if line else ">"
        for line in normalized.split("\n")
    )


def _format_command_text(command: Any) -> str:
    if isinstance(command, list):
        parts = [str(part) for part in command if part is not None]
        return " ".join(parts) or "(空命令)"
    if isinstance(command, str) and command:
        return command
    return "(未知命令)"


def _build_command_metadata(
    item: dict[str, Any],
    *,
    include_status: bool = True,
) -> str:
    parts = [
        (
            f'exit: {item["exit_code"]}'
            if item.get("exit_code") is not None
            else "exit: ?"
        )
    ]

    duration_ms = item.get("duration_ms")
    if duration_ms is not None:
        parts.append(f"{duration_ms}ms")

    status = item.get("status")
    if include_status and status and status != "completed":
        parts.append(f"status: {status}")

    return ", ".join(parts)


def _is_failed_command(item: dict[str, Any]) -> bool:
    exit_code = item.get("exit_code")
    status = str(item.get("status", ""))
    return exit_code not in (None, 0) or status == "failed"


def _select_changed_item_text(item: dict[str, Any]) -> str | None:
    content = _non_empty_text(item.get("content"))
    if content is not None:
        return content
    return _non_empty_text(item.get("delta"))


def _select_reasoning_payload(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    for field_name in ("content", "delta"):
        payload = item.get(field_name)
        if isinstance(payload, dict) and payload:
            return payload
    return None


def _format_file_change_kind(kind: Any) -> str | None:
    candidate: Any = None
    if isinstance(kind, dict):
        candidate = kind.get("type") or kind.get("kind")
    elif kind is not None:
        candidate = kind

    if candidate is None:
        return None

    text = str(candidate).strip()
    return text or None


def _fallback_file_change_kind(file_change: dict[str, Any]) -> str:
    changes = file_change.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            kind = _format_file_change_kind(change.get("kind"))
            if kind:
                return kind

    if _non_empty_text(file_change.get("diff_summary")) is not None:
        return "update"

    return "unknown"


def _render_file_change_entry(
    path: str,
    kind: str,
    diff_text: str | None,
) -> str:
    header = f"{_format_inline_code(path)} ({kind})"
    if diff_text is not None:
        return "\n".join([header, _wrap_fenced_block(diff_text, "diff")])

    note = _build_file_change_empty_note(kind)
    return "\n".join([header, note])


def _build_file_change_empty_note(kind: str) -> str:
    lowered_kind = kind.lower()
    if lowered_kind in {"delete", "deleted", "remove", "removed"}:
        return "_文件已删除_"
    if lowered_kind in {"create", "created", "add", "added"}:
        return "_文件已创建，未提供 diff 内容_"
    return "_未提供 diff 内容_"


def _build_file_change_entries(
    file_changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for index, file_change in enumerate(file_changes, start=1):
        base_path = str(
            file_change.get("path")
            or file_change.get("id")
            or f"file_change_{index}"
        )
        diff_summary = _non_empty_text(file_change.get("diff_summary"))
        raw_changes = file_change.get("changes")
        changes = (
            [change for change in raw_changes if isinstance(change, dict)]
            if isinstance(raw_changes, list)
            else []
        )

        if not changes:
            entries.append(
                {
                    "path": base_path,
                    "kind": _fallback_file_change_kind(file_change),
                    "diff": diff_summary,
                }
            )
            continue

        if len(changes) == 1:
            change = changes[0]
            entries.append(
                {
                    "path": str(change.get("path") or base_path),
                    "kind": _format_file_change_kind(change.get("kind"))
                    or _fallback_file_change_kind(file_change),
                    "diff": _non_empty_text(change.get("diff"))
                    or diff_summary,
                }
            )
            continue

        diff_attached = False
        for change in changes:
            diff_text = _non_empty_text(change.get("diff"))
            if (
                diff_text is None
                and diff_summary is not None
                and not diff_attached
            ):
                diff_text = diff_summary
                diff_attached = True

            entries.append(
                {
                    "path": str(change.get("path") or base_path),
                    "kind": _format_file_change_kind(change.get("kind"))
                    or _fallback_file_change_kind(file_change),
                    "diff": diff_text,
                }
            )

    return entries


def _build_status_changed_item_entry(item: dict[str, Any]) -> str:
    item_id = str(item.get("id") or "unknown")
    item_type = str(item.get("type") or "unknown")
    status = str(item.get("status") or "unknown")

    if item_type == "commandExecution":
        header = (
            f"{_format_inline_code(_format_command_text(item.get('command')))} "
            f"({_build_command_metadata(item)})"
        )
        if _is_failed_command(item):
            header += " ⚠ FAILED"
        output = _select_changed_item_text(item)
        if output is None:
            return header
        return "\n".join([header, _wrap_fenced_block(output)])

    if item_type == "fileChange":
        raw_changes = item.get("changes")
        changes = (
            [change for change in raw_changes if isinstance(change, dict)]
            if isinstance(raw_changes, list)
            else []
        )
        path = item_id
        kind = "unknown"
        meta_parts = []
        if changes:
            first_change = changes[0]
            path = str(first_change.get("path") or item_id)
            kind = _format_file_change_kind(first_change.get("kind")) or kind
            if len(changes) > 1:
                meta_parts.append(f"{len(changes)} 个文件")
        meta_parts.append(f"status: {status}")
        header = (
            f"{_format_inline_code(path)} "
            f"({kind}, {', '.join(meta_parts)})"
        )
        diff_text = _select_changed_item_text(item)
        if diff_text is not None:
            return "\n".join([header, _wrap_fenced_block(diff_text, "diff")])
        return "\n".join([header, _build_file_change_empty_note(kind)])

    if item_type == "reasoning":
        payload = _select_reasoning_payload(item)
        sections = [f"推理 {_format_inline_code(item_id)} (status: {status})"]
        quoted_parts: list[str] = []
        if payload is not None:
            summary = _non_empty_text(payload.get("summary"))
            text = _non_empty_text(payload.get("text"))
            if summary is not None:
                quoted_parts.append(_wrap_quote_block(summary))
            if text is not None:
                quoted_parts.append(_wrap_quote_block(text))
        if quoted_parts:
            sections.append("\n\n".join(quoted_parts))
        return "\n".join(sections)

    header = f"{item_type} {_format_inline_code(item_id)} (status: {status})"
    text = _select_changed_item_text(item)
    if text is None:
        return header
    return "\n".join([header, _wrap_quote_block(text)])


def _build_status_changed_items_block(
    changed_items: list[dict[str, Any]],
) -> str | None:
    if not changed_items:
        return None

    entries = [
        _build_status_changed_item_entry(item)
        for item in changed_items
        if isinstance(item, dict)
    ]
    if not entries:
        return None

    return (
        f"**变化详情** ({len(entries)} 项)\n\n"
        + "\n\n".join(entries)
    )


def build_status_content(structured: dict[str, Any]) -> list[str]:
    """构造 codex_status 的人类可读 Markdown 摘要。"""

    changed_items = structured.get("changed_items", [])
    if not isinstance(changed_items, list):
        changed_items = []

    type_counter = Counter(
        str(item.get("type", "unknown"))
        for item in changed_items
        if isinstance(item, dict)
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

    blocks = ["\n".join(lines)]
    changed_items_block = _build_status_changed_items_block(changed_items)
    if changed_items_block is not None:
        blocks.append(changed_items_block)
    return blocks


def build_result_structured(
    collector: Any,
    *,
    detail: Literal["compact", "full", "raw"] = "compact",
    include_raw_events: bool = False,
) -> dict[str, Any]:
    """构造 codex_result / codex 的结构化结果。

    注意：detail 参数保留向后兼容，但投影层统一使用紧凑模式。
    多行文本已迁移至 Markdown TextContent 块渲染，
    JSON 块中的大文本由 _slim_for_json_block() 剥离。
    """

    aggregated = collector.get_aggregated_result()
    final_result = {
        "agent_messages_text": aggregated["agent_messages_text"],
        "agent_message_items": aggregated["agent_message_items"],
        "command_executions": [
            _project_command_execution(item)
            for item in aggregated["command_executions"]
        ],
        "file_changes": [
            _project_file_change(item)
            for item in aggregated["file_changes"]
        ],
        "reasoning_segments": [
            _project_reasoning_segment(item)
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
        structured["raw_events"] = [
            _project_raw_event(event)
            for event in raw_status.get("new_events", [])
        ]
    return structured


def _build_result_command_block(
    command_executions: list[dict[str, Any]],
) -> str | None:
    if not command_executions:
        return None

    entries: list[str] = []
    for execution in command_executions:
        header = (
            f"{_format_inline_code(_format_command_text(execution.get('command')))} "
            f"({_build_command_metadata(execution)})"
        )
        if _is_failed_command(execution):
            header += " ⚠ FAILED"

        parts = [header]
        output = _non_empty_text(execution.get("output"))
        if output is not None:
            parts.append(_wrap_fenced_block(output))
        entries.append("\n".join(parts))

    return f"**命令执行** ({len(entries)} 条)\n\n" + "\n\n".join(entries)


def _build_result_file_changes_block(
    file_changes: list[dict[str, Any]],
) -> str | None:
    entries = _build_file_change_entries(file_changes)
    if not entries:
        return None

    rendered = [
        _render_file_change_entry(
            str(entry["path"]),
            str(entry["kind"]),
            entry["diff"] if isinstance(entry.get("diff"), str) else None,
        )
        for entry in entries
    ]
    return f"**文件变更** ({len(rendered)} 个文件)\n\n" + "\n\n".join(rendered)


def _build_result_reasoning_block(
    reasoning_segments: list[dict[str, Any]],
) -> str | None:
    entries: list[str] = []
    for segment in reasoning_segments:
        parts: list[str] = []
        summary = _non_empty_text(segment.get("summary"))
        text = _non_empty_text(segment.get("text"))
        if summary is not None:
            parts.append(_wrap_quote_block(summary))
        if text is not None:
            parts.append(_wrap_quote_block(text))
        if parts:
            entries.append("\n\n".join(parts))

    if not entries:
        return None

    return f"**推理过程** ({len(entries)} 段)\n\n" + "\n\n".join(entries)


def build_result_content(structured: dict[str, Any]) -> list[str]:
    """构造 codex / codex_result 的人类可读 Markdown 内容。"""

    final_result = structured["final_result"]
    agent_text = (
        final_result.get("agent_messages_text")
        or "_没有可展示的代理消息输出_"
    )
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

    blocks = [agent_text, "\n".join(summary_lines)]

    command_block = _build_result_command_block(
        final_result["command_executions"]
    )
    if command_block is not None:
        blocks.append(command_block)

    file_changes_block = _build_result_file_changes_block(
        final_result["file_changes"]
    )
    if file_changes_block is not None:
        blocks.append(file_changes_block)

    reasoning_block = _build_result_reasoning_block(
        final_result["reasoning_segments"]
    )
    if reasoning_block is not None:
        blocks.append(reasoning_block)

    return blocks


def build_start_content(data: dict[str, Any]) -> list[str]:
    """构造 codex_start 的人类可读 Markdown 内容。"""

    thread_id = data.get("thread_id", "")
    lines = [
        "**任务已启动**",
        f"- 线程：`{thread_id}`",
        f'- 状态：{data.get("status", "unknown")}',
        "",
        f'使用 `codex_status(thread_id="{thread_id}")` 查看进度。',
    ]
    return ["\n".join(lines)]


def build_interrupt_content(data: dict[str, Any]) -> list[str]:
    """构造 codex_interrupt 的人类可读 Markdown 内容。"""

    message = data.get("message", "中断请求已发送。")
    lines = ["**中断**", f"- {message}"]
    events_collected = data.get("events_collected")
    if events_collected is not None:
        lines.append(f"- 已收集事件数：{events_collected}")
    return ["\n".join(lines)]


def build_approve_content(data: dict[str, Any]) -> list[str]:
    """构造 codex_approve 的人类可读 Markdown 内容。"""

    message = data.get("message", "审批已处理。")
    lines = [f"**{message}**"]
    remaining = data.get("remaining_approvals")
    if remaining is not None:
        lines.append(f"- 剩余审批：{remaining}")
    return ["\n".join(lines)]
