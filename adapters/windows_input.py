"""Windows desktop input and precise application activation.

PyAutoGUI handles ASCII keyboard input; Win32 Unicode input handles all other
text without relying on the current keyboard layout or an IME.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import PureWindowsPath
from typing import Any

from ..actions import Action


def to_pyautogui(action: Action) -> str:
    """生成 Windows 点击、滚动、按键、拖拽和等待命令。

    文本输入和应用切换由 WindowsEnvironment 的 Unicode/窗口接口处理。
    """
    args, name = action.args, action.action
    if args.get("grounding_failed"):
        return ""
    head = "import pyautogui; import time; "
    if name in ("click", "double_click", "right_click"):
        x, y = args.get("x"), args.get("y")
        if x is None or y is None or int(x) < 0 or int(y) < 0:
            return ""
        if name == "double_click":
            return f"{head}pyautogui.doubleClick({x}, {y})"
        suffix = ", button='right'" if name == "right_click" else ""
        return f"{head}pyautogui.click({x}, {y}{suffix})"
    if name == "scroll":
        amount = int(args.get("amount", 1)) * (1 if args.get("direction") == "up" else -1)
        return f"{head}pyautogui.scroll({amount})"
    if name == "press":
        return f"{head}pyautogui.press({str(args.get('key', ''))!r})"
    if name == "hotkey":
        return f"{head}pyautogui.hotkey(*{list(args.get('keys', []))!r})"
    if name == "drag_and_drop":
        if any(args.get(key) is None for key in ("x1", "y1", "x2", "y2")):
            return ""
        return (f"{head}pyautogui.moveTo({args['x1']}, {args['y1']}); "
                f"pyautogui.dragTo({args['x2']}, {args['y2']}, duration=1.0)")
    if name == "wait":
        return f"{head}time.sleep({float(args.get('seconds', 1))})"
    return ""


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                ("wParamH", ctypes.c_uint16)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_uint32), ("value", _INPUT_UNION)]


def _load_user32():
    if sys.platform != "win32":
        raise OSError("Windows desktop input requires Windows")
    return ctypes.WinDLL("user32", use_last_error=True)


def write_text(text: str, gui=None) -> None:
    """Send text to the focused control, preserving Unicode code points."""
    text = str(text)
    if not text:
        return
    if text.isascii():
        if gui is None:
            import pyautogui as gui
        gui.write(text)
        return
    user32 = _load_user32()
    user32.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = ctypes.c_uint32
    encoded = text.encode("utf-16-le")
    units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
    events = (_INPUT * (2 * len(units)))()
    for i, unit in enumerate(units):
        events[2 * i].type = events[2 * i + 1].type = 1  # INPUT_KEYBOARD
        events[2 * i].ki = _KEYBDINPUT(0, unit, 0x0004, 0, 0)  # KEYEVENTF_UNICODE
        events[2 * i + 1].ki = _KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, 0)
    inserted = user32.SendInput(len(events), events, ctypes.sizeof(_INPUT))
    if inserted != len(events):
        raise OSError(ctypes.get_last_error(),
                      f"SendInput inserted {inserted}/{len(events)} Unicode events")


def _enumerate_windows(*, include_untitled=False) -> list[dict[str, Any]]:
    """List visible top-level windows with executable names and full titles."""
    user32 = _load_user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hwnd_type = ctypes.c_void_p
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, hwnd_type, ctypes.c_ssize_t)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]
    user32.EnumWindows.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [hwnd_type]
    user32.IsWindowVisible.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [hwnd_type]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [hwnd_type, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [hwnd_type, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                   ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    windows = []

    @callback_type
    def collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return 1
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0 and not include_untitled:
            return 1
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        process_id = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        executable = ""
        process = kernel32.OpenProcess(0x1000, 0, process_id.value)  # QUERY_LIMITED_INFORMATION
        if process:
            try:
                path = ctypes.create_unicode_buffer(32768)
                size = ctypes.c_uint32(len(path))
                if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    executable = PureWindowsPath(path.value).name.casefold()
            finally:
                kernel32.CloseHandle(process)
        windows.append({"hwnd": hwnd, "pid": process_id.value,
                        "title": title.value, "executable": executable})
        return 1

    if not user32.EnumWindows(collect, 0):
        raise OSError(ctypes.get_last_error(), "Cannot enumerate desktop windows")
    return windows


_APP_EXECUTABLES = {
    "notepad": {"notepad.exe"}, "记事本": {"notepad.exe"},
    "chrome": {"chrome.exe"}, "google_chrome": {"chrome.exe"},
    "edge": {"msedge.exe"}, "msedge": {"msedge.exe"},
    "firefox": {"firefox.exe"},
    "file_explorer": {"explorer.exe"}, "explorer": {"explorer.exe"},
    "calculator": {"calculatorapp.exe", "calc.exe"},
    "excel": {"excel.exe"}, "microsoft_excel": {"excel.exe"},
    "word": {"winword.exe"}, "microsoft_word": {"winword.exe"},
    "powerpoint": {"powerpnt.exe"}, "microsoft_powerpoint": {"powerpnt.exe"},
    "vscode": {"code.exe"}, "visual_studio_code": {"code.exe"},
    "libreoffice": {"soffice.exe", "soffice.bin"},
}
_LIBREOFFICE_TITLES = {
    "libreoffice_calc": "libreoffice calc", "libreoffice_writer": "libreoffice writer",
    "libreoffice_impress": "libreoffice impress",
}


def _matching_windows(windows: list[dict], app_code: str) -> list[dict]:
    query = app_code.strip().casefold()
    exact_titles = [window for window in windows if window["title"].strip().casefold() == query]
    if exact_titles:
        return exact_titles
    alias = query.replace(" ", "_").replace("-", "_")
    if alias in _LIBREOFFICE_TITLES:
        suffix = _LIBREOFFICE_TITLES[alias]
        return [window for window in windows
                if window["executable"].casefold() in {"soffice.exe", "soffice.bin"}
                and (window["title"].casefold() == suffix
                     or window["title"].casefold().endswith(" - " + suffix))]
    executables = _APP_EXECUTABLES.get(alias)
    if executables is None:
        # Unregistered app codes still require an exact executable basename.
        executable = PureWindowsPath(query).name
        executables = {executable if executable.endswith((".exe", ".bin")) else executable + ".exe"}
    return [window for window in windows if window["executable"].casefold() in executables]


def foreground_window():
    user32 = _load_user32()
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return user32.GetForegroundWindow()


def window_process_id(hwnd):
    """Read the current owner of a full-width window handle; unknown returns zero."""
    try:
        user32 = _load_user32()
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        pid = ctypes.c_uint32()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return 0
        return pid.value
    except (OSError, AttributeError):
        return 0


def activate_application(app_code: str, work_screen=None, *, allow_browsers=True, presenter=None) -> dict:
    """Activate exactly one matching window; never substitute a blind Alt+Tab."""
    if not app_code or not app_code.strip():
        return {"status": "rejected", "dispatched": False, "error": "缺少目标应用 app_code"}
    candidates = _matching_windows(_enumerate_windows(), app_code)
    if not candidates:
        return {"status": "rejected", "dispatched": False,
                "error": f"找不到已打开的目标应用窗口: {app_code}"}
    if len(candidates) != 1:
        return {"status": "rejected", "dispatched": False,
                "error": f"目标应用有多个窗口，请用完整窗口标题指定: {app_code}",
                "matching_titles": [window["title"] for window in candidates]}
    window = candidates[0]
    if not allow_browsers and not window.get("executable"):
        return {"status": "rejected", "dispatched": False,
                "error": "无法确认目标应用进程身份，未激活该窗口"}
    if not allow_browsers and window["executable"].casefold() in {
            "chrome.exe", "chromium.exe", "msedge.exe", "firefox.exe", "brave.exe",
            "opera.exe", "vivaldi.exe", "safari.exe", "iexplore.exe"}:
        return {"status": "rejected", "dispatched": False,
                "error": "浏览器窗口须通过 goto 启动或激活本任务的无痕实例"}
    hwnd = window["hwnd"]
    if presenter is not None:
        pid = window.get("pid")
        if not pid:
            return {"status": "rejected", "dispatched": False,
                    "error": "无法确认目标应用窗口 PID，未激活该窗口"}
        result = dict(presenter.activate(hwnd, pid))
        result.update(window_title=window["title"], window_executable=window["executable"])
        if result.get("status") != "executed":
            return result
        if work_screen is not None and work_screen.detected and work_screen.monitor_count > 1:
            from .windows_display import move_window_to_work_screen
            if (foreground_window() != hwnd or window_process_id(hwnd) != pid
                    or not move_window_to_work_screen(hwnd, work_screen)
                    or foreground_window() != hwnd or window_process_id(hwnd) != pid):
                result.update(status="uncertain", dispatched=True,
                              error=f"目标窗口未能在主屏保持焦点: {window['title']}")
        return result
    user32 = _load_user32()
    for name in ("IsIconic", "SetForegroundWindow"):
        getattr(user32, name).argtypes = [ctypes.c_void_p]
        getattr(user32, name).restype = ctypes.c_int
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if work_screen is not None:
            from .windows_display import move_window_to_work_screen
            if not move_window_to_work_screen(hwnd, work_screen):
                return {"status": "uncertain", "dispatched": True,
                        "error": f"目标窗口未能移动到主屏工作区: {window['title']}"}
        user32.SetForegroundWindow(hwnd)
        focused = user32.GetForegroundWindow() == hwnd
    except Exception as exc:  # noqa: BLE001  The activation might have partially completed.
        return {"status": "uncertain", "dispatched": True,
                "error": f"激活目标窗口异常: {exc}"}
    if not focused:
        return {"status": "uncertain", "dispatched": True,
                "error": f"目标窗口未获得焦点: {window['title']}"}
    return {"status": "executed", "dispatched": True, "window_title": window["title"],
            "window_executable": window["executable"]}
