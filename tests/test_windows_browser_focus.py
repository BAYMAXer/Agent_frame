"""Windows browser routing follows foreground focus without using a real desktop."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osworld_agent import utils
from osworld_agent.actions import Action
from osworld_agent.adapters.windows_env import WindowsEnvironment


@pytest.fixture
def desktop(monkeypatch):
    gui = SimpleNamespace(screenshot=Mock(return_value="desktop screenshot"),
                          click=Mock(), doubleClick=Mock(), write=Mock(),
                          hotkey=Mock(), press=Mock(), scroll=Mock(),
                          moveTo=Mock(), dragTo=Mock())
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    return gui


@pytest.fixture
def env(monkeypatch):
    instance = WindowsEnvironment()
    monkeypatch.setattr(utils, "detect_foreground_browser", Mock(return_value=True))
    session = Mock()
    session.snapshot = {"kind": "browser_ax", "snapshot_id": "current", "url": "https://example.com"}
    session.screenshot.return_value = "viewport screenshot"
    session.collect.return_value = session.snapshot
    session.route.return_value = {"channel": "browser_dom", "target_ref": "1",
                                  "snapshot_id": "current"}
    session.resolve_target.return_value = None
    for method in ("execute", "execute_target", "goto", "click_at_viewport"):
        getattr(session, method).return_value = {"status": "executed", "dispatched": True}
    instance._browser_session = session
    return instance


def test_desktop_environment_is_not_browser_only():
    assert WindowsEnvironment().browser_only is False


def test_foreground_check_uses_os_focus_even_with_cdp_pages(monkeypatch):
    foreground = Mock(return_value=False)
    monkeypatch.setattr(utils, "detect_foreground_browser", foreground)
    pages = Mock(return_value=[{"url": "https://example.com"}])
    monkeypatch.setattr(utils, "list_cdp_pages", pages)
    assert WindowsEnvironment()._focus_is_browser() is False
    foreground.assert_called_once_with("windows")
    pages.assert_not_called()


def test_session_requires_both_browser_foreground_and_document_focus():
    env = WindowsEnvironment()
    session = env._get_browser_session()
    assert session.options.require_focus is True
    assert session.foreground_check == env._focus_is_browser


def test_desktop_observation_ignores_background_browser(env, desktop, monkeypatch):
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    obs = env.reset("Edit a document")
    assert obs.screenshot == "desktop screenshot"
    assert obs.context == []
    assert obs.info["scene"]["browser_use"] == 0
    assert obs.info["screenshot_coordinate_space"] == "screen"
    env._browser_session.collect.assert_not_called()
    env._browser_session.screenshot.assert_not_called()
    env._browser_session.invalidate.assert_called()


def test_stale_snapshot_does_not_override_foreground_focus(env, monkeypatch):
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    scene = env._derive_scene(env._browser_session.snapshot)
    assert scene["browser_use"] == 0
    assert scene["structure_available"] is False
    assert env._collect_browser_snapshot() is None
    env._browser_session.collect.assert_not_called()


def test_snapshot_is_discarded_if_focus_changes_during_collect(env, monkeypatch):
    monkeypatch.setattr(utils, "detect_foreground_browser", Mock(side_effect=[True, False]))
    assert env._collect_browser_snapshot() is None
    env._browser_session.invalidate.assert_called_once()


def test_failed_collect_invalidates_previous_structure(env):
    env._browser_session.collect.return_value = None
    assert env._collect_browser_snapshot() is None
    env._browser_session.invalidate.assert_called_once()


def test_focused_page_keeps_viewport_cdp_coordinates(env, desktop):
    obs = env.reset("Click a page control")
    assert obs.screenshot == "viewport screenshot"
    assert obs.info["scene"]["mode"] == "browser_dom"
    assert obs.info["screenshot_coordinate_space"] == "css_viewport"
    result = env.step(Action("click", {"x": 45, "y": 65}))
    env._browser_session.click_at_viewport.assert_called_once()
    assert result.info["dispatched"] is True
    desktop.click.assert_not_called()


def test_browser_chrome_ui_uses_desktop_screenshot_and_input(env, desktop):
    # document.hasFocus() is false when the address bar or browser menu owns focus.
    env._browser_session.screenshot.return_value = None
    obs = env.reset("Use the address bar")
    assert obs.screenshot == "desktop screenshot"
    assert obs.context == []
    assert obs.info["scene"]["browser_use"] == 1
    assert obs.info["scene"]["structure_available"] is False
    assert obs.info["screenshot_coordinate_space"] == "screen"
    assert env.route_action(Action("hotkey", {"keys": ["ctrl", "l"]}), obs) is None
    env.step(Action("click", {"x": 45, "y": 65}))
    desktop.click.assert_called_once_with(45, 65)
    env._browser_session.click_at_viewport.assert_not_called()


def test_snapshot_failure_followed_by_chrome_focus_refreshes_full_screen(env, desktop):
    env._browser_session.screenshot.side_effect = ["viewport screenshot", None]
    env._browser_session.collect.return_value = None
    obs = env.reset("Open browser menu")
    assert obs.screenshot == "desktop screenshot"
    assert obs.context == []
    assert obs.info["screenshot_coordinate_space"] == "screen"


@pytest.mark.parametrize("action", [Action("click", {"target_ref": "1"}),
                                    Action("scroll", {"direction": "down", "amount": 1}),
                                    Action("press", {"key": "enter"}),
                                    Action("hotkey", {"keys": ["ctrl", "l"]})])
def test_background_browser_actions_do_not_route(env, desktop, monkeypatch, action):
    obs = env.reset("Use a page")
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    route = env.route_action(action, obs)
    assert route["channel"] == "reobserve"
    assert route["refresh_observation"] is True
    env._browser_session.route.assert_not_called()


def test_desktop_observation_does_not_require_reobserve_without_browser_focus(env, desktop, monkeypatch):
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    obs = env.reset("Use a desktop application")
    assert env.route_action(Action("press", {"key": "enter"}), obs) is None


@pytest.mark.parametrize("action,route", [
    (Action("goto", {"url": "https://example.com"}), {"channel": "browser_dom"}),
    (Action("click", {"target_ref": "1"}), {"channel": "browser_dom", "target_ref": "1"}),
    (Action("scroll", {"direction": "down", "amount": 1}), {"channel": "browser_dom"}),
    (Action("press", {"key": "enter"}), {"channel": "browser_dom"}),
    (Action("hotkey", {"keys": ["ctrl", "l"]}), {"channel": "browser_dom"}),
])
def test_routed_actions_reject_focus_loss_without_falling_back(env, desktop, monkeypatch, action, route):
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    result = env.execute_routed(action, route)
    assert result.info["dispatched"] is False
    assert result.info["status"] == "rejected"
    assert result.observation.info["scene"]["browser_use"] == 0
    for method in ("execute", "execute_target", "goto", "click_at_viewport"):
        getattr(env._browser_session, method).assert_not_called()
    desktop.click.assert_not_called()
    desktop.hotkey.assert_not_called()


def test_viewport_coordinates_are_never_reused_on_desktop_after_focus_loss(env, desktop, monkeypatch):
    env.reset("Use a page")
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    result = env.step(Action("type", {"x": 45, "y": 65, "text": "hello"}))
    assert result.info["dispatched"] is False
    desktop.click.assert_not_called()
    desktop.write.assert_not_called()
    env._browser_session.click_at_viewport.assert_not_called()


@pytest.mark.parametrize("action", [
    Action("press", {"key": "enter"}),
    Action("hotkey", {"keys": ["ctrl", "a"]}),
    Action("scroll", {"direction": "down", "amount": 1}),
    Action("select", {"x": 45, "y": 65, "option": "One"}),
    Action("drag_and_drop", {"x1": 45, "y1": 65, "x2": 145, "y2": 165}),
    Action("type", {"text": "hello"}),
])
def test_all_viewport_inputs_reject_focus_loss(env, desktop, monkeypatch, action):
    env.reset("Use a page")
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    result = env.step(action)
    assert result.info["status"] == "rejected"
    assert result.info["dispatched"] is False
    assert result.observation.info["scene"]["browser_use"] == 0
    for method in ("press", "hotkey", "scroll", "click", "write", "moveTo", "dragTo"):
        getattr(desktop, method).assert_not_called()


@pytest.mark.parametrize("action", [
    Action("select", {"x": 45, "y": 65, "option": "One"}),
    Action("drag_and_drop", {"x1": 45, "y1": 65, "x2": 145, "y2": 165}),
])
def test_native_viewport_actions_switch_to_full_screen_before_grounding(env, desktop, action):
    env.reset("Use a page")
    result = env.step(action)
    assert result.info["status"] == "rejected"
    assert result.info["dispatched"] is False
    assert result.observation.screenshot == "desktop screenshot"
    assert result.observation.context == []
    assert result.observation.info["screenshot_coordinate_space"] == "screen"
    assert result.observation.info["scene"]["browser_use"] == 1
    assert result.observation.info["scene"]["structure_available"] is False
    assert env._screenshot_is_viewport is False
    env._browser_session.invalidate.assert_called()
    for method in ("click", "write", "moveTo", "dragTo"):
        getattr(desktop, method).assert_not_called()

    # The next action uses newly grounded full-screen coordinates, so native GUI is safe.
    next_action = (Action("select", {"x": 345, "y": 465, "option": "One"})
                   if action.action == "select" else
                   Action("drag_and_drop", {"x1": 345, "y1": 465, "x2": 445, "y2": 565}))
    env.step(next_action)
    if action.action == "select":
        desktop.click.assert_called_once_with(345, 465)
    else:
        desktop.moveTo.assert_called_once_with(345, 465)
        desktop.dragTo.assert_called_once_with(445, 565, duration=1.0)
    env._browser_session.click_at_viewport.assert_not_called()


def test_desktop_coordinates_are_not_reinterpreted_when_browser_gains_focus(env, desktop, monkeypatch):
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    env.reset("Use a desktop application")
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: True)
    env.step(Action("click", {"x": 45, "y": 65}))
    desktop.click.assert_called_once_with(45, 65)
    env._browser_session.click_at_viewport.assert_not_called()


def test_page_source_is_not_fetched_from_background_browser(env, monkeypatch):
    monkeypatch.setattr(utils, "detect_foreground_browser", lambda os_name: False)
    fetch = Mock()
    monkeypatch.setattr(utils, "fetch_page_source", fetch)
    assert "当前焦点不在浏览器" in env.get_page_source()
    fetch.assert_not_called()
