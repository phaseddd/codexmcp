from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codexmcp.collector import EventCollector


def build_completed_collector() -> EventCollector:
    collector = EventCollector("thr_completed")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_1", "threadId": "thr_completed"}},
    )
    collector.append_event(
        "item/started",
        {"item": {"id": "msg_1", "type": "agentMessage"}},
    )
    collector.append_event(
        "item/agentMessage/delta",
        {"itemId": "msg_1", "delta": "第一段结果"},
    )
    collector.append_event(
        "item/completed",
        {"item": {"id": "msg_1", "type": "agentMessage", "status": "completed"}},
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
        {"item": {"id": "file_1", "type": "fileChange"}},
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
                "changes": [{"path": "README.md", "kind": "update"}],
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


def build_multi_message_collector() -> EventCollector:
    collector = EventCollector("thr_multi")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_multi", "threadId": "thr_multi"}},
    )
    for item_id, text in (("msg_1", "第一段"), ("msg_2", "第二段")):
        collector.append_event(
            "item/started",
            {"item": {"id": item_id, "type": "agentMessage"}},
        )
        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": item_id, "delta": text},
        )
        collector.append_event(
            "item/completed",
            {"item": {"id": item_id, "type": "agentMessage", "status": "completed"}},
        )
    collector.append_event(
        "turn/completed",
        {"turn": {"id": "turn_multi", "threadId": "thr_multi"}},
    )
    return collector


def build_transport_lost_collector() -> EventCollector:
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
        {"itemId": "msg_1", "delta": "partial"},
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


def build_failed_collector() -> EventCollector:
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


def build_reasoning_collector() -> EventCollector:
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


def build_running_collector() -> EventCollector:
    collector = EventCollector("thr_running")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_running", "threadId": "thr_running"}},
    )
    collector.append_event(
        "item/started",
        {"item": {"id": "msg_1", "type": "agentMessage"}},
    )
    collector.append_event(
        "item/agentMessage/delta",
        {"itemId": "msg_1", "delta": "still running"},
    )
    return collector


def build_large_output_collector() -> EventCollector:
    collector = EventCollector("thr_large")
    collector.append_event(
        "turn/started",
        {"turn": {"id": "turn_large", "threadId": "thr_large"}},
    )
    collector.append_event(
        "item/started",
        {"item": {"id": "msg_1", "type": "agentMessage"}},
    )
    collector.append_event(
        "item/agentMessage/delta",
        {"itemId": "msg_1", "delta": "最终说明"},
    )
    collector.append_event(
        "item/completed",
        {"item": {"id": "msg_1", "type": "agentMessage", "status": "completed"}},
    )
    large_output = ("HEAD\n" + ("x" * 2600) + "\nTAIL\n")
    collector.append_event(
        "item/started",
        {
            "item": {
                "id": "cmd_1",
                "type": "commandExecution",
                "command": ["rg", "TODO"],
            }
        },
    )
    collector.append_event(
        "item/commandExecution/outputDelta",
        {"itemId": "cmd_1", "delta": large_output},
    )
    collector.append_event(
        "item/completed",
        {
            "item": {
                "id": "cmd_1",
                "type": "commandExecution",
                "status": "completed",
                "command": ["rg", "TODO"],
                "exitCode": 0,
                "durationMs": 88,
            }
        },
    )
    collector.append_event(
        "turn/completed",
        {"turn": {"id": "turn_large", "threadId": "thr_large"}},
    )
    return collector


class FakeBridge:
    def __init__(self, collector: EventCollector | None) -> None:
        self._collector = collector

    def get_collector(self, thread_id: str) -> EventCollector | None:
        if self._collector and self._collector.thread_id == thread_id:
            return self._collector
        return None


class ReadyBridge:
    async def ensure_ready(self) -> None:
        return None


class FakeContext:
    async def report_progress(self, **_: int) -> None:
        return None


class BlockingBridge:
    def __init__(self, mode: str = "transport_lost") -> None:
        self.mode = mode
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
        self,
        thread_id: str,
        turn_id: str | None,
    ) -> dict[str, str | None]:
        return {"thread_id": thread_id, "turn_id": turn_id}

    async def rpc_call(
        self,
        method: str,
        params: dict[str, object],
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
                "item/completed",
                {"item": {"id": "msg_1", "type": "agentMessage", "status": "completed"}},
            )

            if self.mode == "completed":
                collector.append_event(
                    "turn/completed",
                    {"turn": {"id": "turn_blocking", "threadId": thread_id}},
                )
            else:
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


@pytest.fixture
def completed_collector() -> EventCollector:
    return build_completed_collector()


@pytest.fixture
def multi_message_collector() -> EventCollector:
    return build_multi_message_collector()


@pytest.fixture
def transport_lost_collector() -> EventCollector:
    return build_transport_lost_collector()


@pytest.fixture
def failed_collector() -> EventCollector:
    return build_failed_collector()


@pytest.fixture
def reasoning_collector() -> EventCollector:
    return build_reasoning_collector()


@pytest.fixture
def running_collector() -> EventCollector:
    return build_running_collector()


@pytest.fixture
def large_output_collector() -> EventCollector:
    return build_large_output_collector()


@pytest.fixture
def fake_context() -> FakeContext:
    return FakeContext()


@pytest.fixture
def fake_bridge_factory():
    return FakeBridge


@pytest.fixture
def ready_bridge_factory():
    return ReadyBridge


@pytest.fixture
def blocking_bridge_factory():
    return BlockingBridge
