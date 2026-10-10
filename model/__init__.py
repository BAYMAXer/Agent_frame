"""模型层：决策模型（decision_model）+ 视觉定位（grounding）。

对外统一从 model 导入，调用方不需要关心具体在哪个子模块。
"""
from __future__ import annotations

from .decision_model import (
    ChatModel,
    ChatMessage,
    MODEL_REGISTRY,
    resolve_model_name,
    build_decision_model,
)
from .grounding import (
    Grounding,
    GroundingResult,
    UItarsGrounding,
    NullGrounding,
    resolve_coords,
    mark_coordinate,
    GROUNDING_REGISTRY,
    build_grounding_model,
)

__all__ = [
    "ChatModel", "ChatMessage", "MODEL_REGISTRY", "resolve_model_name",
    "build_decision_model",
    "Grounding", "GroundingResult", "UItarsGrounding", "NullGrounding",
    "resolve_coords", "mark_coordinate", "GROUNDING_REGISTRY", "build_grounding_model",
]
