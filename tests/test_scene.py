"""scene.py 单元测试：场景判定与 browser_ax 门控（无需真实浏览器/OSWorld）。"""
from osworld_agent.env import Observation
from osworld_agent.scene import Scene, observation_scene, browser_context, scene_hint


def test_scene_defaults():
    d = Scene().to_dict()
    assert d["browser_use"] == 0
    assert d["structure_available"] is False
    assert d["mode"] == "visual"


def test_observation_scene_none():
    assert observation_scene(None)["mode"] == "visual"


def test_observation_scene_no_marker_defaults_visual():
    obs = Observation()
    assert observation_scene(obs)["reason"] == "no_scene_marker"


def test_observation_scene_reads_marker():
    obs = Observation(info={"scene": Scene(browser_use=1, structure_available=True,
                                            mode="browser_dom").to_dict()})
    sc = observation_scene(obs)
    assert sc["browser_use"] == 1 and sc["structure_available"] is True


def test_browser_context_gated_by_scene():
    ctx = [{"kind": "browser_ax", "snapshot_id": "abc", "nodes": []}]
    obs = Observation(context=ctx, info={"scene": Scene(
        browser_use=1, structure_available=True, snapshot_id="abc").to_dict()})
    assert browser_context(obs) == ctx
    obs2 = Observation(context=ctx, info={"scene": Scene(
        browser_use=0, structure_available=False).to_dict()})
    assert browser_context(obs2) == []


def test_browser_context_legacy_fallback():
    # 未设置 scene marker 的旧环境（如尚未接入的 VMware）：退回 context 里的 browser_ax
    ctx = [{"kind": "browser_ax", "snapshot_id": "abc", "nodes": []}]
    obs = Observation(context=ctx)
    assert browser_context(obs) == ctx


def test_scene_hint_only_decision_keys():
    obs = Observation(info={"scene": Scene(
        browser_use=1, structure_available=True, mode="browser_dom", reason="structured_page",
        url="u", snapshot_id="s", page_id="p").to_dict()})
    hint = scene_hint(obs)
    assert set(hint.keys()) == {"browser_use", "structure_available", "mode", "reason"}
    assert hint["mode"] == "browser_dom"
