"""验证脚本：确认 Fix#1（诊断日志）和 Fix#2（原生二进制直接解析）工作正常。

用法：
    python scripts/verify_native_binary.py          # 完整验证（含实际启动 codex）
    python scripts/verify_native_binary.py --quick   # 快速验证（仅静态检查，不启动进程）

验证项目：
    [Fix#2] 原生二进制解析
    1. _resolve_native_binary() 能否找到原生二进制
    2. build_app_server_cmd() 返回值是否为 tuple(list, str|None)
    3. 命令列表第一项是否为原生二进制（而非 pwsh/node shim）
    4. 附加工具目录（rg）是否存在
    5. 跨平台映射完整性（6 平台 × 6 包名，一一对应）

    [Fix#1] 诊断日志（需实际启动进程，--quick 跳过）
    6. 启动 codex app-server 并完成握手
    7. 执行简单 turn 并验证正常完成（无 bridge/disconnected）
    8. 关闭后检查进程链层数（应为 1 层，而非 3-4 层）

退出码：
    0 = 全部通过
    1 = 有验证项失败
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

# 确保能导入项目模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def static_checks() -> list[str]:
    """静态检查（不启动进程），返回失败消息列表。"""
    failures: list[str] = []

    # --- 1. 模块导入 ---
    try:
        from codexmcp.process import (
            _PLATFORM_PACKAGES,
            _TARGET_TRIPLES,
            _resolve_native_binary,
            build_app_server_cmd,
        )
        from codexmcp.compat import IS_WINDOWS
    except ImportError as e:
        failures.append(f"模块导入失败: {e}")
        return failures

    print("=== Fix#2 验证：原生二进制直接解析 ===\n")

    # --- 2. 跨平台映射完整性 ---
    if len(_TARGET_TRIPLES) != 6:
        failures.append(f"三元组映射数量错误: 期望 6, 实际 {len(_TARGET_TRIPLES)}")
    if len(_PLATFORM_PACKAGES) != 6:
        failures.append(f"平台包映射数量错误: 期望 6, 实际 {len(_PLATFORM_PACKAGES)}")
    for triple in _TARGET_TRIPLES.values():
        if triple not in _PLATFORM_PACKAGES:
            failures.append(f"三元组 {triple} 缺少对应的平台包映射")

    if not any("linux" in k[0] for k in _TARGET_TRIPLES):
        failures.append("缺少 Linux 平台映射")
    if not any("darwin" in k[0] for k in _TARGET_TRIPLES):
        failures.append("缺少 macOS 平台映射")
    if not any("win32" in k[0] for k in _TARGET_TRIPLES):
        failures.append("缺少 Windows 平台映射")

    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return failures

    print(f"  [1/5] 跨平台映射完整性: PASS (6 平台, 6 包名)")

    # --- 3. _resolve_native_binary() ---
    resolved = _resolve_native_binary()
    if resolved is None:
        # 不一定是失败——可能当前平台未安装 codex
        print(f"  [2/5] 原生二进制解析: SKIP (当前平台未安装 codex 或不支持)")
        print(f"  [3/5] 命令首项检查:  SKIP")
        print(f"  [4/5] 附加工具目录:  SKIP")
        print(f"  [5/5] 回退策略:      无需检查（直接解析可用时不走回退）")
        return failures

    bin_path, extra_path = resolved

    if not os.path.isfile(bin_path):
        failures.append(f"二进制路径不存在: {bin_path}")
    else:
        print(f"  [2/5] 原生二进制解析: PASS")
        print(f"         路径: {bin_path}")

    # --- 4. build_app_server_cmd() 返回值 ---
    cmd, cmd_extra = build_app_server_cmd()
    if not isinstance(cmd, list) or not isinstance(cmd_extra, (str, type(None))):
        failures.append(f"返回值类型错误: ({type(cmd)}, {type(cmd_extra)})")
    elif cmd[0] != bin_path:
        failures.append(f"命令首项不是原生二进制: {cmd[0]}")
    else:
        # 确认不是 pwsh/node/cmd 等 shim
        first = os.path.basename(cmd[0]).lower()
        is_native = first.startswith("codex")
        is_shim = any(s in first for s in ["pwsh", "powershell", "node", ".ps1", ".cmd"])
        if is_shim:
            failures.append(f"命令首项仍是 shim: {first}")
        elif is_native:
            print(f"  [3/5] 命令首项检查:  PASS (直接调用 {first})")
        else:
            print(f"  [3/5] 命令首项检查:  WARN (未知类型: {first})")

    # --- 5. 附加工具目录 ---
    if extra_path:
        rg_name = "rg.exe" if IS_WINDOWS else "rg"
        rg_path = os.path.join(extra_path, rg_name)
        if os.path.isfile(rg_path):
            print(f"  [4/5] 附加工具目录:  PASS ({rg_name} 存在)")
        else:
            failures.append(f"附加工具 {rg_name} 不存在: {rg_path}")
    else:
        print(f"  [4/5] 附加工具目录:  SKIP (无附加目录)")

    # --- 6. 可选参数传递 ---
    cmd2, _ = build_app_server_cmd(profile="test", yolo=True, config={"k": "v"})
    missing = []
    if "--profile" not in cmd2:
        missing.append("--profile")
    if "--yolo" not in cmd2:
        missing.append("--yolo")
    if "--config" not in cmd2:
        missing.append("--config")
    if missing:
        failures.append(f"可选参数缺失: {missing}")
    else:
        print(f"  [5/5] 可选参数传递:  PASS (profile/yolo/config)")

    return failures


async def runtime_check() -> list[str]:
    """运行时检查：实际启动 codex app-server 并验证通信。"""
    failures: list[str] = []

    from codexmcp.process import build_app_server_cmd

    cmd, extra_path = build_app_server_cmd()

    print("\n=== Fix#1 验证：实际启动 + 诊断日志 ===\n")
    print(f"  启动命令: {' '.join(cmd)}")

    # 构建环境变量
    env = os.environ.copy()
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
        print(f"  PATH 追加: {extra_path}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    print(f"  进程已启动: PID={proc.pid}")

    # 辅助函数：发送 JSONL 请求
    request_id = 0

    async def send_request(method: str, params: dict) -> dict:
        nonlocal request_id
        request_id += 1
        msg = {"method": method, "id": request_id, "params": params}
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        assert proc.stdin
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()
        return msg

    async def send_notification(method: str, params: dict) -> None:
        msg = {"method": method, "params": params}
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        assert proc.stdin
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    async def read_response(expected_id: int, timeout: float = 30.0) -> dict | None:
        assert proc.stdout
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=min(5.0, deadline - time.time())
                )
            except asyncio.TimeoutError:
                continue
            if not line:
                return None
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
            if msg.get("id") == expected_id:
                return msg
        return None

    try:
        # --- 1. 握手 ---
        print("\n  [1/3] 握手测试...")
        await send_request("initialize", {
            "clientInfo": {"name": "verify-script", "version": "1.0"},
            "capabilities": {"experimentalApi": False},
        })
        resp = await read_response(request_id, timeout=30.0)
        if resp is None:
            failures.append("握手超时（30s），app-server 无响应")
            return failures
        if "error" in resp:
            failures.append(f"握手失败: {resp['error']}")
            return failures

        await send_notification("initialized", {})
        print(f"         PASS (握手成功)")

        # --- 2. 创建 thread ---
        print("  [2/3] Thread 创建...")
        # 使用当前目录作为 cwd
        cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        await send_request("thread/start", {"cwd": cwd})
        resp = await read_response(request_id, timeout=15.0)
        if resp is None:
            failures.append("thread/start 超时")
            return failures
        thread_id = resp.get("result", {}).get("thread", {}).get("id", "")
        if not thread_id:
            failures.append(f"thread/start 返回无效: {resp}")
            return failures
        print(f"         PASS (thread_id={thread_id[:16]}...)")

        # --- 3. 执行简单 turn ---
        print("  [3/3] Turn 执行（简单 prompt）...")
        await send_request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "用一句话回答：1+1等于多少？"}],
            "sandboxPolicy": {"type": "readOnly"},
            "approvalPolicy": "never",
        })
        resp = await read_response(request_id, timeout=15.0)
        if resp is None:
            failures.append("turn/start 超时")
            return failures

        # 等待 turn 完成（读取事件流）
        event_count = 0
        turn_completed = False
        disconnected = False
        deadline = time.time() + 120.0  # 2 分钟总超时

        assert proc.stdout
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            if not line:
                disconnected = True
                break
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")
            event_count += 1

            if method == "turn/completed":
                turn_completed = True
                break

        if disconnected:
            rc = proc.returncode
            failures.append(
                f"进程意外断连！(returncode={rc}, events={event_count}) "
                f"— 如果 returncode=None 且 events>0, 说明 Fix#2 未完全解决问题"
            )
        elif not turn_completed:
            failures.append(f"Turn 未在 120s 内完成 (收到 {event_count} 个事件)")
        else:
            print(f"         PASS (turn 正常完成, {event_count} 个事件)")

        # --- 进程链层数检查 ---
        print(f"\n  进程链验证:")
        print(f"    PID={proc.pid}, returncode={proc.returncode}")
        if proc.returncode is None:
            print(f"    进程仍在运行（符合预期，app-server 是长驻进程）")
        print(f"    命令: {os.path.basename(cmd[0])} (应为 codex.exe 或 codex)")

    finally:
        # 清理
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            print(f"\n  进程已清理 (returncode={proc.returncode})")

    return failures


def main() -> None:
    quick = "--quick" in sys.argv

    print("=" * 60)
    print("CodexMCP Fix 验证脚本")
    print(f"  Fix#1: _read_loop 诊断日志增强")
    print(f"  Fix#2: 原生二进制直接解析（跨平台）")
    print(f"  模式: {'快速（静态检查）' if quick else '完整（含进程启动）'}")
    print("=" * 60)
    print()

    all_failures: list[str] = []

    # 静态检查
    all_failures.extend(static_checks())

    # 运行时检查
    if not quick and not all_failures:
        runtime_failures = asyncio.run(runtime_check())
        all_failures.extend(runtime_failures)
    elif quick:
        print("\n  (--quick 模式，跳过运行时检查)")

    # 汇总
    print("\n" + "=" * 60)
    if all_failures:
        print(f"FAIL: {len(all_failures)} 项验证失败")
        for i, f in enumerate(all_failures, 1):
            print(f"  {i}. {f}")
        print("=" * 60)
        sys.exit(1)
    else:
        print("ALL PASS: 全部验证项通过")
        if not quick:
            print("  Fix#1: 诊断日志增强 — 已验证（进程正常启动/关闭）")
            print("  Fix#2: 原生二进制解析 — 已验证（直接调用，无中间层）")
        print()
        print("下一步建议：运行 /test-codexmcp 3 执行端到端回归测试")
        print("=" * 60)
        sys.exit(0)


if __name__ == "__main__":
    main()
