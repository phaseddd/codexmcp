"""进程工具模块：命令构建、信号处理、父进程监控。

从原 server.py 迁移并改造 _build_popen_cmd()，
新增 app-server 启动命令构建和 MCP 主进程健壮性工具。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import threading
import time
from typing import Callable

from codexmcp.compat import IS_WINDOWS

logger = logging.getLogger(__name__)


def build_app_server_cmd(
    *,
    config: dict[str, str] | None = None,
    profile: str = "",
    yolo: bool = False,
) -> list[str]:
    """构建 codex app-server 启动命令列表。

    在 Windows 上，npm 全局安装的包会生成 .ps1 和 .cmd 两种 shim 脚本。
    本函数按以下优先级解析执行策略：
        1. pwsh       + codex.ps1  （PowerShell 7，推荐）
        2. powershell + codex.ps1  （Windows PowerShell 5.1）
        3. codex.cmd               （通过 CreateProcessW 直接执行）

    在 Unix/macOS 上，直接通过 shutil.which() 解析路径并执行。

    注意：使用 -NoLogo 而非 -NoProfile，以保留用户的 Profile 配置
    （如 UTF-8 编码设置、代理设置等）。

    Args:
        config: 可选的 --config key=value 参数字典
        profile: 可选的 --profile 参数（配置 profile 名称）
        yolo: 是否启用 --yolo 模式（跳过所有审批）

    Returns:
        可直接传给 asyncio.create_subprocess_exec() 的命令列表
    """
    codex_path = shutil.which("codex") or "codex"

    # 基础命令：codex app-server --listen stdio://
    base_args = ["app-server", "--listen", "stdio://"]

    # 追加可选参数
    if profile:
        base_args.extend(["--profile", profile])
    if yolo:
        base_args.append("--yolo")
    if config:
        for key, value in config.items():
            base_args.extend(["--config", f"{key}={value}"])

    if not IS_WINDOWS:
        # Unix/macOS：直接执行
        return [codex_path] + base_args

    # --- Windows：按优先级解析 shell ---
    base, ext = os.path.splitext(codex_path)
    ext_lower = ext.lower()

    # 第 1 步：尝试 .ps1 + PowerShell（首选路径）
    ps1_path = codex_path if ext_lower == ".ps1" else base + ".ps1"
    if os.path.isfile(ps1_path):
        ps_shell = shutil.which("pwsh") or shutil.which("powershell")
        if ps_shell:
            return [ps_shell, "-NoLogo", "-File", ps1_path] + base_args

    # 第 2 步：回退到 .cmd（Windows 原生 CreateProcessW 可直接处理）
    cmd_path = codex_path if ext_lower == ".cmd" else base + ".cmd"
    if os.path.isfile(cmd_path):
        return [cmd_path] + base_args

    # 第 3 步：兜底方案 — 使用 shutil.which 找到的任何路径
    return [codex_path] + base_args


def setup_signal_handlers(shutdown_callback: Callable[[], None]) -> None:
    """设置信号处理器，实现优雅关闭。

    仅在主线程中注册信号处理器。
    SIGINT 始终注册，SIGTERM 仅在非 Windows 平台注册。

    Args:
        shutdown_callback: 收到关闭信号时调用的回调函数
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def handle_shutdown(signum: int, frame: object) -> None:
        """信号处理：触发优雅关闭。"""
        logger.info(f"收到信号 {signum}，开始优雅关闭...")
        shutdown_callback()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, handle_shutdown)


def start_parent_monitor() -> None:
    """启动 Windows 父进程监控守护线程。

    通过 Windows API 周期性检查父进程是否存活，
    父进程退出后自动终止当前进程，防止孤儿进程。

    仅在 Windows 平台生效。非 Windows 平台静默返回。
    """
    if not IS_WINDOWS:
        return

    import ctypes

    parent_pid = os.getppid()

    def is_parent_alive(pid: int) -> bool:
        """通过 Windows API 检查指定进程是否存活。"""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True  # 无法访问则假定存活（安全策略）
        exit_code = ctypes.c_ulong()
        result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(result and exit_code.value == STILL_ACTIVE)

    def monitor_parent() -> None:
        """守护线程：周期性检查父进程存活状态。"""
        while True:
            if not is_parent_alive(parent_pid):
                logger.warning(f"父进程 {parent_pid} 已退出，终止当前进程")
                os._exit(0)
            time.sleep(2)

    thread = threading.Thread(target=monitor_parent, daemon=True, name="parent-monitor")
    thread.start()
    logger.debug(f"父进程监控已启动 (parent_pid={parent_pid})")
