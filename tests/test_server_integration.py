from __future__ import annotations

from pathlib import Path

from mcp.types import CallToolResult

from codexmcp.server import codex, codex_result, codex_status


async def test_codex_status_returns_call_tool_result_compact(
    monkeypatch,
    completed_collector,
    fake_bridge_factory,
) -> None:
    monkeypatch.setattr(
        "codexmcp.server.get_bridge",
        lambda: fake_bridge_factory(completed_collector),
    )

    result = await codex_status("thr_completed")

    assert isinstance(result, CallToolResult)
    assert result.structuredContent["success"] is True
    assert result.structuredContent["status"] == "completed"
    changed_by_id = {
        item["id"]: item for item in result.structuredContent["changed_items"]
    }
    assert "content" not in changed_by_id["cmd_1"]
    assert "delta" not in changed_by_id["msg_1"]


async def test_codex_result_compact_returns_agent_text_and_truncated_output(
    monkeypatch,
    large_output_collector,
    fake_bridge_factory,
) -> None:
    monkeypatch.setattr(
        "codexmcp.server.get_bridge",
        lambda: fake_bridge_factory(large_output_collector),
    )

    result = await codex_result("thr_large", detail="compact")

    assert isinstance(result, CallToolResult)
    assert result.content[0].text == "最终说明"
    execution = result.structuredContent["final_result"]["command_executions"][0]
    assert execution["output_truncated"] is True


async def test_codex_result_full_returns_full_structured_result(
    monkeypatch,
    large_output_collector,
    fake_bridge_factory,
) -> None:
    monkeypatch.setattr(
        "codexmcp.server.get_bridge",
        lambda: fake_bridge_factory(large_output_collector),
    )

    result = await codex_result("thr_large", detail="full")

    assert isinstance(result, CallToolResult)
    execution = result.structuredContent["final_result"]["command_executions"][0]
    assert execution["output"].startswith("HEAD\n")
    assert execution["output"].endswith("\nTAIL\n")


async def test_codex_result_rejects_running_thread(
    monkeypatch,
    running_collector,
    fake_bridge_factory,
) -> None:
    monkeypatch.setattr(
        "codexmcp.server.get_bridge",
        lambda: fake_bridge_factory(running_collector),
    )

    result = await codex_result("thr_running")

    assert result.isError is True
    assert result.structuredContent["success"] is False
    assert "thread 尚未完成" in result.content[0].text


async def test_codex_blocking_matches_compact_result(
    monkeypatch,
    fake_context,
    blocking_bridge_factory,
) -> None:
    bridge = blocking_bridge_factory(mode="completed")
    monkeypatch.setattr("codexmcp.server.get_bridge", lambda: bridge)

    blocking_result = await codex(
        PROMPT="hello",
        cd=Path("D:/codexmcp"),
        ctx=fake_context,
    )
    final_result = await codex_result("thr_blocking", detail="compact")

    assert isinstance(blocking_result, CallToolResult)
    assert isinstance(final_result, CallToolResult)
    assert (
        blocking_result.structuredContent["final_result"]
        == final_result.structuredContent["final_result"]
    )


async def test_codex_error_path_returns_call_tool_result(
    monkeypatch,
    fake_context,
    ready_bridge_factory,
) -> None:
    monkeypatch.setattr(
        "codexmcp.server.get_bridge",
        lambda: ready_bridge_factory(),
    )

    result = await codex(
        PROMPT="hello",
        cd=Path("D:/__missing_codexmcp__"),
        ctx=fake_context,
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["success"] is False


async def test_codex_transport_lost_returns_current_aggregate(
    monkeypatch,
    fake_context,
    blocking_bridge_factory,
) -> None:
    bridge = blocking_bridge_factory(mode="transport_lost")
    monkeypatch.setattr("codexmcp.server.get_bridge", lambda: bridge)

    result = await codex(
        PROMPT="hello",
        cd=Path("D:/codexmcp"),
        ctx=fake_context,
    )

    assert isinstance(result, CallToolResult)
    assert result.structuredContent["status"] == "transport_lost"
    assert result.structuredContent["transport"]["disconnected"] is True
    assert result.content[0].text == "partial"
