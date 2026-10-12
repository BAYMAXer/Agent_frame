"""Task-local primary display selection; never change Windows display settings."""
from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkScreen:
    left: int
    top: int
    width: int
    height: int
    work_area: tuple[int, int, int, int]  # Win32 RECT: left, top, right, bottom
    device: str = ""
    monitor_count: int = 1
    monitor_handle: Any = None
    detected: bool = True

    @property
    def region(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.width, self.height

    def to_dict(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width,
                "height": self.height, "work_area": list(self.work_area),
                "device": self.device, "monitor_count": self.monitor_count,
                "detected": self.detected}


class _RECT(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", ctypes.c_uint32), ("szDevice", ctypes.c_wchar * 32)]


def _load_user32():
    if sys.platform != "win32":
        raise OSError("Windows display APIs require Windows")
    return ctypes.WinDLL("user32", use_last_error=True)


def _configure(api, args, result=ctypes.c_int):
    api.argtypes, api.restype = args, result
    return api


def _last_error() -> int:
    return getattr(ctypes, "get_last_error", lambda: 0)()


def initialize_dpi_awareness() -> dict:
    """Call before importing PyAutoGUI; return the awareness that was established."""
    if sys.platform != "win32":
        return {"configured": False, "status": "unavailable", "reason": "not_windows"}
    failures = []
    try:
        user32 = _load_user32()
    except OSError as exc:
        return {"configured": False, "status": "failed", "error": str(exc)}
    # A manifest or an earlier import can prevent changing the process default.
    # Changing the calling thread still makes this runner's coordinates physical.
    for name, scope in (("SetProcessDpiAwarenessContext", "process"),
                        ("SetThreadDpiAwarenessContext", "thread")):
        try:
            api = _configure(getattr(user32, name), [ctypes.c_void_p],
                             ctypes.c_void_p if scope == "thread" else ctypes.c_int)
            if api(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
                return {"configured": True, "status": "per_monitor_v2", "scope": scope}
            failures.append(f"{name}: Win32 error {_last_error()}")
        except (AttributeError, OSError) as exc:
            failures.append(f"{name}: {exc}")
    try:
        if _configure(user32.SetProcessDPIAware, [])():
            return {"configured": True, "status": "system_aware", "scope": "process"}
        failures.append(f"SetProcessDPIAware: Win32 error {_last_error()}")
    except (AttributeError, OSError) as exc:
        failures.append(f"SetProcessDPIAware: {exc}")
    error = "; ".join(failures)
    log.warning("无法统一 Windows DPI 坐标: %s", error)
    return {"configured": False, "status": "failed", "error": error}


def _enumerate_monitors(user32) -> list[dict]:
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(_RECT), ctypes.c_ssize_t)
    get_info = _configure(user32.GetMonitorInfoW,
                          [ctypes.c_void_p, ctypes.POINTER(_MONITORINFOEXW)])
    monitors, errors = [], []

    @callback_type
    def collect(handle, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not get_info(handle, ctypes.byref(info)):
            errors.append(_last_error())
            return 0
        monitors.append({"handle": handle, "primary": bool(info.dwFlags & 1),
                         "rect": tuple(getattr(info.rcMonitor, n) for n, _ in _RECT._fields_),
                         "work_area": tuple(getattr(info.rcWork, n) for n, _ in _RECT._fields_),
                         "device": info.szDevice})
        return 1

    enum = _configure(user32.EnumDisplayMonitors,
                      [ctypes.c_void_p, ctypes.POINTER(_RECT), callback_type, ctypes.c_ssize_t])
    if not enum(None, None, collect, 0) or errors:
        raise OSError(errors[0] if errors else _last_error(), "Cannot enumerate display monitors")
    return monitors


def detect_primary_work_screen(fallback_width=1920, fallback_height=1080) -> WorkScreen:
    """Refresh the layout for every task; enumeration order never chooses the screen."""
    width, height = max(1, int(fallback_width)), max(1, int(fallback_height))
    fallback = WorkScreen(0, 0, width, height, (0, 0, width, height), detected=False)
    if sys.platform != "win32":
        return fallback
    try:
        user32 = _load_user32()
        primary = next(m for m in _enumerate_monitors(user32) if m["primary"])
        left, top, right, bottom = primary["rect"]
        wl, wt, wr, wb = primary["work_area"]
        if right <= left or bottom <= top or wr <= wl or wb <= wt:
            raise ValueError("Invalid primary monitor rectangle")
        count = _configure(user32.GetSystemMetrics, [ctypes.c_int])(80)  # SM_CMONITORS
        if count < 1:
            raise OSError("Cannot determine display monitor count")
        return WorkScreen(left, top, right - left, bottom - top, (wl, wt, wr, wb),
                          device=primary["device"], monitor_count=count,
                          monitor_handle=primary["handle"])
    except (OSError, ValueError, StopIteration, AttributeError) as exc:
        log.warning("主屏检测失败，回退至 %sx%s: %s", width, height, exc)
        return fallback


def move_window_to_work_screen(hwnd, screen: WorkScreen) -> bool:
    """Place only the given window on the primary work area; True includes no-op."""
    if screen.monitor_count <= 1:
        return True
    if not hwnd or not screen.detected or not screen.monitor_handle:
        return False
    user32, was_maximized = None, False
    try:
        user32 = _load_user32()
        monitor = _configure(user32.MonitorFromWindow,
                             [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p)
        current = monitor(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if current == screen.monitor_handle:
            return True
        if not current:
            return False
        iconic = _configure(user32.IsIconic, [ctypes.c_void_p])
        zoomed = _configure(user32.IsZoomed, [ctypes.c_void_p])
        show = _configure(user32.ShowWindow, [ctypes.c_void_p, ctypes.c_int])
        was_maximized = bool(zoomed(hwnd))
        if iconic(hwnd) or was_maximized:
            show(hwnd, 9)  # SW_RESTORE; its return value is previous visibility, not success.
            if iconic(hwnd):
                return False
            # A minimized window can restore directly to its previous maximized state.
            if zoomed(hwnd):
                was_maximized = True
                show(hwnd, 1)  # SW_SHOWNORMAL before positioning the restored bounds.
                if iconic(hwnd) or zoomed(hwnd):
                    return False
        rect = _RECT()
        if not _configure(user32.GetWindowRect,
                          [ctypes.c_void_p, ctypes.POINTER(_RECT)])(hwnd, ctypes.byref(rect)):
            return False
        wl, wt, wr, wb = screen.work_area
        if wr <= wl or wb <= wt or rect.right <= rect.left or rect.bottom <= rect.top:
            return False
        width, height = min(rect.right - rect.left, wr - wl), min(rect.bottom - rect.top, wb - wt)
        x, y = wl + (wr - wl - width) // 2, wt + (wb - wt - height) // 2
        position = _configure(user32.SetWindowPos,
                              [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint32])
        if not position(hwnd, None, x, y, width, height, 0x0004 | 0x0010):  # NOZORDER | NOACTIVATE
            return False
        return monitor(hwnd, 2) == screen.monitor_handle
    except (OSError, AttributeError) as exc:
        log.warning("窗口无法移至主屏: %s", exc)
        return False
    finally:
        if user32 is not None and was_maximized:
            try:
                user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE, preserving the original state.
            except (OSError, AttributeError):
                log.warning("窗口移至主屏后未能恢复最大化状态")
