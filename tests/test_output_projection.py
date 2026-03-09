from __future__ import annotations

from mcp.types import CallToolResult

from codexmcp.collector import EventCollector
from codexmcp.output import (
    build_error_result,
    build_result_structured,
    build_status_structured,
    preview_text,
    strip_ansi,
)


def test_preview_text_short_text_is_not_truncated() -> None:
    preview = preview_text("hello", limit=10)

    assert preview["text"] == "hello"
    assert preview["truncated"] is False
    assert preview["original_len"] == 5


def test_preview_text_long_text_is_head_tail_truncated() -> None:
    preview = preview_text("a" * 220, limit=100)

    assert preview["truncated"] is True
    assert preview["original_len"] == 220
    assert "... [已截断" in preview["text"]


def test_strip_ansi_removes_terminal_escape_sequences() -> None:
    text = "\x1b[31mred\x1b[0m plain"
    assert strip_ansi(text) == "red plain"


def test_build_status_structured_projects_compact_summary(
    completed_collector: EventCollector,
) -> None:
    structured = build_status_structured(
        completed_collector,
        cursor=0,
        raw_events=False,
        detail="compact",
    )

    assert structured["success"] is True
    assert structured["completed"] is True
    assert structured["final_result"]["message_count"] == 1
    assert structured["final_result"]["command_count"] == 1
    changed_by_id = {item["id"]: item for item in structured["changed_items"]}
    assert "content" not in changed_by_id["cmd_1"]
    assert "delta" not in changed_by_id["msg_1"]


def test_build_status_structured_previews_raw_events(
    large_output_collector: EventCollector,
) -> None:
    structured = build_status_structured(
        large_output_collector,
        cursor=0,
        raw_events=True,
        detail="verbose",
    )

    assert "new_events" in structured
    output_event = next(
        event
        for event in structured["new_events"]
        if event["method"] == "item/commandExecution/outputDelta"
    )
    assert output_event["params"]["delta_truncated"] is True
    assert len(output_event["params"]["delta"]) < output_event["params"]["delta_original_len"]


def test_build_result_structured_compact_truncates_command_output(
    large_output_collector: EventCollector,
) -> None:
    structured = build_result_structured(
        large_output_collector,
        detail="compact",
    )
    execution = structured["final_result"]["command_executions"][0]

    assert execution["output_truncated"] is True
    assert execution["output_original_len"] > len(execution["output"])


def test_build_result_structured_projects_raw_events_when_requested(
    large_output_collector: EventCollector,
) -> None:
    structured = build_result_structured(
        large_output_collector,
        detail="compact",
        include_raw_events=True,
    )

    output_event = next(
        event
        for event in structured["raw_events"]
        if event["method"] == "item/commandExecution/outputDelta"
    )
    assert output_event["params"]["delta_truncated"] is True
    assert len(output_event["params"]["delta"]) < output_event["params"]["delta_original_len"]


def test_build_result_structured_full_keeps_full_command_output(
    large_output_collector: EventCollector,
) -> None:
    structured = build_result_structured(
        large_output_collector,
        detail="full",
    )
    execution = structured["final_result"]["command_executions"][0]

    assert execution["output"].startswith("HEAD\n")
    assert execution["output"].endswith("\nTAIL\n")
    assert "\x1b" not in execution["output"]
    assert execution.get("output_truncated") is not True


def test_build_result_structured_raw_keeps_original_ansi(
    large_output_collector: EventCollector,
) -> None:
    collector = large_output_collector
    collector.items["cmd_1"].content_buffer = "\x1b[31mHEAD\x1b[0m\nBODY\nTAIL\n"

    structured = build_result_structured(
        collector,
        detail="raw",
    )
    execution = structured["final_result"]["command_executions"][0]

    assert "\x1b[31m" in execution["output"]
    assert execution.get("output_truncated") is not True


def test_build_error_result_returns_call_tool_result() -> None:
    result = build_error_result("boom", thread_id="thr_err", details={"status": "failed"})

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["success"] is False
    assert result.structuredContent["thread_id"] == "thr_err"
    assert result.content[0].text == "**错误**\nboom"
