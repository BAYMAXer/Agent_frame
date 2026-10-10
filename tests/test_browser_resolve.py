"""resolve_target 单元测试：语义匹配 / ref 提取 / allowed 过滤（纯内存，无需浏览器）。"""
from osworld_agent.browser_session import BrowserSession, BrowserOptions


def _session():
    s = BrowserSession(options=BrowserOptions())
    s.snapshot = {"kind": "browser_ax", "snapshot_id": "abc", "nodes": []}
    s.nodes = {
        "1": {"ref": "1", "role": "menuitem", "name": "Remove Account", "value": "", "direct": True},
        "2": {"ref": "2", "role": "button", "name": "Submit", "value": "", "direct": True},
        "3": {"ref": "3", "role": "textbox", "name": "搜索", "value": "", "direct": True},
        "4": {"ref": "4", "role": "generic", "name": "Submit", "value": "", "direct": False},
    }
    return s


def test_quoted_exact_match():
    s = _session()
    assert s.resolve_target("The 'Remove Account' option in the menu") == "1"


def test_ref_in_target_extracted():
    # 模型把 ref 写进 target 描述里（如 "[3]"）→ 直接提取用
    s = _session()
    assert s.resolve_target("The search box [3]") == "3"


def test_non_direct_skipped():
    # 同名节点 "4" 是非 direct，应跳过，命中 direct 的 "2"
    s = _session()
    assert s.resolve_target("The 'Submit' element") == "2"


def test_allowed_filter():
    s = _session()
    assert s.resolve_target("The 'Remove Account' option", allowed={"2", "3"}) is None


def test_no_match_returns_none():
    s = _session()
    assert s.resolve_target("The canvas drawing area") is None


def test_empty_and_no_snapshot():
    s = _session()
    assert s.resolve_target("") is None
    s2 = BrowserSession(options=BrowserOptions())
    assert s2.resolve_target("The 'Submit' button") is None


def test_resolve_id():
    s = _session()
    s.nodes["2"]["id"] = "download-btn"
    assert s.resolve_id("download-btn") == "2"
    assert s.resolve_id("missing") is None


def test_bracket_phrase_match():
    # 方括号短语（如 "[PDF] xxx"）也应能命中 DOM
    s = _session()
    s.nodes["5"] = {"ref": "5", "role": "link", "name": "[PDF] ieeexplore.ieee.org",
                    "value": "", "direct": True}
    assert s.resolve_target("The [PDF] ieeexplore.ieee.org link next to the first search result") == "5"


def test_route_by_element_id():
    from osworld_agent.actions import Action
    from osworld_agent.env import Observation
    s = _session()
    s.nodes["2"]["id"] = "download-btn"
    obs = Observation(info={"exposed_refs": list(s.nodes.keys())})
    action = Action("click", {"target": "the download button", "element_id": "download-btn"})
    route = s.route(action, obs)
    assert route["channel"] == "browser_dom"
    assert route["target_ref"] == "2"


def test_screen_to_viewport():
    class FakePage:
        def evaluate(self, js):
            return {"sx": 100, "sy": 80, "iw": 1200, "ih": 800, "oh": 900, "dpr": 1}
    # vx = 300/1 - 100 = 200; vy = 500/1 - 80 - (900-800) = 320
    assert BrowserSession._screen_to_viewport(FakePage(), 300, 500) == (200, 320)
