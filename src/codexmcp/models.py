"""数据模型定义：ItemState 和 CollectedEvent。

定义事件收集器使用的核心数据结构，
用于追踪 Item 生命周期状态和记录收集的事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ItemState:
    """单个 Item 的状态追踪。

    追踪 Item 从 started → streaming → completed 的完整生命周期。
    每种 Item 类型（agentMessage / commandExecution / fileChange / reasoning）
    都使用此数据结构统一管理。

    Attributes:
        item_id: Item 唯一标识符
        item_type: Item 类型（agentMessage / commandExecution / fileChange / reasoning）
        status: 当前状态（started / streaming / completed）
        content_buffer: 累积的 delta 内容（文本/输出/diff）
        reasoning_summary: reasoning 类型的 summaryDelta 累积
        reasoning_text: reasoning 类型的 textDelta 累积
        metadata: item/started 事件的完整参数（保留原始信息用于聚合）
    """

    item_id: str
    item_type: str
    status: str
    content_buffer: str = ""
    reasoning_summary: str = ""
    reasoning_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectedEvent:
    """单个收集的事件记录。

    不可变的事件快照，按接收顺序存入有序列表。
    配合游标机制实现增量读取。

    Attributes:
        index: 全局序号（在所属 EventCollector 中的位置）
        timestamp: 接收时间戳（time.time()）
        method: JSON-RPC 方法名（如 "item/agentMessage/delta"）
        params: 原始参数字典
        summary: 人类可读摘要（用于轮询模式展示）
    """

    index: int
    timestamp: float
    method: str
    params: dict[str, Any]
    summary: str
