"""skill 系统：确定性技能（本地精确计算 / 目标机文件与时钟查询）。

- 注册表在 config/skills.yaml（description/params/enabled）；
- 执行逻辑在 handlers.SKILL_HANDLERS；
- 执行结果【不立刻进上下文】，由 agent 存进 pending、下一步才注入。
"""
from __future__ import annotations

from .calculator import safe_calc
from .registry import (
    SkillSpec,
    SkillContext,
    SKILL_REGISTRY,
    load_skill_registry,
    is_skill,
    enabled_skills,
    validate_skill,
    run_skill,
)
from .handlers import SKILL_HANDLERS

__all__ = [
    "SkillSpec", "SkillContext", "SKILL_REGISTRY", "load_skill_registry",
    "is_skill", "enabled_skills", "validate_skill", "run_skill",
    "SKILL_HANDLERS", "safe_calc",
]
