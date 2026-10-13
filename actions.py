"""动作空间：类型化 + 结构化 + 配置驱动。

核心思路（解决旧版 9B 出 Python 代码老截断/格式错的问题）：
- 动作注册表从 config/action.yaml 加载（含 extra_actions 扩展区），换动作/加动作只改 yaml；
- 模型只输出【一个 JSON 对象】，字段固定、类型严格；
- 解析后立刻用注册表 schema 校验，不合法就带中文反馈重试；
- 需要定位的（resolve=point/select/drag）只给「目标描述」，坐标交给 grounding 去算。

每个动作（ActionSpec）字段见 config/action.yaml 顶部注释。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import ACTION_YAML, load_yaml


# --------------------------------------------------------------------------- #
# 动作定义
# --------------------------------------------------------------------------- #
@dataclass
class Action:
    action: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionSpec:
    """动作注册表里的一项。"""
    name: str
    params: List[str] = field(default_factory=list)
    description: str = ""
    kind: str = "env"          # env / shell / subagent / terminal / memory
    resolve: str = "none"      # none / point / select / drag


# --------------------------------------------------------------------------- #
# 注册表加载
# --------------------------------------------------------------------------- #
def load_action_registry() -> Dict[str, ActionSpec]:
    """从 config/action.yaml 加载并合并 actions + extra_actions。"""
    data = load_yaml(ACTION_YAML)
    registry: Dict[str, ActionSpec] = {}
    for section in ("actions", "extra_actions"):
        for name, raw in (data.get(section) or {}).items():
            raw = raw or {}
            registry[name] = ActionSpec(
                name=name,
                params=list(raw.get("params") or []),
                description=raw.get("description", ""),
                kind=raw.get("kind", "env"),
                resolve=raw.get("resolve", "none"),
            )
    return registry


# 进程内缓存一次
ACTION_REGISTRY: Dict[str, ActionSpec] = load_action_registry()


def get_action_spec(name: str) -> Optional[ActionSpec]:
    return ACTION_REGISTRY.get(name)


def actions_by_kind(kind: str) -> List[str]:
    """返回某个 kind 下的动作名列表（保持注册表顺序）。"""
    return [n for n, s in ACTION_REGISTRY.items() if s.kind == kind]


def all_action_names() -> List[str]:
    return list(ACTION_REGISTRY.keys())


# --------------------------------------------------------------------------- #
# 解析 + 校验
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里提取第一个 JSON 对象（容错多余文字/markdown 围栏）。"""
    text = text.strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.IGNORECASE).strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def parse_action(text: str) -> Optional[Action]:
    """从旧的扁平格式 {"action": ..., ...} 提取动作（向后兼容）。"""
    obj = _extract_json(text)
    if obj is None or "action" not in obj:
        return None
    return Action(action=str(obj["action"]).strip(),
                  args={k: v for k, v in obj.items() if k != "action"})


def parse_decision(text: str) -> Optional[Dict[str, Any]]:
    """解析决策模型的 JSON 输出，返回完整 dict。

    结构：{"previous_action_assessment": {...}, "state_update": {...}, "next_action": {...}}
    """
    return _extract_json(text)


def action_from_decision(obj: Dict[str, Any]) -> Optional[Action]:
    """从决策 JSON 里提取动作：优先 next_action.type，回退旧的 action 字段。"""
    if not isinstance(obj, dict):
        return None
    if "next_action" in obj:
        na = obj["next_action"]
        if isinstance(na, dict) and "type" in na:
            return Action(action=str(na["type"]).strip(),
                          args={k: v for k, v in na.items() if k != "type"})
        return None
    if "action" in obj:
        return Action(action=str(obj["action"]).strip(),
                      args={k: v for k, v in obj.items() if k != "action"})
    return None


def validate_action(act: Action) -> Optional[str]:
    """校验动作是否合法；返回 None 表示合法，否则返回中文错误说明。"""
    spec = ACTION_REGISTRY.get(act.action)
    if spec is None:
        return f"未知动作类型「{act.action}」。可选动作：{', '.join(ACTION_REGISTRY)}"
    # 策略只能输出语义目标，不能提供执行器内部坐标 / 状态字段
    if any(k in act.args for k in ("x", "y", "x1", "y1", "x2", "y2", "grounding_failed")):
        return "策略只能输出语义目标，不能提供执行器内部坐标或状态字段"
    # target_ref：DOM 节点引用（可选，但有就必须是非空字符串）
    if "target_ref" in act.args and (not isinstance(act.args["target_ref"], str) or not act.args["target_ref"]):
        return "target_ref 必须是当前观察中的非空节点引用"
    # element_id：HTML id 引用（可选，但有就必须是非空字符串）
    if "element_id" in act.args and (not isinstance(act.args["element_id"], str) or not act.args["element_id"]):
        return "element_id 必须是当前页面元素的 HTML id 字符串"
    missing = [k for k in spec.params if act.args.get(k) in (None, "")]
    if missing:
        return f"动作「{act.action}」缺少必填参数：{', '.join(missing)}"
    # 类型级校验（仅对有额外约束的动作）
    if act.action == "scroll":
        try:
            amount = int(act.args["amount"])
            if amount <= 0:
                return "scroll 的 amount 必须是正整数"
            act.args["amount"] = amount
        except (ValueError, TypeError):
            return "scroll 的 amount 必须是整数"
        if act.args["direction"] not in ("up", "down"):
            return "scroll 的 direction 只能是 up 或 down"
    if act.action == "wait":
        try:
            secs = float(act.args["seconds"])
            if not (0 < secs <= 60):
                return "wait 的 seconds 需在 0~60 之间"
            act.args["seconds"] = secs
        except (ValueError, TypeError):
            return "wait 的 seconds 必须是数字"
    if act.action == "hotkey":
        keys = act.args.get("keys")
        if not isinstance(keys, list) or not keys:
            return "hotkey 的 keys 必须是非空列表"
    if act.action == "complete_subtask":
        if any(not isinstance(act.args.get(k), str) or not act.args[k].strip()
               for k in ("step_id", "result", "evidence")):
            return "complete_subtask 的 step_id、result、evidence 必须是非空字符串"
        refs = act.args.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
            return "evidence_refs 必须是节点引用字符串数组"
    return None


def action_signature(act: Action) -> str:
    """动作的紧凑签名，用于判断「连续相同动作」早停。"""
    return json.dumps({"action": act.action, "args": act.args}, ensure_ascii=False, sort_keys=True)
