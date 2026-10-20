"""语义动作规范化：把框架的 action（含坐标）转成「意图级」规范字符串。

语义轨迹树（semantic trajectory tree）需要比较两个动作「语义是否相同」，
从而把相同语义 history prefix 的轨迹共享节点、在分叉处做 branch comparison。

关键：grounding 给的坐标（x/y/x1/y1/...）不是决策意图，要丢掉；只保留
「动作类型 + 意图参数」（target / text / keys / url / 单元格内容等）。

这样 `click(target="Edit 菜单", x=536, y=447)` 和 `click(target="Edit 菜单", x=540, y=450)`
会归一化成同一个 `click(target='Edit 菜单')`，视为同一个语义动作。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Tuple

# 每个动作类型保留的意图参数（按序），其余（坐标等）丢弃。
# 注意：不含 x/y/x1/y1/x2/y2 等 grounding 坐标。
_SEMANTIC_ARGS: Dict[str, Tuple[str, ...]] = {
    "click": ("target",),
    "double_click": ("target",),
    "right_click": ("target",),
    "type": ("target", "text", "overwrite"),
    "scroll": ("target", "direction", "amount"),
    "press": ("key",),
    "hotkey": ("keys",),
    "goto": ("url",),
    "download": ("url", "filename"),
    "rename_artifact": ("artifact_source", "filename"),
    "select": ("target", "option"),
    "drag_and_drop": ("from_target", "to_target"),
    "set_cell_values": ("new_cell_values", "app_name", "sheet_name"),
    "save": ("app_name",),
    "switch_applications": ("app_code",),
    "wait": ("seconds",),
    "run_in_terminal": ("command",),
    "call_code_agent": ("task",),
    "request_finish": (),
    "fail": (),
    "get_page_source": (),
}

# grounding 坐标类参数（一律丢弃）
_COORD_KEYS = {"x", "y", "x1", "y1", "x2", "y2"}


def _norm_value(v: Any) -> Any:
    """把参数值规范成可哈希、可比较的表示。"""
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    if isinstance(v, (list, tuple)):
        return tuple(_norm_value(x) for x in v)
    return v


def parse_action(action: Any) -> Dict[str, Any]:
    """把 action（dict 或 JSON 字符串）解析成 {"action": ..., "args": {...}}。"""
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except (json.JSONDecodeError, TypeError):
            return {"action": action, "args": {}}
    if isinstance(action, dict):
        return action
    return {"action": str(action), "args": {}}


def semantic_action(action: Any) -> str:
    """返回语义动作规范字符串（可哈希，供语义树节点比较）。"""
    a = parse_action(action)
    name = str(a.get("action", ""))
    args = a.get("args") or {}
    keep = _SEMANTIC_ARGS.get(name, ())
    parts = []
    for k in keep:
        if k not in args:
            continue
        v = _norm_value(args[k])
        if v is None or v == "" or v == () or v == []:
            continue
        parts.append(f"{k}={v!r}")
    return f"{name}({', '.join(parts)})"


def semantic_history(actions) -> Tuple[str, ...]:
    """把一串 action 转成语义历史（语义动作的 tuple），供语义树前缀匹配。"""
    return tuple(semantic_action(a) for a in actions)
