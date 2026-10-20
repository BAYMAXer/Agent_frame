"""Pure action and outcome formatting helpers for pipeline and trajectory output."""
from __future__ import annotations

import json
from typing import Optional, Tuple

from .actions import Action


def _coordinate_of(action: Action) -> Optional[Tuple[int, int]]:
    a = action.args
    if "x" in a and "y" in a:
        return int(a["x"]), int(a["y"])
    if "x1" in a and "y1" in a:
        return int(a["x1"]), int(a["y1"])
    return None


def _target_of(action: Action) -> str:
    a = action.args
    if action.action in ("click", "double_click", "right_click", "select"):
        return str(a.get("target", ""))
    if action.action == "drag_and_drop":
        return f"{a.get('from_target', '')} -> {a.get('to_target', '')}"
    if action.action == "type":
        t = str(a.get("target", ""))
        txt = str(a.get("text", ""))
        if t:
            return f"{txt!r} → {t}"
        return txt
    if action.action == "scroll":
        return f"{a.get('direction', '')} {a.get('amount', '')}"
    if action.action == "cli":
        return str(a.get("command") or (a.get("operation", "") + " " + a.get("path", "") + " → " + a.get("destination", "")))
    if action.action == "run_in_terminal":
        return str(a.get("command", ""))
    if action.action == "goto":
        return str(a.get("url", ""))
    if action.action == "hotkey":
        return "+".join(a.get("keys", []))
    if action.action == "press":
        return str(a.get("key", ""))
    return ""


def _action_desc(action: str, target: str) -> str:
    if target:
        return f"{action} {target}"
    return action


def _operation_mode(action, route) -> str:
    """Execution route known before dispatch; Jev's later hint is separate."""
    channel = (route or {}).get('channel')
    if channel == 'cli' or action.action == 'cli':
        return 'cli'
    if channel == 'reobserve':
        return 'blocked'
    if action.action in {'request_finish', 'fail'}:
        return 'framework'
    if action.action in {'observe_desktop', 'observe_browser', 'list_tabs'}:
        return 'observe'
    return 'dom' if channel == 'browser_dom' else 'gui'


def _compact_action(action) -> str:
    if isinstance(action, Action):
        return json.dumps({"action": action.action, "args": action.args}, ensure_ascii=False)
    return str(action)


def _describe_outcome(step_result) -> str:
    if step_result.done:
        return "任务完成"
    if step_result.reward and step_result.reward > 0:
        return f"有进展(奖励 {step_result.reward})"
    return "已执行（屏幕状态待下一步判断）"
