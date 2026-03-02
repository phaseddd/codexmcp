---
name: test-codexmcp
description: CodexMCP 验收测试执行器。基于预定义用例调用 mcp__codex__codex 工具，记录测试结果到 examples/results/ 目录。用法：/test-codexmcp [all | 1 3 5 | 自定义prompt]
---

# CodexMCP 验收测试 Skill

你是 CodexMCP 的验收测试执行器。按照以下流程执行测试用例并记录结果。

## 输入格式

- `/test-codexmcp` — 显示用例列表，让用户选择要执行哪些
- `/test-codexmcp all` — 执行全部用例
- `/test-codexmcp 1 3 5` — 执行指定编号的用例
- `/test-codexmcp <自定义 PROMPT>` — 以自定义 prompt 执行单次测试（cd 默认为 `D:\wifi-densepose`）

## 执行流程

### 步骤 1：准备

1. 读取用例清单文件 `examples/test-cases.md`，了解所有用例定义
2. 获取当前时间（调用 get-current-datetime agent）
3. 获取当前 git commit hash（`git rev-parse --short HEAD`）
4. 获取 CodexMCP 版本号（读取 `pyproject.toml` 中的 version）

### 步骤 2：执行

对每个选中的用例：

1. 记录开始时间
2. 调用 `mcp__codex__codex` 工具，传入用例定义的参数
3. 记录结束时间，计算耗时
4. 检查返回结果是否符合验证点：
   - `success` 字段是否符合预期
   - `SESSION_ID` 是否正常返回
   - `agent_messages` 是否包含预期内容
5. 判定用例状态：`PASS` / `FAIL` / `SKIP`

**特殊处理 — 用例 4（多轮对话）**:
- 先执行第一轮调用
- 从返回结果中提取 `SESSION_ID`
- 将 `SESSION_ID` 填入第二轮参数中
- 再执行第二轮调用
- 两轮都成功且上下文连贯才算 PASS

### 步骤 3：记录结果

将所有结果写入 `examples/results/YYYYMMDD-HHMMSS.md`，格式如下：

```markdown
# CodexMCP 验收测试结果

## 测试环境
| 项目 | 值 |
|------|-----|
| 执行时间 | YYYY-MM-DD HH:MM:SS |
| CodexMCP 版本 | vX.Y.Z |
| Git Commit | abc1234 |
| 目标项目 | D:\wifi-densepose |
| 操作系统 | Windows 10 / WSL / macOS |

## 测试总结
| 总计 | 通过 | 失败 | 跳过 |
|------|------|------|------|
| N | X | Y | Z |

---

## 用例 1：项目概览
- **状态**: PASS / FAIL
- **耗时**: X.Xs
- **SESSION_ID**: `xxx-xxx-xxx`

### 验证点
- [x] success: true
- [x] agent_messages 非空
- [x] 识别出四大模块
- [x] SESSION_ID 正常返回

### 输出摘要
> （用 1-3 句话概括 agent_messages 的核心内容）

<details>
<summary>完整输出</summary>

（粘贴 agent_messages 完整内容）

</details>

---

（后续用例同上格式）
```

### 步骤 4：汇报

测试执行完毕后：
1. 输出测试总结（通过/失败/跳过数量）
2. 如有失败用例，简要说明失败原因
3. 告知结果文件保存路径

## 注意事项

- 每个 MCP 调用可能耗时 10-60 秒，请耐心等待
- 如果某个用例超时或报错，标记为 FAIL 并继续下一个，不要中断整个测试
- 结果文件中的完整输出用 `<details>` 标签折叠，保持可读性
- 如果目标项目路径不存在，跳过所有依赖该路径的用例（仅执行用例 5 错误路径测试）
