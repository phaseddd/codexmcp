from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp.tools.base import Tool
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel


async def _run_tool(fn):
    tool = Tool.from_function(fn, structured_output=False)
    return await tool.run({}, convert_result=True)


class EchoModel(BaseModel):
    text: str


async def test_string_return_keeps_real_newlines() -> None:
    def tool() -> str:
        return "a\n\nb"

    result = await _run_tool(tool)

    assert len(result) == 1
    assert result[0].text == "a\n\nb"


async def test_dict_return_is_json_text() -> None:
    def tool() -> dict[str, str]:
        return {"x": "a\n\nb"}

    result = await _run_tool(tool)

    assert len(result) == 1
    assert '"x"' in result[0].text
    assert "\\n\\n" in result[0].text


async def test_text_content_list_is_preserved() -> None:
    def tool() -> list[TextContent]:
        return [
            TextContent(type="text", text="one"),
            TextContent(type="text", text="two"),
        ]

    result = await _run_tool(tool)

    assert [block.text for block in result] == ["one", "two"]


async def test_call_tool_result_is_not_rewrapped() -> None:
    def tool() -> Annotated[CallToolResult, EchoModel]:
        return CallToolResult(
            content=[TextContent(type="text", text="hello")],
            structuredContent={"text": "hello"},
        )

    result = await _run_tool(tool)

    assert isinstance(result, CallToolResult)
    assert result.content[0].text == "hello"
    assert result.structuredContent == {"text": "hello"}
