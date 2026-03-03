"""平台兼容层：Windows 转义和平台检测。

从原 server.py 迁移 windows_escape() 函数，
并提供统一的 escape_prompt() 接口按平台条件调用。
"""

from __future__ import annotations

import os


# 平台检测常量
IS_WINDOWS: bool = os.name == "nt"


def windows_escape(prompt: str) -> str:
    """Windows 风格的字符串转义函数。

    将常见特殊字符转义成 \\\\ 形式，适合命令行、JSON 或路径场景使用。
    例如：\\n 变成 \\\\n，" 变成 \\\\"。

    Args:
        prompt: 需要转义的原始字符串

    Returns:
        转义后的安全字符串
    """
    # 先处理反斜杠，避免干扰后续替换
    result = prompt.replace("\\", "\\\\")
    # 双引号转义，防止字符串边界混乱
    result = result.replace('"', '\\"')
    # 换行符（Windows 常用 \\r\\n，分开转义）
    result = result.replace("\n", "\\n")
    result = result.replace("\r", "\\r")
    # 制表符
    result = result.replace("\t", "\\t")
    # 退格符和换页符
    result = result.replace("\b", "\\b")
    result = result.replace("\f", "\\f")
    # 单引号（Windows 命令行虽不严格要求，但保险起见也转义）
    result = result.replace("'", "\\'")

    return result


def escape_prompt(prompt: str) -> str:
    """按当前平台条件转义 prompt 字符串。

    在 Windows 上调用 windows_escape() 处理特殊字符，
    在 Unix/macOS 上直接返回原字符串（无需额外转义）。

    Args:
        prompt: 原始 prompt 字符串

    Returns:
        平台安全的 prompt 字符串
    """
    if IS_WINDOWS:
        return windows_escape(prompt)
    return prompt
