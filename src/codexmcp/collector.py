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
    _LIFECYCLE_METHODS = {
        "turn/started",
        "turn/completed",
        "turn/error",
        "item/started",
        "item/completed",
        "thread/tokenUsage/updated",
        "bridge/disconnected",
    }
    _DIAGNOSTIC_METHODS = {"bridge/disconnected"}

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.events: list[CollectedEvent] = []
        self.items: dict[str, ItemState] = {}
        self.turn_completed: asyncio.Event = asyncio.Event()
        self.turn_result: dict[str, Any] | None = None
        self.turn_error: dict[str, Any] | None = None
        self.transport_error: dict[str, Any] | None = None
        self.transport_disconnected: bool = False
        self.process_exit_code: int | None = None
        self.disconnect_reason: str | None = None
        self.completion_state: str = "running"
        self.current_turn_id: str | None = None
        self.truncated: bool = False
        self.last_access_time: float = time.time()
        self.last_event_method: str | None = None
        self.pending_approvals: dict[int, dict[str, Any]] = {}
        self.auto_approve: bool = False
        self._token_usage: dict[str, Any] = {}  # thread/tokenUsage/updated 事件缓存
        self._next_event_index: int = 0

    def append_event(self, method: str, params: dict[str, Any]) -> None:
        """追加事件，更新 Item 状态机和 turn/transport 状态。"""
        if len(self.events) >= self.MAX_EVENTS_PER_THREAD:
            half = len(self.events) // 2
            self.events = self.events[half:]
            self.truncated = True

        event = CollectedEvent(
            index=self._next_event_index,
            timestamp=time.time(),
            method=method,
            params=params,
            summary=self._make_summary(method, params),
        )
        self.events.append(event)
        self._next_event_index += 1
        self.last_event_method = method

        if method == "item/started":
            item_payload = params.get("item", {})
            item_id = item_payload.get("id", "")
            if item_id:
                metadata = dict(item_payload)
                self.items[item_id] = ItemState(
                    item_id=item_id,
                    item_type=item_payload.get("type", "unknown"),
                    status=item_payload.get("status") or "started",
                    metadata_started=metadata,
                    metadata_effective=metadata,
                )

        elif self._is_item_delta_event(method):
            item_id = params.get("itemId", "")
            item = self._ensure_item_state(
                item_id, self._infer_item_type_from_method(method)
            )
            if item is not None:
                item.status = "streaming"
                delta = params.get("delta", "")
                if method == "item/reasoning/summaryDelta":
                    item.reasoning_summary += delta
                elif method == "item/reasoning/textDelta":
                    item.reasoning_text += delta
                else:
                    item.content_buffer += delta

        elif method == "item/completed":
            completed_item = params.get("item", {})
            item_id = completed_item.get("id", "")
            item = self._ensure_item_state(
                item_id, completed_item.get("type", "unknown")
            )
            if item is not None:
                completed_metadata = dict(completed_item)
                item.metadata_completed = completed_metadata
                item.metadata_effective = self._merge_dicts(
                    item.metadata_effective or item.metadata_started,
                    completed_metadata,
                )
                item.status = completed_item.get("status") or "completed"

        elif method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage", {})
            if token_usage:
                self._token_usage = token_usage

        elif method == "turn/completed":
            self.turn_result = params
            self.completion_state = "completed"
            self.turn_completed.set()

        elif method == "turn/error":
            self.turn_error = params
            self.completion_state = "failed"
            self.turn_completed.set()

        elif method == "turn/started":
            turn = params.get("turn", {})
            self.current_turn_id = turn.get("id")
            self.completion_state = "running"

        elif method == "bridge/disconnected":
            self.transport_disconnected = True
            self.transport_error = dict(params) if params else {
                "disconnect_reason": "transport_disconnected"
            }
            self.process_exit_code = params.get("process_exit_code")
            self.disconnect_reason = (
                params.get("disconnect_reason")
                or params.get("error")
                or "app-server 传输已断开"
            )
            if self.completion_state == "running":
                self.completion_state = "transport_lost"
            self.turn_completed.set()

    def read_incremental(
        self,
        since_index: int = 0,
        *,
        raw_events: bool = False,
    ) -> dict[str, Any]:
        """从指定原始事件索引开始读取增量状态。"""
        self.touch()
        since_index = max(0, min(since_index, self._next_event_index))

        result = {
            "thread_id": self.thread_id,
            "completed": self.turn_completed.is_set(),
            "has_error": self.turn_error is not None,
            "status": self.completion_state,
            "truncated": self.truncated,
            "next_cursor": self._next_event_index,
            "transport": {
                "connected": not self.transport_disconnected,
                "disconnected": self.transport_disconnected,
                "process_exit_code": self.process_exit_code,
                "disconnect_reason": self.disconnect_reason,
            },
            "diagnostics": {
                "last_event_method": self.last_event_method,
                "event_count": self._next_event_index,
                "active_items": self._build_active_items(),
            },
            "changed_items": self._build_changed_items_since(since_index),
            "lifecycle_events": self._build_lifecycle_events_since(since_index),
            "diagnostic_events": self._build_diagnostic_events_since(
                since_index
            ),
            "pending_approvals": [
                {"request_id": rid, "params": params}
                for rid, params in self.pending_approvals.items()
            ],
        }

        if raw_events:
            result["new_events"] = [
                self._serialize_raw_event(event)
                for event in self._events_since(since_index)
            ]

        return result

    def get_aggregated_result(self) -> dict[str, Any]:
        """聚合当前 turn 的 Item 状态为最终结果。"""
        agent_messages: list[str] = []
        command_executions: list[dict[str, Any]] = []
        file_changes: list[dict[str, Any]] = []
        reasoning_segments: list[dict[str, Any]] = []

        for item in self.items.values():
            metadata = self._get_effective_metadata(item)
            status = metadata.get("status") or item.status

            if item.item_type == "agentMessage":
                if item.content_buffer:
                    agent_messages.append(item.content_buffer)
            elif item.item_type == "commandExecution":
                command_executions.append(
                    {
                        "id": item.item_id,
                        "command": metadata.get("command", []),
                        "status": status,
                        "output": item.content_buffer,
                        "exit_code": metadata.get("exitCode"),
                        "duration_ms": metadata.get("durationMs"),
                    }
                )
            elif item.item_type == "fileChange":
                changes = self._normalize_changes(metadata.get("changes"))
                file_change: dict[str, Any] = {
                    "id": item.item_id,
                    "status": status,
                    "changes": changes,
                }
                path = metadata.get("path") or self._derive_primary_path(changes)
                if path:
                    file_change["path"] = path
                if item.content_buffer:
                    file_change["diff_summary"] = item.content_buffer
                file_changes.append(file_change)
            elif item.item_type == "reasoning":
                reasoning_segment: dict[str, Any] = {
                    "id": item.item_id,
                    "status": status,
                }
                if item.reasoning_summary:
                    reasoning_segment["summary"] = item.reasoning_summary
                if item.reasoning_text:
                    reasoning_segment["text"] = item.reasoning_text
                if len(reasoning_segment) > 2:
                    reasoning_segments.append(reasoning_segment)

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
            "agent_messages": "".join(agent_messages),
            "command_executions": command_executions,
            "file_changes": file_changes,
            "reasoning_segments": reasoning_segments,
            "token_usage": token_usage,
            "event_count": self._next_event_index,
        }

    def reset_for_new_turn(self) -> None:
        """重置 turn 相关状态，准备接收新 turn 的事件。

        创建新的 asyncio.Event 实例（而非 .clear()），
        确保不会影响其他协程对旧 Event 的引用。
        """
        self.events = []
        self.items = {}
        self.turn_completed = asyncio.Event()
        self.turn_result = None
        self.turn_error = None
        self.transport_error = None
        self.transport_disconnected = False
        self.process_exit_code = None
        self.disconnect_reason = None
        self.completion_state = "running"
        self.current_turn_id = None
        self.truncated = False
        self.last_event_method = None
        self.pending_approvals.clear()
        self._token_usage = {}
        self._next_event_index = 0

    def is_completed(self) -> bool:
        """检查 turn 是否已完成（完成/失败/传输断开都算结束）。"""
        return self.turn_completed.is_set()

    def get_current_turn_id(self) -> str | None:
        """获取当前 turn 的 ID（用于中断操作）。"""
        return self.current_turn_id

    def touch(self) -> None:
        """更新最后访问时间（TTL 清理用）。"""
        self.last_access_time = time.time()

    def _events_since(self, since_index: int) -> list[CollectedEvent]:
        return [event for event in self.events if event.index >= since_index]

    def _build_active_items(self) -> dict[str, dict[str, Any]]:
        return {
            item_id: {"type": item.item_type, "status": item.status}
            for item_id, item in self.items.items()
            if item.status not in {"completed", "failed", "cancelled"}
        }

    def _build_changed_items_since(
        self, since_index: int
    ) -> list[dict[str, Any]]:
        changed: dict[str, dict[str, Any]] = {}

        for event in self._events_since(since_index):
            method = event.method
            params = event.params

            if method == "item/started":
                item_payload = params.get("item", {})
                item_id = item_payload.get("id", "")
                if item_id:
                    changed.setdefault(item_id, self._empty_change_record())[
                        "started"
                    ] = True

            elif method == "item/completed":
                item_payload = params.get("item", {})
                item_id = item_payload.get("id", "")
                if item_id:
                    changed.setdefault(item_id, self._empty_change_record())[
                        "completed"
                    ] = True

            elif self._is_item_delta_event(method):
                item_id = params.get("itemId", "")
                if item_id:
                    change = changed.setdefault(
                        item_id, self._empty_change_record()
                    )
                    delta = params.get("delta", "")
                    if method == "item/reasoning/summaryDelta":
                        change["reasoning_summary_delta"] += delta
                    elif method == "item/reasoning/textDelta":
                        change["reasoning_text_delta"] += delta
                    else:
                        change["content_delta"] += delta

        snapshots: list[dict[str, Any]] = []
        for item_id, change in changed.items():
            item = self.items.get(item_id)
            if item is None:
                continue

            metadata = self._get_effective_metadata(item)
            status = metadata.get("status") or item.status
            snapshot: dict[str, Any] = {
                "id": item.item_id,
                "type": item.item_type,
                "status": status,
                "delta": self._build_item_delta(item, change),
            }

            include_content = (
                since_index == 0 or change["started"] or change["completed"]
            )
            if include_content:
                content = self._build_item_content(item)
                if content not in ("", {}, [], None):
                    snapshot["content"] = content

            if item.item_type == "commandExecution":
                snapshot["command"] = metadata.get("command", [])
                snapshot["exit_code"] = metadata.get("exitCode")
                snapshot["duration_ms"] = metadata.get("durationMs")
            elif item.item_type == "fileChange":
                snapshot["changes"] = self._normalize_changes(
                    metadata.get("changes")
                )

            snapshots.append(snapshot)

            item.last_emitted_content_len = len(item.content_buffer)
            item.last_emitted_reasoning_summary_len = len(
                item.reasoning_summary
            )
            item.last_emitted_reasoning_text_len = len(item.reasoning_text)

        return snapshots

    def _build_lifecycle_events_since(
        self, since_index: int
    ) -> list[dict[str, Any]]:
        return [
            self._serialize_lifecycle_event(event)
            for event in self._events_since(since_index)
            if self._is_lifecycle_event(event.method)
        ]

    def _build_diagnostic_events_since(
        self, since_index: int
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for event in self._events_since(since_index):
            if event.method not in self._DIAGNOSTIC_METHODS:
                continue
            payload = self._serialize_lifecycle_event(event)
            payload["error"] = event.params.get("error")
            payload["timestamp"] = event.params.get("timestamp")
            transport_error = event.params.get("transport_error")
            if transport_error is not None:
                payload["transport_error"] = transport_error
            events.append(payload)
        return events

    def _serialize_raw_event(self, event: CollectedEvent) -> dict[str, Any]:
        return {
            "index": event.index,
            "method": event.method,
            "summary": event.summary,
            "params": event.params,
        }

    def _serialize_lifecycle_event(
        self, event: CollectedEvent
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": event.index,
            "method": event.method,
            "summary": event.summary,
        }
        params = event.params

        if event.method in {"turn/started", "turn/completed"}:
            turn = params.get("turn", {})
            if turn.get("id"):
                payload["turn_id"] = turn["id"]
        elif event.method == "turn/error":
            if params.get("message"):
                payload["message"] = params["message"]
        elif event.method in {"item/started", "item/completed"}:
            item_payload = params.get("item", {})
            if item_payload.get("id"):
                payload["item_id"] = item_payload["id"]
            if item_payload.get("type"):
                payload["item_type"] = item_payload["type"]
            if item_payload.get("status"):
                payload["status"] = item_payload["status"]
        elif event.method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage", {})
            if token_usage:
                payload["token_usage"] = token_usage
        elif event.method.startswith("approvalRequest/"):
            request_id = params.get("request_id") or params.get("requestId")
            if request_id is not None:
                payload["request_id"] = request_id
        elif event.method == "bridge/disconnected":
            payload["disconnect_reason"] = (
                params.get("disconnect_reason") or params.get("error")
            )
            payload["process_exit_code"] = params.get("process_exit_code")

        return payload

    def _ensure_item_state(
        self, item_id: str, item_type: str = "unknown"
    ) -> ItemState | None:
        if not item_id:
            return None

        item = self.items.get(item_id)
        if item is None:
            item = ItemState(
                item_id=item_id,
                item_type=item_type or "unknown",
                status="started",
            )
            self.items[item_id] = item
        elif item_type and item.item_type == "unknown":
            item.item_type = item_type
        return item

    def _get_effective_metadata(self, item: ItemState) -> dict[str, Any]:
        return (
            item.metadata_effective
            or item.metadata_completed
            or item.metadata_started
        )

    def _normalize_changes(self, changes: Any) -> list[Any]:
        if isinstance(changes, list):
            return changes
        return []

    def _derive_primary_path(self, changes: list[Any]) -> str | None:
        for change in changes:
            if isinstance(change, dict) and change.get("path"):
                return str(change["path"])
        return None

    def _build_item_delta(
        self, item: ItemState, change: dict[str, Any]
    ) -> str | dict[str, str]:
        if item.item_type == "reasoning":
            delta: dict[str, str] = {}
            if change["reasoning_summary_delta"]:
                delta["summary"] = change["reasoning_summary_delta"]
            if change["reasoning_text_delta"]:
                delta["text"] = change["reasoning_text_delta"]
            return delta
        return change["content_delta"]

    def _build_item_content(self, item: ItemState) -> str | dict[str, str]:
        if item.item_type == "reasoning":
            content: dict[str, str] = {}
            if item.reasoning_summary:
                content["summary"] = item.reasoning_summary
            if item.reasoning_text:
                content["text"] = item.reasoning_text
            return content
        return item.content_buffer

    def _merge_dicts(
        self, base: dict[str, Any], override: dict[str, Any]
    ) -> dict[str, Any]:
        """递归合并字典，忽略 override 中的 None 值。

        该策略用于保留 started 阶段已经拿到的有效字段，
        避免 completed 事件里缺省的 null 覆盖已有值。
        """
        merged = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge_dicts(merged[key], value)
            elif value is not None:
                merged[key] = value
        return merged

    def _infer_item_type_from_method(self, method: str) -> str:
        parts = method.split("/")
        if len(parts) >= 2 and parts[0] == "item":
            return parts[1]
        return "unknown"

    def _empty_change_record(self) -> dict[str, Any]:
        return {
            "started": False,
            "completed": False,
            "content_delta": "",
            "reasoning_summary_delta": "",
            "reasoning_text_delta": "",
        }

    def _is_lifecycle_event(self, method: str) -> bool:
        return method in self._LIFECYCLE_METHODS or method.startswith(
            "approvalRequest/"
        )

    def _is_item_delta_event(self, method: str) -> bool:
        return method.startswith("item/") and (
            method.endswith("/delta") or method.endswith("Delta")
        )

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
