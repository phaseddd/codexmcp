# CodexMCP 验收测试用例

> 测试目标项目: `D:\wifi-densepose`
> WiFi DensePose — 基于 WiFi 信号的人体姿态估计系统（Python + Rust + C + JS 多语言项目）

## 用例总览

| # | 场景 | sandbox | 要点 |
|---|------|---------|------|
| 1 | 项目概览 | read-only | 基础能力：能否正确读取并概括项目结构 |
| 2 | 单文件代码分析 | read-only | 深度分析：能否理解 CSI 信号处理逻辑 |
| 3 | 跨文件搜索 | read-only | 搜索能力：能否找到分散在多处的 API 端点 |
| 4 | 多轮对话 | read-only | 会话管理：SESSION_ID 是否正确传递，上下文是否连贯 |
| 5 | 错误路径 | read-only | 容错能力：无效路径时返回清晰错误 |
| 6 | 跨语言对比分析 | read-only | 高级能力：能否跨 Python/Rust 比较同一功能的实现 |

---

## 用例 1：项目概览

**场景**: 让 Codex 读取项目顶层结构并给出概括性说明

**参数**:
```json
{
  "PROMPT": "列出项目的顶层目录结构，简要说明每个目录的用途和使用的编程语言",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success: true`
- [ ] `agent_messages` 非空
- [ ] 能识别出 `v1/`(Python)、`rust-port/`(Rust)、`firmware/`(C)、`ui/`(JS) 四大模块
- [ ] `SESSION_ID` 正常返回

---

## 用例 2：单文件代码分析

**场景**: 对一个核心 Python 文件进行深度分析

**参数**:
```json
{
  "PROMPT": "阅读 v1/src/core/csi_processor.py，分析 CSI 信号处理的核心流程，包括数据输入格式、处理步骤和输出结果",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success: true`
- [ ] 能描述 CSI 数据的处理管线（采集 → 清洗 → 特征提取）
- [ ] 提及关键方法名和处理逻辑

---

## 用例 3：跨文件搜索

**场景**: 在多个文件中搜索特定模式

**参数**:
```json
{
  "PROMPT": "找到所有 FastAPI 路由定义，列出每个 API 端点的 HTTP 方法、路径和功能描述",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success: true`
- [ ] 找到 `health`、`pose`、`stream` 相关路由
- [ ] 列出具体的端点路径（如 `/api/v1/pose`）

---

## 用例 4：多轮对话

**场景**: 第一轮获取概览，第二轮用 SESSION_ID 在同一上下文中追问细节

**第一轮参数**:
```json
{
  "PROMPT": "简要描述这个项目的 Rust 部分有哪些 crate，各自的职责是什么",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**第二轮参数**（使用第一轮返回的 SESSION_ID）:
```json
{
  "PROMPT": "在刚才分析的基础上，详细说明 wifi-densepose-signal crate 中的 FFT 频谱分析是如何实现呼吸检测的",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only",
  "SESSION_ID": "<第一轮返回的 SESSION_ID>"
}
```

**验证点**:
- [ ] 第一轮 `success: true`，返回 `SESSION_ID`
- [ ] 第二轮 `success: true`，使用相同 `SESSION_ID`
- [ ] 第二轮回复能引用第一轮的上下文（如 "刚才提到的..."、不重复列举所有 crate）

---

## 用例 5：错误路径

**场景**: 传入不存在的目录路径

**参数**:
```json
{
  "PROMPT": "分析这个项目的结构",
  "cd": "D:\\this-path-does-not-exist",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success: false`
- [ ] `error` 字段包含有意义的错误信息
- [ ] 不会崩溃或无限挂起

---

## 用例 6：跨语言对比分析

**场景**: 让 Codex 跨越 Python 和 Rust 两种语言对比同一功能的实现

**参数**:
```json
{
  "PROMPT": "对比 Python 版本 v1/src/core/csi_processor.py 和 Rust 版本 rust-port/wifi-densepose-rs/crates/wifi-densepose-signal/ 中信号处理的实现差异，从算法、性能设计、错误处理三个维度进行分析",
  "cd": "D:\\wifi-densepose",
  "sandbox": "read-only"
}
```

**验证点**:
- [ ] `success: true`
- [ ] 从算法、性能、错误处理三个维度给出对比
- [ ] 能指出具体的代码差异（而非泛泛而谈）
