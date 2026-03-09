from __future__ import annotations

from codexmcp.collector import EventCollector


def test_completed_items_are_compact_in_incremental_view(completed_collector: EventCollector) -> None:
    status = completed_collector.read_incremental(0, view="compact")
    changed_by_id = {item["id"]: item for item in status["changed_items"]}

    assert status["status"] == "completed"
    assert "delta" not in changed_by_id["msg_1"]
    assert "content" not in changed_by_id["msg_1"]
    assert "delta" not in changed_by_id["cmd_1"]
    assert "content" not in changed_by_id["cmd_1"]
    assert changed_by_id["cmd_1"]["command"] == ["python", "--version"]
    assert changed_by_id["cmd_1"]["exit_code"] == 0
    assert "delta" not in changed_by_id["file_1"]
    assert "content" not in changed_by_id["file_1"]


def test_active_items_keep_delta_in_verbose_view(running_collector: EventCollector) -> None:
    status = running_collector.read_incremental(0, view="verbose")

    assert status["status"] == "running"
    assert status["changed_items"][0]["delta"] == "still running"
    assert status["changed_items"][0]["content"] == "still running"


def test_cursor_stale_marks_resync_required(monkeypatch) -> None:
    monkeypatch.setattr(EventCollector, "MAX_EVENTS_PER_THREAD", 4)
    collector = EventCollector("thr_stale")
    for index in range(6):
        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": "msg_1", "delta": str(index)},
        )

    status = collector.read_incremental(0, view="compact")

    assert status["cursor_stale"] is True
    assert status["resync_required"] is True
    assert status["oldest_cursor"] > 0
    assert status["next_cursor"] == 6


def test_agent_message_boundaries_are_preserved(multi_message_collector: EventCollector) -> None:
    final_result = multi_message_collector.get_aggregated_result()

    assert final_result["agent_messages_text"] == "第一段\n\n第二段"
    assert final_result["agent_message_items"] == [
        {"id": "msg_1", "text": "第一段"},
        {"id": "msg_2", "text": "第二段"},
    ]


def test_transport_disconnect_is_not_mapped_to_turn_error(transport_lost_collector: EventCollector) -> None:
    status = transport_lost_collector.read_incremental(0, view="compact")

    assert transport_lost_collector.turn_error is None
    assert status["status"] == "transport_lost"
    assert status["has_error"] is False
    assert status["transport"]["disconnected"] is True
    assert status["diagnostic_events"][0]["method"] == "bridge/disconnected"


def test_reasoning_is_aggregated_in_final_result(reasoning_collector: EventCollector) -> None:
    final_result = reasoning_collector.get_aggregated_result()

    assert final_result["reasoning_segments"] == [
        {
            "id": "reason_1",
            "status": "completed",
            "summary": "先总结",
            "text": "再展开",
        }
    ]


def test_reset_for_new_turn_resets_oldest_cursor(monkeypatch) -> None:
    monkeypatch.setattr(EventCollector, "MAX_EVENTS_PER_THREAD", 4)
    collector = EventCollector("thr_reset")
    for index in range(6):
        collector.append_event(
            "item/agentMessage/delta",
            {"itemId": "msg_1", "delta": str(index)},
        )

    stale_status = collector.read_incremental(0, view="compact")
    assert stale_status["cursor_stale"] is True

    collector.reset_for_new_turn()
    fresh_status = collector.read_incremental(0, view="compact")

    assert fresh_status["oldest_cursor"] == 0
    assert fresh_status["cursor_stale"] is False
    assert fresh_status["next_cursor"] == 0
