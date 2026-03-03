"""CodexMCP 服务器的控制台入口点。

配置日志系统，初始化信号处理和父进程监控，
然后启动 FastMCP 服务器。
"""

import logging

from codexmcp.process import setup_signal_handlers, start_parent_monitor
from codexmcp.server import run


def main() -> None:
    """启动 CodexMCP 服务器。

    初始化步骤：
    1. 配置日志（INFO 级别，输出到 stderr 避免干扰 stdio 传输）
    2. 注册信号处理器（SIGINT/SIGTERM → 优雅关闭）
    3. 启动 Windows 父进程监控（防止孤儿进程）
    4. 启动 FastMCP 服务器
    """
    # 配置日志：输出到 stderr，避免干扰 MCP stdio 传输
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=__import__("sys").stderr,
    )

    # 信号处理：收到终止信号时优雅关闭 app-server
    def shutdown_callback() -> None:
        from codexmcp.bridge import get_bridge

        bridge = get_bridge()
        if bridge._process and bridge._process.returncode is None:
            bridge._process.terminate()
            try:
                bridge._process.wait(timeout=5)
            except Exception:
                bridge._process.kill()

    setup_signal_handlers(shutdown_callback)

    # Windows 父进程监控（防止孤儿进程）
    start_parent_monitor()

    # 启动 MCP 服务器
    run()


if __name__ == "__main__":
    main()
