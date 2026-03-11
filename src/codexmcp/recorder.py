"""测试记录器：记录 MCP 工具完整输出到本地文件。

通过环境变量 CODEXMCP_RECORD=1 启用，记录文件保存在项目根目录的 test-dumps/ 下。
每次工具调用生成一个 JSON 文件，包含：
- content_blocks: 所有 Markdown 文本块（output.py 渲染的最终输出）
- structured_data_full: slim 前的原始结构化 JSON（完整，不受截断影响）

文件命名格式：{timestamp}_{tool_name}.json
"""

from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

logger = logging.getLogger(__name__)

# 记录目录：项目根目录下的 test-dumps/
RECORD_DIR = Path(__file__).resolve().parent.parent.parent / "test-dumps"

# 是否启用记录（通过环境变量控制）
ENABLED = os.environ.get("CODEXMCP_RECORD", "").lower() in ("1", "true", "yes")

# 暂存区：build_call_tool_result() slim 前的原始结构化数据
_last_structured: dict[str, Any] | None = None


def stash_pre_slim(data: dict[str, Any] | None) -> None:
    """在 build_call_tool_result 执行 slim 前暂存原始结构化数据。

    由 output.py 的 build_call_tool_result() 调用，
    在 _slim_for_json_block() 之前保存完整的结构化数据引用。
    slim 使用 deepcopy 不会修改原始数据，所以直接引用即可。
    """
    global _last_structured
    if ENABLED and data is not None:
        _last_structured = data


def _pop_pre_slim() -> dict[str, Any] | None:
    """取出并清空暂存的原始结构化数据。"""
    global _last_structured
    val = _last_structured
    _last_structured = None
    return val


def record_tool_output(tool_name: str, result: CallToolResult) -> None:
    """将完整工具输出记录到 JSON 文件。

    记录内容：
    - timestamp: 调用时间
    - tool: 工具名称
    - is_error: 是否为错误结果
    - content_blocks: output.py 渲染的所有 Markdown 文本块
    - structured_data_full: slim 前的原始结构化 JSON 数据
    """
    if not ENABLED:
        return

    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{ts}_{tool_name}.json"

        # 提取所有文本内容块（output.py 渲染的 Markdown 输出）
        content_texts: list[str] = []
        for block in result.content or []:
            if hasattr(block, "text"):
                content_texts.append(block.text)

        # 取出 slim 前的原始结构化数据
        pre_slim = _pop_pre_slim()

        record: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "tool": tool_name,
            "is_error": result.isError or False,
            "content_block_count": len(content_texts),
            "content_blocks": content_texts,
        }
        if pre_slim is not None:
            record["structured_data_full"] = pre_slim

        filepath = RECORD_DIR / filename
        filepath.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("工具输出已记录: %s", filepath.name)
    except Exception:
        logger.warning("记录工具输出失败", exc_info=True)


def record_result(tool_name: str):
    """装饰器：自动记录 MCP 工具的返回结果。

    用法：
        @mcp.tool(name="codex", ...)
        @record_result("codex")
        async def codex(...):
            ...

    装饰器放在 @mcp.tool 之后（更靠近函数定义），
    这样 FastMCP 的签名检查能正确解析参数。
    functools.wraps 保证 __wrapped__ 传递，inspect.signature() 可追溯原始签名。
    """

    def decorator(func):  # type: ignore[type-arg]
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            if isinstance(result, CallToolResult):
                record_tool_output(tool_name, result)
            return result

        return wrapper

    return decorator
