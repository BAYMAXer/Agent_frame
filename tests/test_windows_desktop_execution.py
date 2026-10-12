"""Desktop regressions use mocked GUI/Win32 calls and never operate the desktop."""
import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osworld_agent.actions import Action
from osworld_agent.adapters import windows_input
from osworld_agent.adapters.windows_env import WindowsEnvironment
from osworld_agent.env import Observation


@pytest.fixture
def desktop(monkeypatch):
    calls = []
    gui = SimpleNamespace(**{name: Mock(side_effect=lambda *args, _name=name, **kwargs:
                                        calls.append((_name, args, kwargs)))
                             for name in ("click", "write", "press", "hotkey", "scroll")})
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    monkeypatch.setattr("osworld_agent.adapters.windows_env.time.sleep",
                        lambda seconds: calls.append(("sleep", seconds)))
    env = WindowsEnvironment()
    env._focus_is_browser = lambda: False
    env._build_observation = lambda: calls.append(("screenshot",)) or Observation(screenshot="desktop")
    return env, gui, calls


def test_ascii_type_focuses_clears_types_and_waits_before_observation(desktop):
    env, gui, calls = desktop
    result = env.step(Action("type", {"x": 10, "y": 20, "text": "hello", "overwrite": True}))
    assert calls == [("click", (10, 20), {}), ("hotkey", ("ctrl", "a"), {}),
                     ("press", ("backspace",), {}), ("write", ("hello",), {}),
                     ("sleep", 0.3), ("screenshot",)]
    assert result.info == {"channel": "gui", "action": "type", "status": "executed", "dispatched": True}


def test_unicode_type_uses_utf16_down_up_events_including_surrogate_pair(desktop, monkeypatch):
    env, gui, calls = desktop
    sent = []
    def send(count, events, size):
        sent.append((count, size, [(event.type, event.ki.wVk, event.ki.wScan, event.ki.dwFlags)
                                  for event in events]))
        calls.append(("unicode",))
        return count
    user32 = SimpleNamespace(SendInput=Mock(side_effect=send))
    monkeypatch.setattr(windows_input, "_load_user32", lambda: user32)
    result = env.step(Action("type", {"x": 10, "y": 20, "text": "中文😀"}))
    gui.write.assert_not_called()
    assert sent == [(8, ctypes.sizeof(windows_input._INPUT),
                     [(1, 0, 0x4E2D, 4), (1, 0, 0x4E2D, 6),
                      (1, 0, 0x6587, 4), (1, 0, 0x6587, 6),
                      (1, 0, 0xD83D, 4), (1, 0, 0xD83D, 6),
                      (1, 0, 0xDE00, 4), (1, 0, 0xDE00, 6)])]
    assert calls == [("click", (10, 20), {}), ("unicode",), ("sleep", 0.3), ("screenshot",)]
    assert result.info["status"] == "executed"


def test_win32_input_structures_have_pointer_aligned_full_union():
    assert ctypes.sizeof(windows_input._INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(windows_input._KEYBDINPUT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


def test_partial_unicode_dispatch_is_uncertain_and_visible(desktop, monkeypatch):
    env, gui, calls = desktop
    user32 = SimpleNamespace(SendInput=Mock(return_value=1))
    monkeypatch.setattr(windows_input, "_load_user32", lambda: user32)
    result = env.step(Action("type", {"x": 10, "y": 20, "text": "中"}))
    assert result.info["status"] == "uncertain"
    assert result.info["dispatched"] is True
    assert "1/2" in result.info["error"]
    gui.write.assert_not_called()
    assert calls[-2:] == [("sleep", 0.3), ("screenshot",)]


def test_select_can_input_unicode_option(desktop, monkeypatch):
    env, gui, calls = desktop
    user32 = SimpleNamespace(SendInput=Mock(side_effect=lambda count, _events, _size: count))
    monkeypatch.setattr(windows_input, "_load_user32", lambda: user32)
    result = env.step(Action("select", {"x": 10, "y": 20, "option": "中文选项"}))
    user32.SendInput.assert_called_once()
    gui.write.assert_not_called()
    gui.press.assert_called_once_with("enter")
    assert calls == [("click", (10, 20), {}), ("sleep", 0.5), ("press", ("enter",), {}),
                     ("sleep", 0.3), ("screenshot",)]
    assert result.info["status"] == "executed"


@pytest.mark.parametrize("action", [Action("click", {"grounding_failed": True}),
                                    Action("click", {}), Action("type", {"text": "oops"}),
                                    Action("save"), Action("set_cell_values"),
                                    Action("unsupported")])
def test_invalid_or_unsupported_actions_are_rejected_without_dispatch(desktop, action):
    env, gui, calls = desktop
    result = env.step(action)
    assert result.info["status"] == "rejected"
    assert result.info["dispatched"] is False
    assert result.info["action"] == action.action
    assert result.info["error"]
    assert calls == [("screenshot",)]


def test_gui_exception_is_uncertain_instead_of_success(desktop):
    env, gui, calls = desktop
    gui.click.side_effect = RuntimeError("mouse failed")
    result = env.step(Action("click", {"x": 10, "y": 20}))
    assert result.info["status"] == "uncertain"
    assert result.info["dispatched"] is True
    assert "mouse failed" in result.info["error"]
    assert calls == [("sleep", 0.3), ("screenshot",)]


def test_wait_does_not_add_another_settle_delay(desktop):
    env, gui, calls = desktop
    result = env.step(Action("wait", {"seconds": 1.25}))
    assert calls == [("sleep", 1.25), ("screenshot",)]
    assert result.info["status"] == "executed"


def test_settle_delay_can_be_disabled(desktop):
    env, gui, calls = desktop
    env.action_settle_seconds = 0
    env.step(Action("press", {"key": "enter"}))
    assert calls == [("press", ("enter",), {}), ("screenshot",)]


def _window(hwnd, title, executable):
    return {"hwnd": hwnd, "title": title, "executable": executable}


@pytest.fixture
def windows(monkeypatch):
    inventory = [_window(101, "Untitled - Notepad", "notepad.exe"),
                 _window(102, "Downloads - File Explorer", "explorer.exe"),
                 _window(103, "Sample - Google Chrome", "chrome.exe")]
    monkeypatch.setattr(windows_input, "_enumerate_windows", lambda: inventory)
    user32 = SimpleNamespace(IsIconic=Mock(return_value=0), ShowWindow=Mock(),
                             SetForegroundWindow=Mock(), GetForegroundWindow=Mock(return_value=101))
    monkeypatch.setattr(windows_input, "_load_user32", lambda: user32)
    return inventory, user32


def test_switch_applications_activates_requested_executable_without_alt_tab(desktop, windows):
    env, gui, calls = desktop
    inventory, user32 = windows
    result = env.step(Action("switch_applications", {"app_code": "notepad"}))
    user32.SetForegroundWindow.assert_called_once_with(101)
    gui.hotkey.assert_not_called()
    assert result.info["status"] == "executed"
    assert result.info["window_executable"] == "notepad.exe"
    assert calls == [("sleep", 0.3), ("screenshot",)]


@pytest.mark.parametrize("app_code", ["missing", "mychrome"])
def test_missing_application_is_rejected_without_switching(desktop, windows, app_code):
    env, gui, calls = desktop
    inventory, user32 = windows
    result = env.step(Action("switch_applications", {"app_code": app_code}))
    assert result.info["status"] == "rejected"
    assert result.info["dispatched"] is False
    user32.SetForegroundWindow.assert_not_called()
    gui.hotkey.assert_not_called()
    assert calls == [("screenshot",)]


def test_ambiguous_windows_require_exact_title(desktop, windows):
    env, gui, calls = desktop
    inventory, user32 = windows
    inventory.append(_window(104, "Notes.txt - Notepad", "notepad.exe"))
    result = env.step(Action("switch_applications", {"app_code": "notepad"}))
    assert result.info["status"] == "rejected"
    assert result.info["dispatched"] is False
    assert result.info["matching_titles"] == ["Untitled - Notepad", "Notes.txt - Notepad"]
    user32.SetForegroundWindow.assert_not_called()
    user32.GetForegroundWindow.return_value = 104
    result = env.step(Action("switch_applications", {"app_code": "Notes.txt - Notepad"}))
    assert result.info["status"] == "executed"
    user32.SetForegroundWindow.assert_called_once_with(104)


def test_minimized_window_is_restored_and_failed_focus_is_uncertain(desktop, windows):
    env, gui, calls = desktop
    inventory, user32 = windows
    user32.IsIconic.return_value = 1
    user32.GetForegroundWindow.return_value = 102
    result = env.step(Action("switch_applications", {"app_code": "notepad"}))
    user32.ShowWindow.assert_called_once_with(101, 9)
    assert result.info["status"] == "uncertain"
    assert result.info["dispatched"] is True
    assert "未获得焦点" in result.info["error"]


def test_activation_exception_after_dispatch_is_uncertain(desktop, windows):
    env, gui, calls = desktop
    inventory, user32 = windows
    user32.SetForegroundWindow.side_effect = OSError("activation failed")
    result = env.step(Action("switch_applications", {"app_code": "notepad"}))
    assert result.info["status"] == "uncertain"
    assert result.info["dispatched"] is True
    assert "activation failed" in result.info["error"]
    gui.hotkey.assert_not_called()


def test_libreoffice_app_codes_distinguish_shared_process():
    inventory = [_window(101, "Sheet.ods - LibreOffice Calc", "soffice.bin"),
                 _window(102, "Text.odt - LibreOffice Writer", "soffice.bin")]
    assert windows_input._matching_windows(inventory, "libreoffice_calc") == inventory[:1]
    assert windows_input._matching_windows(inventory, "libreoffice_writer") == inventory[1:]
    assert windows_input._matching_windows(inventory, "libreoffice_impress") == []
