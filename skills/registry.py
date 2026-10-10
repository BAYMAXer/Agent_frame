"""skill 系统：注册表 + 校验 + 分发。

分层：
- SkillSpec：一个 skill 的声明（description/params/enabled），来自 config/skills.yaml；
- SkillContext：执行时传给 handler 的上下文，提供 env / controller 访问；
- run_skill：按 name 分发到 handlers.SKILL_HANDLERS 执行。

与「动作」的区别：动作最终落到环境（鼠标键盘），skill 是确定性能力
（本地精确计算 / 目标机文件与时钟查询），返回一个字符串结果给决策模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import SKILLS_YAML, load_yaml


@dataclass
class SkillSpec:
    name: str
    description: str = ""
    params: List[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class SkillContext:
    """传给 skill handler 的上下文，用于访问环境（目标机）。

    环境接入后（env.controller 非空），verify_file / get_current_time 这类
    需要查询目标机的 skill 就走 controller；未接入时 handler 自行回退。
    """
    env: Any = None

    @property
    def controller(self):
        return getattr(self.env, "controller", None) if self.env is not None else None


def load_skill_registry() -> Dict[str, SkillSpec]:
    """从 config/skills.yaml 加载并合并 skills + extra_skills。"""
    data = load_yaml(SKILLS_YAML)
    registry: Dict[str, SkillSpec] = {}
    for section in ("skills", "extra_skills"):
        for name, raw in (data.get(section) or {}).items():
            raw = raw or {}
            registry[name] = SkillSpec(
                name=name,
                description=raw.get("description", ""),
                params=list(raw.get("params") or []),
                enabled=bool(raw.get("enabled", True)),
            )
    return registry


SKILL_REGISTRY: Dict[str, SkillSpec] = load_skill_registry()


def is_skill(name: str) -> bool:
    return name in SKILL_REGISTRY


def enabled_skills() -> List[str]:
    return [n for n, s in SKILL_REGISTRY.items() if s.enabled]


def validate_skill(name: str, args: Dict[str, Any]) -> Optional[str]:
    """校验一个 skill 动作；返回 None 表示合法，否则返回中文错误。"""
    spec = SKILL_REGISTRY.get(name)
    if spec is None:
        return None  # 不是 skill，交给动作层判断
    if not spec.enabled:
        return f"skill「{name}」当前被禁用"
    missing = [k for k in spec.params if args.get(k) in (None, "")]
    if missing:
        return f"skill「{name}」缺少必填参数：{', '.join(missing)}"
    return None


def run_skill(name: str, args: Dict[str, Any], ctx: Optional[SkillContext] = None) -> str:
    """执行一个 skill，返回结果字符串。未注册/未实现时返回中文报错。"""
    if name not in SKILL_REGISTRY:
        return f"未注册的 skill: {name}"
    # 延迟导入，避免 registry <-> handlers 循环依赖
    from .handlers import SKILL_HANDLERS
    handler = SKILL_HANDLERS.get(name)
    if handler is None:
        return f"skill「{name}」已声明但未实现 handler（见 skills/handlers.py 的 SKILL_HANDLERS）"
    try:
        return handler(args or {}, ctx)
    except Exception as exc:  # noqa: BLE001
        return f"skill「{name}」执行失败: {exc}"
