"""记忆包：结构化记忆（新机制）。

新记忆机制（本地 Qwen3.5-VL-9B）：
- StructuredMemory：task_state / recent_events / known_facts / failure_memory 四部分；
- 不保存每步完整的 reasoning/plan（易污染后续判断），长期保留的是结构化事实；
- 核心原则：LLM 负责理解环境、判断状态变化，程序负责真正保存和更新记忆；
- 模型只能输出 state_update 补丁（patch），不能重写整份 memory。
"""
from __future__ import annotations

from .structured_memory import StructuredMemory

__all__ = ["StructuredMemory"]
