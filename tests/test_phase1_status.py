"""Phase 1 / P0 状态语义与聚合结果回归测试。"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

if "mcp.server.fastmcp" not in sys.modules:
    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")

    class _TestContext:
        async def report_progress(self, **_: int) -> None:
            return None

    class _TestFastMCP:
        def __init__(self, *_: object, **__: object) -> None:
            return None

        def tool(self, *_: object, **__: object):
            def decorator(func):
                return func

            return decorator

        def run(self, *_: object, **__: object) -> None:
            return None

    fastmcp_module.Context = _TestContext
    fastmcp_module.FastMCP = _TestFastMCP
    server_module.fastmcp = fastmcp_module
    mcp_module.server = server_module
    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.server"] = server_module
    sys.modules["mcp.server.fastmcp"] = fastmcp_module

from codexmcp.collector import EventCollector
from codexmcp.server import codex, codex_status


def _build_completed_collector() -> EventCollector:
    collector = EventCollector("thr_completed")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_1", "threadId": "thr_completed"}},
    )
    collector.append_event(
        "item/started",
        {
            "item": {
                "id": "cmd_1",
                "type": "commandExecution",
                "command": ["python", "--version"],
            }
        },
    )
    collector.append_event(
        "item/commandExecution/outputDelta",
        {"itemId": "cmd_1", "delta": "Python 3.12.0\n"},
    )
    collector.append_event(
        "item/completed",
        {
            "item": {
                "id": "cmd_1",
                "type": "commandExecution",
                "status": "completed",
                "command": ["python", "--version"],
                "exitCode": 0,
                "durationMs": 42,
            }
        },
    )
    collector.append_event(
        "item/started",
        {
            "item": {
                "id": "file_1",
                "type": "fileChange",
            }
        },
    )
    collector.append_event(
        "item/fileChange/outputDelta",
        {"itemId": "file_1", "delta": "--- a/README.md\n+++ b/README.md\n"},
    )
    collector.append_event(
        "item/completed",
        {
            "item": {
                "id": "file_1",
                "type": "fileChange",
                "status": "completed",
                "changes": [
                    {"path": "README.md", "kind": "update"},
                ],
            }
        },
    )
    collector.append_event(
        "thread/tokenUsage/updated",
        {"tokenUsage": {"total": {"inputTokens": 10, "outputTokens": 20}}},
    )
    collector.append_event(
        "turn/completed",
        {"turn": {"id": "turn_1", "threadId": "thr_completed"}},
    )
    return collector


def _build_transport_lost_collector() -> EventCollector:
    collector = EventCollector("thr_transport")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_2", "threadId": "thr_transport"}},
    )
    collector.append_event(
        "item/started",
        {"item": {"id": "msg_1", "type": "agentMessage"}},
    )
    collector.append_event(
        "item/agentMessage/delta",
        {"itemId": "msg_1", "delta": "hello"},
    )
    collector.append_event(
        "bridge/disconnected",
        {
            "error": "app-server 进程已断开",
            "disconnect_reason": "stdout_eof",
            "process_exit_code": 1,
            "timestamp": 123.0,
            "transport_error": {"type": "stdout_eof"},
        },
    )
    return collector


def _build_failed_collector() -> EventCollector:
    collector = EventCollector("thr_failed")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_failed", "threadId": "thr_failed"}},
    )
    collector.append_event(
        "turn/error",
        {"message": "Turn 执行失败", "code": "turn_failed"},
    )
    return collector


def _build_reasoning_collector() -> EventCollector:
    collector = EventCollector("thr_reasoning")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_reasoning", "threadId": "thr_reasoning"}},
    )
    collector.append_event(
        "item/started",
        {"item": {"id": "reason_1", "type": "reasoning"}},
    )
    collector.append_event(
        "item/reasoning/summaryDelta",
        {"itemId": "reason_1", "delta": "先总结"},
    )
    collector.append_event(
        "item/reasoning/textDelta",
        {"itemId": "reason_1", "delta": "再展开"},
    )
    collector.append_event(
        "item/completed",
        {
            "item": {
                "id": "reason_1",
                "type": "reasoning",
                "status": "completed",
            }
        },
    )
    collector.append_event(
        "turn/completed",
        {"turn": {"id": "turn_reasoning", "threadId": "thr_reasoning"}},
    )
    return collector


class _FakeBridge:
    def __init__(self, collector: EventCollector | None) -> None:
        self._collector = collector

    def get_collector(self, thread_id: str) -> EventCollector | None:
        if self._collector and self._collector.thread_id == thread_id:
            return self._collector
        return None


class _FakeContext:
    async def report_progress(self, **_: int) -> None:
        return None


class _BlockingBridge:
    def __init__(self) -> None:
        self._collectors: dict[str, EventCollector] = {}

    async def ensure_ready(self) -> None:
        return None

    def get_or_create_collector(self, thread_id: str) -> EventCollector:
        collector = self._collectors.get(thread_id)
        if collector is None:
            collector = EventCollector(thread_id)
            self._collectors[thread_id] = collector
        return collector

    def get_collector(self, thread_id: str) -> EventCollector | None:
        return self._collectors.get(thread_id)

    def rebind_collector(self, old_thread_id: str, new_thread_id: str) -> None:
        collector = self._collectors.pop(old_thread_id)
        collector.thread_id = new_thread_id
        self._collectors[new_thread_id] = collector

    def remove_collector(self, thread_id: str) -> None:
        self._collectors.pop(thread_id, None)

    async def interrupt_turn(
        self, thread_id: str, turn_id: str | None
    ) -> dict[str, str | None]:
        return {"thread_id": thread_id, "turn_id": turn_id}

    async def rpc_call(
        self, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        if method == "thread/start":
            return {"thread": {"id": "thr_blocking"}}

        if method == "turn/start":
            thread_id = str(params["threadId"])
            collector = self._collectors[thread_id]
            collector.append_event(
                "turn/started",
                {"turn": {"id": "turn_blocking", "threadId": thread_id}},
            )
            collector.append_event(
                "item/started",
                {"item": {"id": "msg_1", "type": "agentMessage"}},
            )
            collector.append_event(
                "item/agentMessage/delta",
                {"itemId": "msg_1", "delta": "partial"},
            )
            collector.append_event(
                "bridge/disconnected",
                {
                    "error": "app-server 传输已断开",
                    "disconnect_reason": "stdout_eof",
                    "process_exit_code": 1,
                    "timestamp": 321.0,
                },
            )
            return {"turn": {"id": "turn_blocking"}}

        raise AssertionError(f"未预期的 rpc_call: {method}")


class EventCollectorPhase1Tests(unittest.TestCase):
    def test_completed_item_metadata_is_merged_into_final_result(self) -> None:
        collector = _build_completed_collector()

        final_result = collector.get_aggregated_result()

        self.assertEqual(
            final_result["command_executions"][0]["exit_code"],
            0,
        )
        self.assertEqual(
            final_result["command_executions"][0]["duration_ms"],
            42,
        )
        self.assertEqual(
            final_result["command_executions"][0]["output"],
            "Python 3.12.0\n",
        )
        self.assertEqual(
            final_result["file_changes"][0]["changes"][0]["path"],
            "README.md",
        )
        self.assertEqual(
            final_result["file_changes"][0]["diff_summary"],
            "--- a/README.md\n+++ b/README.md\n",
        )
        self.assertEqual(
            final_result["token_usage"]["total"]["outputTokens"],
            20,
        )

    def test_transport_disconnect_is_not_mapped_to_turn_error(self) -> None:
        collector = _build_transport_lost_collector()

        status = collector.read_incremental(0)

        self.assertIsNone(collector.turn_error)
        self.assertEqual(status["status"], "transport_lost")
        self.assertFalse(status["has_error"])
        self.assertTrue(status["transport"]["disconnected"])
        self.assertEqual(status["transport"]["process_exit_code"], 1)
        self.assertEqual(
            status["diagnostic_events"][0]["method"],
            "bridge/disconnected",
        )

    def test_turn_error_sets_failed_completion_state(self) -> None:
        collector = _build_failed_collector()

        status = collector.read_incremental(0)

        self.assertTrue(status["completed"])
        self.assertTrue(status["has_error"])
        self.assertEqual(status["status"], "failed")
        self.assertEqual(collector.turn_error["message"], "Turn 执行失败")
        self.assertIn(
            "turn/error",
            {event["method"] for event in status["lifecycle_events"]},
        )

    def test_read_incremental_defaults_to_item_snapshots(self) -> None:
        collector = _build_completed_collector()

        status = collector.read_incremental(0)

        self.assertNotIn("new_events", status)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(len(status["changed_items"]), 2)
        lifecycle_methods = {
            event["method"] for event in status["lifecycle_events"]
        }
        self.assertIn("turn/started", lifecycle_methods)
        self.assertIn("item/completed", lifecycle_methods)
        self.assertEqual(status["diagnostics"]["event_count"], 9)

    def test_read_incremental_cursor_returns_deduplicated_item_deltas(self) -> None:
        collector = EventCollector("thr_cursor")
        collector.append_event(
            "item/started",
            {"item": {"id": "msg_1", "type": "agentMessage"}},
        )
        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": "msg_1", "delta": "he"},
        )

        first_poll = collector.read_incremental(0)

        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": "msg_1", "delta": "ll"},
        )
        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": "msg_1", "delta": "o"},
        )
        collector.append_event(
            "item/started",
            {"item": {"id": "msg_2", "type": "agentMessage"}},
        )
        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": "msg_2", "delta": "!"},
        )

        second_poll = collector.read_incremental(first_poll["next_cursor"])
        changed_by_id = {
            item["id"]: item for item in second_poll["changed_items"]
        }

        self.assertEqual(len(changed_by_id), 2)
        self.assertEqual(changed_by_id["msg_1"]["delta"], "llo")
        self.assertNotIn("content", changed_by_id["msg_1"])
        self.assertEqual(changed_by_id["msg_2"]["delta"], "!")
        self.assertEqual(changed_by_id["msg_2"]["content"], "!")

    def test_reasoning_deltas_are_aggregated_in_snapshots_and_final_result(self) -> None:
        collector = _build_reasoning_collector()

        status = collector.read_incremental(0)
        final_result = collector.get_aggregated_result()

        self.assertEqual(len(final_result["reasoning_segments"]), 1)
        self.assertEqual(
            final_result["reasoning_segments"][0]["summary"],
            "先总结",
        )
        self.assertEqual(
            final_result["reasoning_segments"][0]["text"],
            "再展开",
        )
        self.assertEqual(len(status["changed_items"]), 1)
        self.assertEqual(
            status["changed_items"][0]["delta"],
            {"summary": "先总结", "text": "再展开"},
        )
        self.assertEqual(
            status["changed_items"][0]["content"],
            {"summary": "先总结", "text": "再展开"},
        )

    def test_reset_for_new_turn_clears_phase1_state(self) -> None:
        collector = _build_transport_lost_collector()
        collector.pending_approvals[7] = {"action": "approve"}
        old_barrier = collector.turn_completed

        collector.reset_for_new_turn()
        status = collector.read_incremental(0)

        self.assertIsNot(old_barrier, collector.turn_completed)
        self.assertFalse(status["completed"])
        self.assertFalse(status["has_error"])
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["next_cursor"], 0)
        self.assertEqual(status["changed_items"], [])
        self.assertEqual(status["lifecycle_events"], [])
        self.assertEqual(status["diagnostic_events"], [])
        self.assertEqual(status["pending_approvals"], [])
        self.assertEqual(status["diagnostics"]["active_items"], {})
        self.assertFalse(status["transport"]["disconnected"])


class CodexStatusPhase1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_status_returns_final_result_when_completed(self) -> None:
        collector = _build_completed_collector()

        with patch(
            "codexmcp.server.get_bridge",
            return_value=_FakeBridge(collector),
        ):
            result = await codex_status("thr_completed")

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("final_result", result)
        self.assertEqual(
            result["final_result"]["command_executions"][0]["exit_code"],
            0,
        )
        self.assertNotIn("new_events", result)

    async def test_codex_status_supports_raw_events_opt_in(self) -> None:
        collector = _build_transport_lost_collector()

        with patch(
            "codexmcp.server.get_bridge",
            return_value=_FakeBridge(collector),
        ):
            result = await codex_status("thr_transport", raw_events=True)

        self.assertTrue(result["success"])
        self.assertIn("new_events", result)
        self.assertEqual(result["new_events"][0]["method"], "turn/started")
        self.assertEqual(result["status"], "transport_lost")

    async def test_codex_blocking_returns_transport_lost_message(self) -> None:
        bridge = _BlockingBridge()

        with patch("codexmcp.server.get_bridge", return_value=bridge):
            result = await codex(
                PROMPT="hello",
                cd=Path("D:/codexmcp"),
                ctx=_FakeContext(),
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "transport_lost")
        self.assertEqual(
            result["message"],
            "app-server 传输已断开，返回当前已聚合结果。",
        )
        self.assertTrue(result["transport"]["disconnected"])
        self.assertEqual(result["final_result"]["agent_messages"], "partial")


if __name__ == "__main__":
    unittest.main()
