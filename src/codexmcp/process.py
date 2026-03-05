"""进程工具模块：命令构建、信号处理、父进程监控。

从原 server.py 迁移并改造 _build_popen_cmd()，
新增 app-server 启动命令构建和 MCP 主进程健壮性工具。

v2.1: 新增原生二进制直接解析，跨平台绕过 Node.js/PowerShell 中间层。
"""

from __future__ import annotations

import logging
import os
import platform as plat
import shutil
import signal
import sys
import threading
import time
from typing import Callable

from codexmcp.compat import IS_WINDOWS

logger = logging.getLogger(__name__)


# === 原生二进制直接解析（跨平台） ===

# 机器架构归一化：处理不同系统/运行环境下的别名差异
_MACHINE_ALIASES: dict[str, str] = {
    # x64 常见写法
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86-64": "x86_64",
    # arm64 常见写法
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

# 单一事实表：(sys.platform, normalized_machine) → {triple, package}
# package 为 npm 平台包目录名（不含 @openai/ 前缀）
_PLATFORM_TARGETS: dict[tuple[str, str], dict[str, str]] = {
    # Linux
    ("linux", "x86_64"): {
        "triple": "x86_64-unknown-linux-musl",
        "package": "codex-linux-x64",
    },
    ("linux", "aarch64"): {
        "triple": "aarch64-unknown-linux-musl",
        "package": "codex-linux-arm64",
    },
    # macOS
    ("darwin", "x86_64"): {
        "triple": "x86_64-apple-darwin",
        "package": "codex-darwin-x64",
    },
    ("darwin", "aarch64"): {
        "triple": "aarch64-apple-darwin",
        "package": "codex-darwin-arm64",
    },
    # Windows
    ("win32", "x86_64"): {
        "triple": "x86_64-pc-windows-msvc",
        "package": "codex-win32-x64",
    },
    ("win32", "aarch64"): {
        "triple": "aarch64-pc-windows-msvc",
        "package": "codex-win32-arm64",
    },
}

# 向后兼容：保留旧常量名，实际由单一事实表自动派生，避免双源漂移
_TARGET_TRIPLES: dict[tuple[str, str], str] = {
    key: value["triple"] for key, value in _PLATFORM_TARGETS.items()
}
_PLATFORM_PACKAGES: dict[str, str] = {
    value["triple"]: value["package"] for value in _PLATFORM_TARGETS.values()
}


def _normalize_platform_arch() -> tuple[str, str, str, str]:
    """规范化平台与架构键，降低 platform.machine() 别名差异带来的失配。"""
    raw_platform = sys.platform
    raw_machine = plat.machine()

    platform_key = raw_platform.lower()
    machine_key = raw_machine.strip().lower()
    normalized_machine = _MACHINE_ALIASES.get(machine_key, machine_key)
    return platform_key, normalized_machine, raw_platform, raw_machine


def _resolve_native_binary() -> tuple[str, str | None] | None:
    """尝试直接定位 codex 原生二进制文件，绕过 Node.js/PowerShell 中间层。

    复制 codex.js 的二进制解析逻辑，从 npm 包结构中直接定位
    平台原生二进制（codex.exe / codex），将进程链从 4 层缩减为 1 层。

    支持的安装布局：
    - npm global (Windows): {prefix}/node_modules/@openai/codex/...
    - npm global (Unix):    {prefix}/lib/node_modules/@openai/codex/...
    - symlink 安装 (bun/pnpm): 通过 os.path.realpath() 追溯

    Returns:
        (binary_path, extra_path_dir) 元组：
            - binary_path: 原生二进制的绝对路径
            - extra_path_dir: 附加工具目录（含 rg 等），可为 None
        无法定位时返回 None（调用方应回退到原有策略）
    """
    codex_path = shutil.which("codex")
    if not codex_path:
        return None

    platform_key, machine_key, raw_platform, raw_machine = _normalize_platform_arch()
    target_info = _PLATFORM_TARGETS.get((platform_key, machine_key))
    if not target_info:
        logger.debug(
            "不支持的平台组合: "
            f"{raw_platform}/{raw_machine} (规范化后: {platform_key}/{machine_key})，"
            "跳过原生解析"
        )
        return None

    triple = target_info["triple"]
    platform_pkg = target_info["package"]
    binary_name = "codex.exe" if IS_WINDOWS else "codex"

    shim_dir = os.path.dirname(os.path.abspath(codex_path))

    # 候选 npm 包根目录（覆盖不同包管理器和平台的安装布局）
    pkg_candidates: list[str] = [
        # Windows npm global: {prefix}/node_modules/@openai/codex/
        os.path.join(shim_dir, "node_modules", "@openai", "codex"),
        # Unix npm global: {prefix}/bin/ → {prefix}/lib/node_modules/@openai/codex/
        os.path.join(shim_dir, "..", "lib", "node_modules", "@openai", "codex"),
    ]

    # 通过 symlink/realpath 追溯（Unix 上 bin/codex 通常是 symlink 到 codex.js）
    real_path = os.path.realpath(codex_path)
    if real_path != os.path.abspath(codex_path):
        # symlink 目标通常在 .../bin/codex.js，包根目录 = ../../
        pkg_from_link = os.path.dirname(os.path.dirname(real_path))
        normalized = os.path.normpath(pkg_from_link)
        if normalized not in [os.path.normpath(p) for p in pkg_candidates]:
            pkg_candidates.append(pkg_from_link)

    for pkg_dir in pkg_candidates:
        pkg_dir = os.path.normpath(pkg_dir)
        if not os.path.isdir(pkg_dir):
            continue

        # 二进制位置候选（npm 嵌套 node_modules 和本地 vendor 两种布局）
        binary_candidates = [
            # 嵌套 node_modules（npm 默认）:
            # {pkg}/node_modules/@openai/{platform_pkg}/vendor/{triple}/codex/{binary}
            os.path.join(
                pkg_dir, "node_modules", "@openai", platform_pkg,
                "vendor", triple, "codex", binary_name,
            ),
            # 本地 vendor（开发模式或 flat install）:
            # {pkg}/vendor/{triple}/codex/{binary}
            os.path.join(
                pkg_dir, "vendor", triple, "codex", binary_name,
            ),
        ]

        for bin_path in binary_candidates:
            if not os.path.isfile(bin_path):
                continue

            # 检查附加工具目录（如 rg，位于 {vendor}/{triple}/path/）
            # bin_path = .../vendor/{triple}/codex/{binary}
            # arch_root = .../vendor/{triple}/
            arch_root = os.path.dirname(os.path.dirname(bin_path))
            path_dir = os.path.join(arch_root, "path")
            extra_path = path_dir if os.path.isdir(path_dir) else None

            logger.info(f"直接定位到原生二进制: {bin_path}")
            if extra_path:
                logger.info(f"附加工具目录: {extra_path}")
            return (bin_path, extra_path)

    logger.debug("未能定位原生二进制，将回退到 shim 启动策略")
    return None


def build_app_server_cmd(
    *,
    config: dict[str, str] | None = None,
    profile: str = "",
    yolo: bool = False,
) -> tuple[list[str], str | None]:
    """构建 codex app-server 启动命令列表。

    优先策略（所有平台通用）：
        0. 直接使用原生二进制 codex[.exe]（绕过 Node.js/PowerShell 层，
           消除中间进程导致的管道断连风险）

    回退策略（仅当直接解析失败时）：
    Windows:
        1. pwsh       + codex.ps1  （PowerShell 7）
        2. powershell + codex.ps1  （Windows PowerShell 5.1）
        3. codex.cmd               （通过 CreateProcessW 直接执行）
    Unix/macOS:
        1. 通过 shutil.which() 解析并直接执行

    注意：回退策略中使用 -NoLogo 而非 -NoProfile，以保留用户的 Profile 配置
    （如 UTF-8 编码设置、代理设置等）。

    Args:
        config: 可选的 --config key=value 参数字典
        profile: 可选的 --profile 参数（配置 profile 名称）
        yolo: 是否启用 --yolo 模式（跳过所有审批）

    Returns:
        (cmd, extra_path_dir) 元组：
            - cmd: 可直接传给 asyncio.create_subprocess_exec() 的命令列表
            - extra_path_dir: 需要追加到 subprocess PATH 的目录（含 rg 等），
              可为 None（无需修改环境变量）
    """
    # 基础命令参数：app-server --listen stdio://
    base_args = ["app-server", "--listen", "stdio://"]

    # 追加可选参数
    if profile:
        base_args.extend(["--profile", profile])
    if yolo:
        base_args.append("--yolo")
    if config:
        for key, value in config.items():
            base_args.extend(["--config", f"{key}={value}"])

    # === 首选策略：直接使用原生二进制（所有平台通用） ===
    resolved = _resolve_native_binary()
    if resolved:
        bin_path, extra_path = resolved
        return [bin_path] + base_args, extra_path

    # === 回退策略 ===
    logger.info("回退到 shim 启动策略")
    codex_path = shutil.which("codex") or "codex"

    if not IS_WINDOWS:
        # Unix/macOS：直接执行 shim（通常是 symlink 到 codex.js）
        return [codex_path] + base_args, None

    # --- Windows 回退：按优先级解析 shell ---
    base, ext = os.path.splitext(codex_path)
    ext_lower = ext.lower()

    # 第 1 步：尝试 .ps1 + PowerShell（首选路径）
    ps1_path = codex_path if ext_lower == ".ps1" else base + ".ps1"
    if os.path.isfile(ps1_path):
        ps_shell = shutil.which("pwsh") or shutil.which("powershell")
        if ps_shell:
            return [ps_shell, "-NoLogo", "-File", ps1_path] + base_args, None

    # 第 2 步：回退到 .cmd（Windows 原生 CreateProcessW 可直接处理）
    cmd_path = codex_path if ext_lower == ".cmd" else base + ".cmd"
    if os.path.isfile(cmd_path):
        return [cmd_path] + base_args, None

    # 第 3 步：兜底方案 — 使用 shutil.which 找到的任何路径
    return [codex_path] + base_args, None


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
