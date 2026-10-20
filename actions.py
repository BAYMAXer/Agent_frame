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

# Policy capability boundary: configuration/skills cannot silently re-enable code shortcuts.
BASIC_ACTIONS = frozenset({"cli","click", "double_click", "right_click", "type", "scroll", "press",
    "hotkey", "goto", "select", "drag_and_drop", "switch_applications", "wait", "get_page_source",
    "run_browser_js", "handle_dialog", "list_tabs", "switch_tab", "close_tab", "observe_desktop", "observe_browser",
    "request_finish", "fail"})


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
    kind: str = "env"          # env / uno / shell / subagent / terminal / memory
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
            if name not in BASIC_ACTIONS:
                continue
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
        # An explicit finish request with an existing explanation is unambiguous.
        # Normalize only this observed spelling error; never invent click/key
        # parameters or infer task success from the operator's explanation.
        if (na == "request_finish"
                and set(obj) <= {"next_action", "summary", "answer"}):
            answer = obj.get("answer", obj.get("summary"))
            if isinstance(answer, str) and answer.strip():
                return Action(action="request_finish", args={"answer": answer})
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
    if act.action == "cli":
        from .cli_executor import validate_cli
        return validate_cli(act.args)
    spec = ACTION_REGISTRY.get(act.action) if act.action in BASIC_ACTIONS else None
    if spec is None:
        return f"未知动作类型「{act.action}」。可选动作：{', '.join(ACTION_REGISTRY)}"
    # 策略只能输出语义目标，不能提供执行器内部坐标 / 状态字段
    if any(k in act.args for k in ("x", "y", "x1", "y1", "x2", "y2", "grounding_failed", "coordinate_space", "observation_metadata")):
        return "策略只能输出语义目标，不能提供执行器内部坐标或状态字段"
    # 空字符串的 element_id / target_ref：模型常输出 element_id:"" 表示「没有 id」，
    # 应忽略（移除）而非拒绝动作，让 target_ref / target 语义匹配继续生效。
    if act.args.get("element_id") == "":
        act.args.pop("element_id")
    if act.args.get("target_ref") == "":
        act.args.pop("target_ref")
    # target_ref：DOM 节点引用（可选，但有就必须是非空字符串）
    if "target_ref" in act.args and (not isinstance(act.args["target_ref"], str) or not act.args["target_ref"]):
        return "target_ref 必须是当前观察中的非空节点引用"
    # element_id：HTML id 引用（可选，但有就必须是非空字符串）
    if "element_id" in act.args and (not isinstance(act.args["element_id"], str) or not act.args["element_id"]):
        return "element_id 必须是当前页面元素的 HTML id 字符串"
    # Anonymous AX controls have deterministic identities without a name.
    # Only target is optional here; other operands and reference checks remain.
    explicit_target = (act.action in {"click", "double_click", "right_click", "type", "select"}
                       and bool(act.args.get("target_ref") or act.args.get("element_id")))
    missing = [k for k in spec.params if act.args.get(k) in (None, "")
               and not (k == "target" and explicit_target)]
    if missing:
        return f"动作「{act.action}」缺少必填参数：{', '.join(missing)}"
    if "submit_key" in act.args:
        if act.action != 'type' or act.args['submit_key'] != 'Enter':
            return 'submit_key 仅支持 type 输入后按 Enter'
        if act.args.get('overwrite') is not True or not (act.args.get('target_ref') or act.args.get('element_id')):
            return '输入后提交须使用当前 DOM 引用并设置 overwrite:true；其他情况分开 type 和 press'
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
    if act.action == "handle_dialog":
        if type(act.args.get("accept")) is not bool:
            return "handle_dialog 的 accept 必须为 true 或 false"
        if "prompt_text" in act.args and not isinstance(act.args["prompt_text"], str):
            return "prompt_text 必须是字符串"
    if act.action in ("switch_tab", "close_tab") and "tab_id" in act.args:
        if not isinstance(act.args["tab_id"], str) or not act.args["tab_id"].strip():
            return "tab_id 必须来自当前标签页列表"
    if act.action in {"download", "rename_artifact"} and "filename" in act.args:
        name = act.args["filename"]
        if (not isinstance(name, str) or not name.strip() or name in {".", ".."}
                or any(c in name for c in '/\\\\\x00') or any(ord(c) < 32 for c in name)):
            return "filename 必须是不含路径的文件名"
    if act.action == "rename_artifact":
        if not isinstance(act.args.get("artifact_id"), str) or not act.args["artifact_id"].strip():
            return "artifact_id 必须来自当前观察中已登记的下载文件"
    return None


def action_signature(act: Action) -> str:
    """动作的紧凑签名，用于判断「连续相同动作」早停。"""
    return json.dumps({"action": act.action, "args": act.args}, ensure_ascii=False, sort_keys=True)
