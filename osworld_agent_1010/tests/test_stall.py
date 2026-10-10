"""卡死检测：稳定动作签名（忽略每次变化的 ref/id/href）测试。"""
from osworld_agent.actions import Action
from osworld_agent.pipeline import Pipeline


def test_stable_key_ignores_changing_ref():
    a1 = Action("click", {"target": "The 'Paper' link (PDF) in the page", "target_ref": "aaaa11112222:5"})
    a2 = Action("click", {"target": "The 'Paper' link (PDF) in the page", "target_ref": "bbbb33334444:5"})
    assert Pipeline._stable_action_key(a1) == Pipeline._stable_action_key(a2)


def test_stable_key_ignores_href_variation():
    a1 = Action("click", {"target": "The 'Paper' link (PDF) with href /x/a-Paper.pdf"})
    a2 = Action("click", {"target": "The 'Paper' link (PDF) with href /y/b-Paper.pdf"})
    assert Pipeline._stable_action_key(a1) == Pipeline._stable_action_key(a2)


def test_stable_key_distinguishes_different_target():
    a1 = Action("click", {"target": "The 'Paper' link"})
    a2 = Action("click", {"target": "The 'Bibtex' link"})
    assert Pipeline._stable_action_key(a1) != Pipeline._stable_action_key(a2)


def test_stable_key_keeps_text():
    a1 = Action("type", {"target": "search box", "text": "Attention"})
    a2 = Action("type", {"target": "search box", "text": "Transformer"})
    assert Pipeline._stable_action_key(a1) != Pipeline._stable_action_key(a2)
