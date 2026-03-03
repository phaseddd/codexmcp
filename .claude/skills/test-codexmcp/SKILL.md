---
name: test-codexmcp
description: CodexMCP v2.0.0 验收测试执行器。覆盖全部 5 个 MCP 工具（codex / codex_start / codex_status / codex_interrupt / codex_approve），验证阻塞模式、非阻塞轮询、任务中断、会话恢复、事件聚合等核心能力。用法：/test-codexmcp [all | 1 3 5 | 自定义prompt]
---

# CodexMCP v2.0.0 验收测试 Skill

你是 CodexMCP 的验收测试执行器。按照以下流程执行测试用例并记录结果。

## 输入格式

- `/test-codexmcp` — 显示用例列表，让用户选择要执行哪些
- `/test-codexmcp all` — 执行全部用例
- `/test-codexmcp 1 3 5` — 执行指定编号的用例
- `/test-codexmcp <自定义 PROMPT>` — 以自定义 prompt 执行单次 codex 调用（cd 默认为 `D:\wifi-densepose`）

## 执行流程

### 步骤 1：准备

1. 读取用例清单文件 `examples/test-cases.md`，了解所有用例定义
2. 获取当前时间（调用 get-current-datetime agent）
3. 获取当前 git commit hash（`git rev-parse --short HEAD`）
4. 获取 CodexMCP 版本号（读取 `pyproject.toml` 中的 version）

### 步骤 2：执行

对每个选中的用例，根据用例类型选择不同的执行策略：

#### 类型 A：阻塞模式用例（用例 1、2、5、6）

1. 记录开始时间
2. 调用 `mcp__codex__codex` 工具，传入用例定义的参数
3. 记录结束时间，计算耗时
4. 逐条检查验证点
5. 判定状态：`PASS` / `FAIL` / `SKIP`

#### 类型 B：非阻塞轮询用例（用例 3）

1. 记录开始时间
2. 调用 `mcp__codex__codex_start` 启动任务
3. 验证启动结果（success、thread_id、status）
4. 进入轮询循环：
   - 初始 cursor = 0
   - 每次调用 `mcp__codex__codex_status`，传入 thread_id 和 cursor
   - 记录返回的 `next_cursor`，下次轮询使用
   - 检查 `completed` 字段
   - 轮询间隔 5 秒（使用 bash `sleep 5`）
   - 最多轮询 60 次（5 分钟超时）
5. 完成后验证 `final_result`
6. 记录结束时间，计算耗时
7. 判定状态

#### 类型 C：任务中断用例（用例 4）

1. 记录开始时间
2. 调用 `mcp__codex__codex_start` 启动超大任务
3. 验证启动结果
4. 等待 3 秒（`sleep 3`），给 Codex 一些执行时间
5. 调用 `mcp__codex__codex_interrupt` 发送中断
6. 验证中断结果
7. 调用 `mcp__codex__codex_status` 确认 turn 已完成
8. 记录结束时间，计算耗时
9. 判定状态

#### 类型 D：多轮对话用例（用例 5）

1. 记录开始时间
2. 执行第一轮 `mcp__codex__codex` 调用
3. 从返回结果中提取 `SESSION_ID`
4. 将 `SESSION_ID` 填入第二轮参数
5. 执行第二轮 `mcp__codex__codex` 调用
6. 验证两轮结果和上下文连贯性
7. 记录结束时间（含两轮总耗时），计算耗时
8. 判定状态

#### 类型 E：错误路径用例（用例 6、7）

1. 记录开始时间
2. 调用对应工具，传入无效参数
3. 验证返回的错误信息
4. 确认不会崩溃或挂起
5. 判定状态

**特殊说明 — 用例 7 包含两个子步骤**:
- 步骤 7.1：调用 `mcp__codex__codex_status` 查询不存在的 thread_id
- 步骤 7.2：调用 `mcp__codex__codex_interrupt` 中断不存在的 thread_id
- 两个子步骤都返回 `success: false` 且不崩溃才算 PASS

### 步骤 3：记录结果

将所有结果写入 `examples/results/YYYYMMDD-HHMMSS.md`，格式如下：

```markdown
# CodexMCP v2.0.0 验收测试结果

## 测试环境
| 项目 | 值 |
|------|-----|
| 执行时间 | YYYY-MM-DD HH:MM:SS |
| CodexMCP 版本 | vX.Y.Z |
| Git Commit | abc1234 |
| 目标项目 | D:\wifi-densepose |
| 操作系统 | Windows 10 Pro |

## 工具覆盖
| 工具 | 测试用例 | 状态 |
|------|----------|------|
| codex | 1, 2, 5, 6 | ✅/❌ |
| codex_start | 3, 4 | ✅/❌ |
| codex_status | 3, 4, 7 | ✅/❌ |
| codex_interrupt | 4, 7 | ✅/❌ |
| codex_approve | (需 workspace-write) | ⏭️ |

## 测试总结
| 总计 | 通过 | 失败 | 跳过 |
|------|------|------|------|
| 7 | X | Y | Z |

---

## 用例 1：阻塞模式 — 项目概览
- **状态**: PASS / FAIL
- **工具**: codex
- **耗时**: X.Xs
- **SESSION_ID**: `xxx-xxx-xxx`

### 验证点
- [x] success === true
- [x] SESSION_ID 非空
- [x] agent_messages 非空且 > 50 字符
- [x] 提及 v1(Python)、rust-port(Rust)、firmware(C)、ui(JS)
- [x] token_usage 存在

### Token 使用
| inputTokens | outputTokens |
|-------------|-------------|
| XXXX | XXXX |

### 输出摘要
> （1-3 句话概括 agent_messages 核心内容）

<details>
<summary>完整输出</summary>

（粘贴 agent_messages 完整内容）

</details>

---

## 用例 3：非阻塞轮询 — API 端点搜索
- **状态**: PASS / FAIL
- **工具**: codex_start → codex_status × N
- **耗时**: X.Xs（含轮询等待）
- **thread_id**: `xxx-xxx-xxx`
- **轮询次数**: N

### 验证点
- [x] codex_start 立即返回 (< 5s)
- [x] thread_id 非空
- [x] status === "running"
- [x] next_cursor 单调递增
- [x] new_events 至少一次非空
- [x] 最终 completed === true
- [x] final_result.agent_messages 提及 health/pose/stream 路由
- [x] 轮询次数 >= 2

### 轮询日志
| 轮次 | cursor → next_cursor | new_events 数量 | completed |
|------|---------------------|----------------|-----------|
| 1 | 0 → X | Y | false |
| 2 | X → Z | W | false |
| ... | ... | ... | ... |
| N | ... → ... | ... | true |

<details>
<summary>final_result 完整输出</summary>

（粘贴 final_result 内容）

</details>

---

## 用例 4：任务中断
- **状态**: PASS / FAIL
- **工具**: codex_start → codex_interrupt → codex_status
- **耗时**: X.Xs
- **thread_id**: `xxx-xxx-xxx`

### 验证点
- [x] codex_start 成功
- [x] codex_interrupt 成功
- [x] 中断后 codex_status.completed === true
- [x] 不崩溃不挂起

<details>
<summary>中断前后事件</summary>

（粘贴中断前后的事件摘要）

</details>

---

（后续用例同上格式，按用例类型调整验证点和输出区块）
```

### 步骤 4：汇报

测试执行完毕后：
1. 输出测试总结（通过/失败/跳过数量）
2. 输出工具覆盖矩阵（哪些工具被测到了）
3. 如有失败用例，简要说明失败原因
4. 告知结果文件保存路径

## 注意事项

- 每个 MCP 调用可能耗时 10-120 秒，请耐心等待
- 如果某个用例超时或报错，标记为 FAIL 并继续下一个，不要中断整个测试
- 结果文件中的完整输出用 `<details>` 标签折叠，保持可读性
- 如果目标项目路径 `D:\wifi-densepose` 不存在，跳过用例 1-5 和 7，仅执行用例 6（错误路径）
- 非阻塞轮询用例（用例 3）最多轮询 60 次，超过则标记为 FAIL（疑似超时）
- 中断用例（用例 4）在启动后等待 3 秒再发送中断，确保有事件产生
- 用例 7 的两个子步骤使用同一个假的 thread_id：`nonexistent-thread-id-12345`
