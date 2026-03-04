"""自定义异常类型和协议常量定义。

定义与 codex app-server JSON-RPC 通信相关的异常类型，
以及背压重试、超时等协议级常量。
"""

from __future__ import annotations


# === 协议常量 ===

# 背压错误码（app-server 过载时返回）
BACKPRESSURE_ERROR_CODE = -32001

# RPC 请求最大重试次数（背压场景）
MAX_RETRIES = 3

# 单次 RPC 请求超时（秒）
REQUEST_TIMEOUT = 300.0

# 握手超时（秒），新进程初始化不应耗时太久
HANDSHAKE_TIMEOUT = 30.0

# Turn 总超时（秒），防止无限等待
TURN_TOTAL_TIMEOUT = 1800


# === 异常类型 ===


class AppServerError(Exception):
    """app-server 返回的 JSON-RPC 错误。

    封装 JSON-RPC error 对象，提供 code 和 message 属性
    便于调用方按错误码分类处理（如背压重试）。
    """

    def __init__(self, error: dict) -> None:
        self.code: int = error.get("code", -1)
        self.message: str = error.get("message", "Unknown error")
        super().__init__(f"[{self.code}] {self.message}")


class AppServerNotReady(Exception):
    """app-server 进程未就绪或已断开。

    在进程未启动、已崩溃或握手未完成时抛出，
    触发调用方的重连逻辑。
    """

    pass


class TurnTimeoutError(Exception):
    """Turn 执行超时。

    当 Turn 执行时间超过 TURN_TOTAL_TIMEOUT 时抛出，
    通常伴随自动中断操作。
    """

    pass
