"""Browser startup is an explicit navigation effect, not task setup or teardown."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osworld_agent import utils
from osworld_agent.actions import Action
from osworld_agent.adapters.windows_env import WindowsEnvironment
from osworld_agent.browser_session import BrowserSession
from osworld_agent.env import Observation, StepResult
from osworld_agent.pipeline import Pipeline, StepState
from osworld_agent.prompts import build_system_prompt


@pytest.fixture
def navigation(monkeypatch):
    focused = {"browser": False}
    launcher = Mock()
    env = WindowsEnvironment(browser_launcher=launcher)
    session = Mock()
    session.goto.return_value = {"status": "executed", "dispatched": True,
                                 "action": "goto", "url": "https://example.com"}
    session.activate.side_effect = lambda: focused.update(browser=True)
    env._browser_session = session
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda _os: focused["browser"])
    monkeypatch.setattr(env, "_build_observation", lambda: Observation())
    gui = SimpleNamespace(hotkey=Mock(), press=Mock(), write=Mock(), click=Mock())
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    return env, launcher, session, focused, gui


def test_reset_desktop_actions_and_close_never_launch(navigation):
    env, launch, session, _focus, gui = navigation
    env.reset("Edit a local document")
    env.step(Action("click", {"target": "Notepad", "x": 10, "y": 20}))
    env.close()
    launch.assert_not_called()
    session.activate.assert_not_called()
    session.goto.assert_not_called()
    gui.click.assert_called_once_with(10, 20)
    session.close.assert_called_once_with()


def test_goto_launches_only_when_requested_then_checks_focus(navigation):
    env, launch, session, focus, gui = navigation
    result = env.step(Action("goto", {"url": "https://example.com"}))
    launch.assert_called_once_with()
    session.activate.assert_called_once_with()
    assert focus["browser"] is True
    session.goto.assert_called_once_with("https://example.com")
    gui.hotkey.assert_not_called()
    assert result.info["status"] == "executed"
    env.step(Action("goto", {"url": "https://example.org"}))
    launch.assert_called_once_with()
    session.activate.assert_called_once_with()


def test_existing_foreground_browser_does_not_launch(navigation):
    env, launch, session, focus, _gui = navigation
    focus["browser"] = True
    env.step(Action("goto", {"url": "example.com"}))
    launch.assert_not_called()
    session.activate.assert_not_called()
    session.goto.assert_called_once_with("example.com")


def test_launch_does_not_force_browser_scene_if_activation_fails(navigation):
    env, launch, session, _focus, gui = navigation
    session.activate.side_effect = None
    session.activate.return_value = False
    result = env.step(Action("goto", {"url": "example.com"}))
    launch.assert_called_once_with()
    session.goto.assert_not_called()
    gui.hotkey.assert_not_called()
    assert result.info["status"] == "rejected"
    assert result.info["dispatched"] is False


def test_browser_launch_failure_is_returned_without_desktop_navigation(navigation):
    env, launch, session, _focus, gui = navigation
    launch.side_effect = FileNotFoundError("missing Chrome")
    result = env.step(Action("goto", {"url": "example.com"}))
    assert result.info["status"] == "rejected"
    assert "missing Chrome" in result.info["error"]
    session.goto.assert_not_called()
    gui.hotkey.assert_not_called()


def test_goto_without_url_does_not_launch(navigation):
    env, launch, _session, _focus, gui = navigation
    result = env.step(Action("goto"))
    launch.assert_not_called()
    gui.hotkey.assert_not_called()
    assert result.info["status"] == "rejected"


def test_focused_browser_without_cdp_uses_native_navigation(navigation, monkeypatch):
    env, launch, session, focus, gui = navigation
    focus["browser"] = True
    session.goto.return_value = {"status": "rejected", "dispatched": False, "error": "No page"}
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    result = env.step(Action("goto", {"url": "example.com"}))
    launch.assert_not_called()
    gui.hotkey.assert_called_once_with("ctrl", "l")
    gui.write.assert_called_once_with("example.com")
    gui.press.assert_called_once_with("enter")
    assert result.info["action"] == "goto"


def test_native_navigation_is_rejected_if_focus_changed_during_cdp(navigation):
    env, _launch, session, focus, gui = navigation
    focus["browser"] = True
    def lose_focus(_url):
        focus["browser"] = False
        return {"status": "rejected", "dispatched": False}
    session.goto.side_effect = lose_focus
    result = env.step(Action("goto", {"url": "example.com"}))
    gui.hotkey.assert_not_called()
    assert result.info["status"] == "rejected"


def test_uncertain_navigation_is_not_repeated_with_native_input(navigation):
    env, _launch, session, focus, gui = navigation
    focus["browser"] = True
    session.goto.return_value = {"status": "uncertain", "dispatched": False}
    result = env.step(Action("goto", {"url": "example.com"}))
    assert result.info["status"] == "uncertain"
    gui.hotkey.assert_not_called()


@pytest.mark.parametrize("url", ["about:blank", "chrome://newtab/", "edge://newtab/",
                                 "https://example.com"])
def test_focused_blank_page_can_be_used_for_first_navigation(url):
    page = SimpleNamespace(url=url, evaluate=Mock(return_value=True))
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    assert BrowserSession._focused_page(browser) is page


def test_blank_background_page_is_not_selected():
    page = SimpleNamespace(url="about:blank", evaluate=Mock(return_value=False))
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    assert BrowserSession._focused_page(browser) is None


def test_page_selection_rechecks_foreground_after_connection():
    page = SimpleNamespace(url="https://example.com", evaluate=Mock(return_value=True))
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    session = BrowserSession(foreground_check=lambda: False)
    session.snapshot = {"snapshot_id": "old"}
    assert session._pick_page(browser) is None
    assert session.snapshot is None
    page.evaluate.assert_not_called()


def test_activation_picks_visible_page_and_only_disconnects(monkeypatch):
    hidden = SimpleNamespace(url="https://hidden.example", evaluate=Mock(return_value=False),
                             bring_to_front=Mock())
    visible = SimpleNamespace(url="about:blank", bring_to_front=Mock())
    visible.evaluate = Mock(side_effect=lambda script: script == "document.visibilityState === 'visible'")
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[hidden, visible])], close=Mock())
    playwright = SimpleNamespace(stop=Mock())
    session = BrowserSession()
    monkeypatch.setattr(session, "_connect", lambda: (playwright, browser))
    assert session.activate() is True
    hidden.bring_to_front.assert_not_called()
    visible.bring_to_front.assert_called_once_with()
    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()


def test_activation_rejects_multiple_visible_windows(monkeypatch):
    pages = [SimpleNamespace(url="https://example.com", bring_to_front=Mock(),
                              evaluate=Mock(side_effect=lambda s: s == "document.visibilityState === 'visible'"))
             for _ in range(2)]
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=pages)], close=Mock())
    playwright = SimpleNamespace(stop=Mock())
    session = BrowserSession()
    monkeypatch.setattr(session, "_connect", lambda: (playwright, browser))
    assert session.activate() is False
    for page in pages:
        page.bring_to_front.assert_not_called()
    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()


def test_windows_prompt_supports_desktop_and_explains_lazy_navigation():
    prompt = build_system_prompt(os_name="windows", browser_only=False)
    assert "GUI 操作智能体" in prompt
    assert "普通桌面任务直接操作对应应用" in prompt
    assert "只有当前焦点位于浏览器才进入 browser use" in prompt


def test_focus_change_reobserve_returns_new_desktop_frame():
    desktop = Observation(screenshot="new desktop", info={"scene": {"browser_use": 0}})
    env = Mock()
    env._fresh_obs_after_action.return_value = desktop
    pipeline = Pipeline(model=Mock(), grounding=Mock(), env=env, system_prompt="")
    state = StepState(obs=Observation(screenshot="old browser"),
                      route={"channel": "reobserve", "refresh_observation": True})
    execution = pipeline.execute(Action("press", {"key": "enter"}), state)
    assert execution.obs is desktop
    assert execution.info["dispatched"] is False
    env.step.assert_not_called()


def test_on_demand_navigation_result_reaches_pipeline_verification(navigation):
    env, _launch, _session, _focus, _gui = navigation
    pipeline = Pipeline(model=Mock(), grounding=Mock(), env=env, system_prompt="")
    execution = pipeline.execute(Action("goto", {"url": "example.com"}), StepState())
    assert execution.info["action"] == "goto"
    assert execution.info["status"] == "executed"
    assert execution.obs is not None
