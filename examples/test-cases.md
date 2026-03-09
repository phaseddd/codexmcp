# CodexMCP v2.0.0 验收测试用例

> 测试目标项目: `D:\wifi-densepose`
> WiFi DensePose — 基于 WiFi 信号的人体姿态估计系统（Python + Rust + C + JS 多语言项目）

## 设计目标

覆盖 v2.0.0 重构后的 **全部 5 个 MCP 工具**，验证核心架构能力：

- **阻塞模式** — codex 工具端到端执行
- **非阻塞工作流** — codex_start + codex_status 增量轮询
- **任务中断** — codex_start + codex_interrupt
- **会话恢复** — SESSION_ID 多轮对话
- **事件聚合** — return_all_messages 完整事件流
- **容错机制** — 无效路径 / 无效 thread_id

## 用例总览

| # | 场景 | 工具 | sandbox | 测试重点 |
|---|------|------|---------|----------|
| 1 | 阻塞模式 — 项目概览 | `codex` | read-only | 基础端到端：prompt → 聚合结果 |
| 2 | 完整事件流 — 信号处理分析 | `codex` (return_all_messages) | read-only | 事件聚合：events / command_executions / reasoning |
| 3 | 非阻塞轮询 — API 端点搜索 | `codex_start` → `codex_status` | read-only | 非阻塞启动 + 游标增量读取 |
| 4 | 任务中断 | `codex_start` → `codex_interrupt` | read-only | 中断正在执行的任务 |
| 5 | 多轮对话 — 会话恢复 | `codex` × 2 (SESSION_ID) | read-only | thread/resume + 上下文连贯 |
| 6 | 错误路径 — 无效目录 | `codex` | read-only | 容错：无效 cd 路径 |
| 7 | 错误路径 — 无效 thread_id | `codex_status` + `codex_interrupt` | N/A | 工具级容错：不存在的 thread |

> **注**: `codex_approve` 需要 workspace-write 沙箱且触发审批的场景才能测试，暂不列入自动化用例。
> 可在手动测试中使用 `sandbox: "workspace-write"` + 文件写入 prompt 来触发。

---

## 用例 1：阻塞模式 — 项目概览

**场景**: 使用 `codex` 工具（阻塞模式）读取项目顶层结构并给出概括

**测试工具**: `mcp__codex__codex`

**参数**:
```json
{
  "PROMPT": "列出项目的顶层目录结构，简要说明每个目录的用途和使用的编程语言。重点关注 v1/、rust-port/、firmware/、ui/ 四个核心目录",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success` === `true`
- [ ] `SESSION_ID` 非空（thread 创建成功）
- [ ] `agent_messages` 非空且长度 > 50
- [ ] `agent_messages` 提及 `v1`（Python）、`rust-port`（Rust）、`firmware`（C/ESP32）、`ui`（JS）
- [ ] `token_usage` 对象存在且含 `inputTokens` / `outputTokens`

---

## 用例 2：完整事件流 — 信号处理分析

**场景**: 使用 `return_all_messages=True` 获取完整事件流，验证事件聚合能力

**测试工具**: `mcp__codex__codex`

**参数**:
```json
{
  "PROMPT": "阅读 v1/src/core/csi_processor.py，分析 CSIProcessor 类的处理管线：preprocess_csi_data → extract_features → detect_human_presence 三个方法各自做了什么",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only",
  "return_all_messages": true
}
```

**验证点**:
- [ ] `success` === `true`
- [ ] `agent_messages` 描述了 CSI 处理管线（预处理 → 特征提取 → 人体检测）
- [ ] `events` 数组存在且非空（return_all_messages 生效）
- [ ] `events` 中只包含生命周期事件（如 `turn/started`、`item/started`、`item/completed`、`turn/completed`），不包含 delta 碎片
- [ ] `command_executions` 数组存在（Codex 执行了文件读取命令）
- [ ] `token_usage` 包含有效的 token 计数

---

## 用例 3：非阻塞轮询 — API 端点搜索

**场景**: 使用非阻塞模式启动任务，通过 `codex_status` 增量轮询直到完成

**测试工具**: `mcp__codex__codex_start` → `mcp__codex__codex_status` (循环)

### 步骤 3.1：启动任务

**参数** (codex_start):
```json
{
  "PROMPT": "找到所有 FastAPI 路由定义（在 v1/src/api/ 目录下），列出每个 API 端点的 HTTP 方法、路径和功能简述",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**步骤 3.1 验证点**:
- [ ] `success` === `true`
- [ ] `thread_id` 非空
- [ ] `status` === `"running"`
- [ ] 立即返回（耗时 < 5 秒，冷启动含握手时 < 30 秒）

### 步骤 3.2：轮询进度

**参数** (codex_status，首次):
```json
{
  "thread_id": "<步骤 3.1 返回的 thread_id>",
  "cursor": 0
}
```

**轮询策略**: 每 5 秒调用一次 `codex_status`，传入上次返回的 `next_cursor`，直到 `completed` === `true`

**步骤 3.2 验证点**:
- [ ] 每次返回的 `next_cursor` >= 上次传入的 `cursor`（游标单调递增）
- [ ] 默认返回的 `changed_items` / `lifecycle_events` 至少有一类非空
- [ ] 最终 `completed` === `true`
- [ ] `final_result` 存在且 `agent_messages` 非空
- [ ] `final_result.agent_messages` 提及 `health`、`pose`、`stream` 相关路由
- [ ] 轮询次数 >= 2（验证增量读取确实发生）

---

## 用例 4：任务中断

**场景**: 启动一个较复杂的任务后立即中断，验证中断机制

**测试工具**: `mcp__codex__codex_start` → `mcp__codex__codex_interrupt`

### 步骤 4.1：启动任务

**参数** (codex_start):
```json
{
  "PROMPT": "详细分析整个 rust-port/wifi-densepose-rs/ 工作区下所有 16 个 crate 的源码，为每个 crate 写一份包含公开 API、内部实现细节和依赖关系的完整报告",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

> 故意设计为一个超大任务，确保有足够时间窗口执行中断

**步骤 4.1 验证点**:
- [ ] `success` === `true`
- [ ] `thread_id` 非空

### 步骤 4.2：等待 3 秒后中断

**参数** (codex_interrupt):
```json
{
  "thread_id": "<步骤 4.1 返回的 thread_id>"
}
```

**步骤 4.2 验证点**:
- [ ] `success` === `true`
- [ ] `message` 包含 "中断" 相关字样
- [ ] `events_collected` > 0（至少收到了部分事件）
- [ ] 不会崩溃或无限挂起

### 步骤 4.3：确认中断生效

**参数** (codex_status):
```json
{
  "thread_id": "<步骤 4.1 返回的 thread_id>",
  "cursor": 0
}
```

**步骤 4.3 验证点**:
- [ ] `completed` === `true`（中断后 turn 应标记为完成）
- [ ] `next_cursor` > 0（至少收到了部分事件）

---

## 用例 5：多轮对话 — 会话恢复

**场景**: 第一轮获取概览，第二轮通过 SESSION_ID 恢复会话追问细节

**测试工具**: `mcp__codex__codex` (两次调用)

### 第一轮参数:
```json
{
  "PROMPT": "简要描述 firmware/esp32-csi-node/ 目录下的 ESP32 固件代码结构，列出主要源文件和各自的功能",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

### 第二轮参数（使用第一轮返回的 SESSION_ID）:
```json
{
  "PROMPT": "在刚才分析的基础上，详细解释 csi_collector.c 中 ADR-018 二进制帧格式的字段定义，以及 wifi_csi_callback 回调函数是如何序列化 CSI 数据的",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only",
  "SESSION_ID": "<第一轮返回的 SESSION_ID>"
}
```

**验证点**:
- [ ] 第一轮 `success` === `true`，`SESSION_ID` 非空
- [ ] 第一轮 `agent_messages` 提及 `main.c`、`csi_collector`、`stream_sender`
- [ ] 第二轮 `success` === `true`，使用相同 `SESSION_ID`
- [ ] 第二轮 `agent_messages` 提及 ADR-018 格式细节（如 magic number `0xC5110001`、帧头字段）
- [ ] 第二轮回复能引用第一轮上下文（不重复列举所有文件，而是直接深入 csi_collector.c）
- [ ] 两轮的 `token_usage` 各自独立存在

---

## 用例 6：错误路径 — 无效目录

**场景**: 传入不存在的目录路径，验证容错能力

**测试工具**: `mcp__codex__codex`

**参数**:
```json
{
  "PROMPT": "分析这个项目的结构",
  "cd": "D:\\this-path-does-not-exist",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success` === `false`
- [ ] `error` 字段存在且包含有意义的错误信息
- [ ] 不会崩溃或无限挂起
- [ ] 返回耗时 < 60 秒（快速失败，含可能的冷启动握手时间）

---

## 用例 7：错误路径 — 无效 thread_id

**场景**: 对不存在的 thread_id 调用 codex_status 和 codex_interrupt，验证工具级容错

**测试工具**: `mcp__codex__codex_status` + `mcp__codex__codex_interrupt`

### 步骤 7.1：查询不存在的 thread

**参数** (codex_status):
```json
{
  "thread_id": "nonexistent-thread-id-12345",
  "cursor": 0
}
```

**步骤 7.1 验证点**:
- [ ] `success` === `false`
- [ ] `error` 包含 "未找到" 相关信息

### 步骤 7.2：中断不存在的 thread

**参数** (codex_interrupt):
```json
{
  "thread_id": "nonexistent-thread-id-12345"
}
```

**步骤 7.2 验证点**:
- [ ] `success` === `false`
- [ ] `error` 包含 "未找到" 相关信息
- [ ] 不会崩溃
