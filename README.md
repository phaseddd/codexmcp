# CodexMCP

> **本项目已于 2026-03-31 正式封存。**

OpenAI 发布了官方 Claude Code 插件 [codex-plugin-cc](https://github.com/openai/codex-plugin-cc)，完整覆盖了本项目的核心场景。既然官方亲自下场，社区方案便没有了继续的理由。

项目历史代码（第二版重构结果）保留在 [`deprecated`](../../tree/deprecated) 分支，`main` 分支为第三版重构骨架（未完成）。

本项目 fork 自一个封装 `codex exec` 的社区方案，在此基础上进行了完全重构——对接 Codex app-server JSON-RPC 协议、设计多 Agent 协作架构与 MCP 工具编排层、实现从 JSON-RPC 事件流到结构化 Markdown 的多级投影与渲染管线，经历了三轮从设计到推翻再重构的完整工程迭代。虽未走到终点，但在跨模型协作、AI Agent 工具链设计和前沿协议集成等方向上积累了扎实的实战经验。

感谢每一位分享知识与源码的开发者。开源世界里没有白走的路。

山高路远，下个项目再见。
