"""CodexMCP 的 FastMCP 服务器实现。

通过 MCP 协议桥接 Claude Code 和 Codex CLI，
封装 `codex exec` 命令，提供会话管理、多轮对话和 JSON 流式输出能力。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Dict, Generator, List, Literal, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator, Field
import shutil

# 初始化 FastMCP 服务器实例
mcp = FastMCP("Codex MCP Server-from guda.studio")


def _empty_str_to_none(value: str | None) -> str | None:
    """将空字符串转换为 None，用于可选的 UUID 参数。"""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _build_popen_cmd(cmd: list[str]) -> list[str]:
    """构建子进程命令列表，根据当前平台解析 codex 可执行文件路径。

    在 Windows 上，npm 全局安装的包会生成 .ps1 和 .cmd 两种 shim 脚本。
    本函数按以下优先级解析执行策略：
        1. pwsh       + codex.ps1  （PowerShell 7，推荐）
        2. powershell + codex.ps1  （Windows PowerShell 5.1）
        3. codex.cmd               （通过 CreateProcessW 直接执行）

    在 Unix/macOS 上，直接通过 shutil.which() 解析路径并执行。

    注意：使用 -NoLogo 而非 -NoProfile，以保留用户的 Profile 配置
    （如 UTF-8 编码设置 [Console]::OutputEncoding、代理设置等）。
    使用 -NoProfile 会跳过这些配置，可能导致中文 Windows 系统出现 GBK 编码问题。

    Args:
        cmd: 原始命令列表（如 ["codex", "exec", ...]）

    Returns:
        解析后可直接传给 subprocess.Popen(shell=False) 的命令列表
    """
    popen_cmd = cmd.copy()
    codex_path = shutil.which('codex') or cmd[0]

    if os.name != 'nt':
        # Unix/macOS：直接执行，无需额外处理
        popen_cmd[0] = codex_path
        return popen_cmd

    # --- Windows：按优先级解析 shell ---
    base, ext = os.path.splitext(codex_path)
    ext_lower = ext.lower()

    # 第 1 步：尝试 .ps1 + PowerShell（首选路径）
    ps1_path = codex_path if ext_lower == '.ps1' else base + '.ps1'
    if os.path.isfile(ps1_path):
        ps_shell = shutil.which('pwsh') or shutil.which('powershell')
        if ps_shell:
            return [ps_shell, '-NoLogo', '-File', ps1_path] + cmd[1:]

    # 第 2 步：回退到 .cmd（Windows 原生 CreateProcessW 可直接处理）
    cmd_path = codex_path if ext_lower == '.cmd' else base + '.cmd'
    if os.path.isfile(cmd_path):
        popen_cmd[0] = cmd_path
        return popen_cmd

    # 第 3 步：兜底方案 — 使用 shutil.which 找到的任何路径
    popen_cmd[0] = codex_path
    return popen_cmd


def run_shell_command(cmd: list[str]) -> Generator[str, None, None]:
    """执行命令并逐行流式输出结果。

    通过独立线程读取子进程的 stdout，放入线程安全队列，
    主线程从队列中消费并 yield 每一行输出。
    当检测到 Codex 输出 `turn.completed` 类型的 JSON 时，
    延迟 0.3 秒后优雅终止子进程。

    Args:
        cmd: 命令及参数列表（如 ["codex", "exec", "prompt"]）

    Yields:
        命令输出的每一行文本
    """
    # 构建实际执行命令，处理 Windows 上 .ps1/.cmd shim 的解析
    # shell 优先级：pwsh > powershell > cmd
    popen_cmd = _build_popen_cmd(cmd)

    # 创建子进程，禁用 shell=True 防止命令注入
    process = subprocess.Popen(
        popen_cmd,
        shell=False,
        stdin=subprocess.DEVNULL,      # 不接受标准输入
        stdout=subprocess.PIPE,        # 捕获标准输出
        stderr=subprocess.STDOUT,      # 将标准错误合并到标准输出
        universal_newlines=True,
        encoding='utf-8',
    )

    # 线程安全的输出队列，None 作为结束信号
    output_queue: queue.Queue[str | None] = queue.Queue()
    # 检测到 turn.completed 后的优雅关闭延迟（秒）
    GRACEFUL_SHUTDOWN_DELAY = 0.3

    def is_turn_completed(line: str) -> bool:
        """通过解析 JSON 检查当前行是否表示回合完成。"""
        try:
            data = json.loads(line)
            return data.get("type") == "turn.completed"
        except (json.JSONDecodeError, AttributeError, TypeError):
            return False

    def read_output() -> None:
        """在独立线程中读取子进程输出，避免阻塞主线程。"""
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                stripped = line.strip()
                output_queue.put(stripped)
                # 检测到回合完成，延迟后终止子进程
                if is_turn_completed(stripped):
                    time.sleep(GRACEFUL_SHUTDOWN_DELAY)
                    process.terminate()
                    break
            process.stdout.close()
        # 发送结束信号
        output_queue.put(None)

    # 启动输出读取线程
    thread = threading.Thread(target=read_output)
    thread.start()

    # 主线程从队列中消费输出行
    while True:
        try:
            line = output_queue.get(timeout=0.5)
            if line is None:
                break
            yield line
        except queue.Empty:
            # 队列为空时检查子进程和线程是否都已结束
            if process.poll() is not None and not thread.is_alive():
                break

    # 等待子进程结束，超时则强制终止
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    # 等待读取线程结束
    thread.join(timeout=5)

    # 清空队列中残余的输出行
    while not output_queue.empty():
        try:
            line = output_queue.get_nowait()
            if line is not None:
                yield line
        except queue.Empty:
            break


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
    result = prompt.replace('\\', '\\\\')
    # 双引号转义，防止字符串边界混乱
    result = result.replace('"', '\\"')
    # 换行符（Windows 常用 \\r\\n，分开转义）
    result = result.replace('\n', '\\n')
    result = result.replace('\r', '\\r')
    # 制表符
    result = result.replace('\t', '\\t')
    # 退格符和换页符
    result = result.replace('\b', '\\b')
    result = result.replace('\f', '\\f')
    # 单引号（Windows 命令行虽不严格要求，但保险起见也转义）
    result = result.replace("'", "\\'")

    return result


@mcp.tool(
    name="codex",
    description="""
    Executes a non-interactive Codex session via CLI to perform AI-assisted coding tasks in a secure workspace.
    This tool wraps the `codex exec` command, enabling model-driven code generation, debugging, or automation based on natural language prompts.
    It supports resuming ongoing sessions for continuity and enforces sandbox policies to prevent unsafe operations. Ideal for integrating Codex into MCP servers for agentic workflows, such as code reviews or repo modifications.

    **Key Features:**
        - **Prompt-Driven Execution:** Send task instructions to Codex for step-by-step code handling.
        - **Workspace Isolation:** Operate within a specified directory, with optional Git repo skipping.
        - **Security Controls:** Three sandbox levels balance functionality and safety.
        - **Session Persistence:** Resume prior conversations via `SESSION_ID` for iterative tasks.

    **Edge Cases & Best Practices:**
        - Ensure `cd` exists and is accessible; tool fails silently on invalid paths.
        - For most repos, prefer "read-only" to avoid accidental changes.
        - If needed, set `return_all_messages` to `True` to parse "all_messages" for detailed tracing (e.g., reasoning, tool calls, etc.).
    """,
    meta={"version": "0.0.0", "author": "guda.studio"},
)
async def codex(
    PROMPT: Annotated[str, "Instruction for the task to send to codex."],
    cd: Annotated[Path, "Set the workspace root for codex before executing the task."],
    sandbox: Annotated[
        Literal["read-only", "workspace-write", "danger-full-access"],
        Field(
            description="Sandbox policy for model-generated commands. Defaults to `read-only`."
        ),
    ] = "read-only",
    SESSION_ID: Annotated[
        str,
        "Resume the specified session of the codex. Defaults to `None`, start a new session.",
    ] = "",
    skip_git_repo_check: Annotated[
        bool,
        "Allow codex running outside a Git repository (useful for one-off directories).",
    ] = True,
    return_all_messages: Annotated[
        bool,
        "Return all messages (e.g. reasoning, tool calls, etc.) from the codex session. Set to `False` by default, only the agent's final reply message is returned.",
    ] = False,
    image: Annotated[
        List[Path],
        Field(
            description="Attach one or more image files to the initial prompt. Separate multiple paths with commas or repeat the flag.",
        ),
    ] = [],
    model: Annotated[
        str,
        Field(
            description="The model to use for the codex session. This parameter is strictly prohibited unless explicitly specified by the user.",
        ),
    ] = "",
    yolo: Annotated[
        bool,
        Field(
            description="Run every command without approvals or sandboxing. Only use when `sandbox` couldn't be applied.",
        ),
    ] = False,
    profile: Annotated[
        str,
        "Configuration profile name to load from `~/.codex/config.toml`. This parameter is strictly prohibited unless explicitly specified by the user.",
    ] = "",
) -> Dict[str, Any]:
    """执行 Codex CLI 会话并返回结果。

    构建命令行参数 → 启动子进程 → 逐行解析 JSON 流 → 提取 agent 消息和会话 ID → 返回结构化结果。
    """
    # 构建命令列表，避免 shell 注入
    cmd = ["codex", "exec", "--sandbox", sandbox, "--cd", str(cd), "--json"]

    # 附加图片参数
    if len(image):
        cmd.extend(["--image", ",".join(str(p) for p in image)])

    # 指定模型（仅在用户明确指定时）
    if model:
        cmd.extend(["--model", model])

    # 指定配置 profile（仅在用户明确指定时）
    if profile:
        cmd.extend(["--profile", profile])

    # YOLO 模式：跳过所有审批和沙箱限制
    if yolo:
        cmd.append("--yolo")

    # 跳过 Git 仓库检查（允许在非 Git 目录下运行）
    if skip_git_repo_check:
        cmd.append("--skip-git-repo-check")

    # 恢复已有会话
    if SESSION_ID:
        cmd.extend(["resume", str(SESSION_ID)])

    # Windows 平台需要对 Prompt 中的特殊字符进行转义
    if os.name == "nt":
        PROMPT = windows_escape(PROMPT)
    cmd += ['--', PROMPT]

    # 初始化结果收集变量
    all_messages: list[Dict[str, Any]] = []  # 所有 JSON 消息
    agent_messages = ""                       # agent 的最终回复文本
    success = True                            # 执行是否成功
    err_message = ""                          # 错误信息累积
    thread_id: Optional[str] = None           # Codex 会话 ID（用于多轮对话）

    # 逐行解析 Codex CLI 的 JSON 流输出
    for line in run_shell_command(cmd):
        try:
            line_dict = json.loads(line.strip())
            all_messages.append(line_dict)

            # 提取 agent 消息文本
            item = line_dict.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                agent_messages = agent_messages + item.get("text", "")

            # 提取会话 ID（thread_id），用于后续 resume
            if line_dict.get("thread_id") is not None:
                thread_id = line_dict.get("thread_id")

            # 处理失败类型的消息
            if "fail" in line_dict.get("type", ""):
                success = False if len(agent_messages) == 0 else success
                err_message += "\n\n[codex error] " + line_dict.get("error", {}).get("message", "")

            # 处理错误类型的消息（过滤掉重连消息）
            if "error" in line_dict.get("type", ""):
                error_msg = line_dict.get("message", "")
                is_reconnecting = bool(re.match(r'^Reconnecting\.\.\.\s+\d+/\d+', error_msg))

                if not is_reconnecting:
                    success = False if len(agent_messages) == 0 else success
                    err_message += "\n\n[codex error] " + error_msg

        except json.JSONDecodeError:
            # 非 JSON 格式的行，记录错误但继续处理
            err_message += "\n\n[json decode error] " + line
            continue

        except Exception as error:
            # 意外异常，记录并中止解析
            err_message += "\n\n[unexpected error] " + f"Unexpected error: {error}. Line: {line!r}"
            success = False
            break

    # 校验必要字段：会话 ID
    if thread_id is None:
        success = False
        err_message = "Failed to get `SESSION_ID` from the codex session. \n\n" + err_message

    # 校验必要字段：agent 回复消息
    if len(agent_messages) == 0:
        success = False
        err_message = "Failed to get `agent_messages` from the codex session. \n\n You can try to set `return_all_messages` to `True` to get the full reasoning information. " + err_message

    # 构建返回结果
    if success:
        result: Dict[str, Any] = {
            "success": True,
            "SESSION_ID": thread_id,
            "agent_messages": agent_messages,
        }
    else:
        result = {"success": False, "error": err_message}

    # 按需附加完整消息列表（用于调试和追踪）
    if return_all_messages:
        result["all_messages"] = all_messages

    return result


def run() -> None:
    """通过 stdio 传输启动 MCP 服务器。"""
    mcp.run(transport="stdio")
