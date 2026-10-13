"""确定性场景判定：显式区分「浏览器有 AX 结构」和「纯视觉」。

借鉴 osworld_agent_v3：把"当前是浏览器场景 / 有没有可用的 browser_ax 快照 / 该走
DOM 还是视觉"做成显式、可观测的状态，写进观察 info 和 prompt，轨迹里也能看到。

- 环境在 _build_observation 里算好 scene 放进 obs.info["scene"]；
- pipeline 把 scene 摘要插进 prompt（模型据此知道该走结构还是视觉）；
- observation_scene / browser_context 是读取侧，供轨迹和路由复用。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass
class Scene:
    browser_use: int = 0              # 1 = 当前在浏览器场景
    structure_available: bool = False # True = 当前有可用的 browser_ax 快照
    mode: str = "visual"              # browser_dom / visual / desktop
    reason: str = "not_detected"
    page_id: str = ""
    url: str = ""
    snapshot_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def observation_scene(observation) -> Dict[str, Any]:
    """从观察里读场景；没有显式 marker 时退回「纯视觉」默认。"""
    if observation is None:
        return Scene().to_dict()
    info = getattr(observation, "info", None) or {}
    value = info.get("scene")
    return value if isinstance(value, dict) else Scene(mode="visual", reason="no_scene_marker").to_dict()


def browser_context(observation) -> List[dict]:
    """只有场景明确说「有可用结构」才返回 browser_ax 块；否则 []（走视觉）。

    未设置 scene marker 的观察仍可从 obs.context 读取 browser_ax 块。
    """
    scene = observation_scene(observation)
    if scene.get("reason") == "no_scene_marker":
        ctx = getattr(observation, "context", None) or []
        return [b for b in ctx if b.get("kind") == "browser_ax"]
    if scene.get("browser_use") != 1 or not scene.get("structure_available"):
        return []
    snap_id = scene.get("snapshot_id", "")
    ctx = getattr(observation, "context", None) or []
    return [b for b in ctx if b.get("kind") == "browser_ax"
            and (not snap_id or b.get("snapshot_id") == snap_id)]


def scene_hint(observation) -> Dict[str, Any]:
    """给 prompt 用的场景摘要（只保留模型需要判断的字段）。"""
    scene = observation_scene(observation)
    return {k: scene.get(k) for k in ("browser_use", "structure_available", "mode", "reason")}
