"""Native foreground activation with reversible, task-local window topmost state."""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
import threading
import uuid


_TOPMOST = 0x00000008
_POSITION_FLAGS = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE/NOMOVE/NOACTIVATE/NOOWNERZORDER
_MARK_PREFIX = "OSWorldAgent.WindowPresenter."
_RECOVERY_LOCK = threading.RLock()


def _journal_path(path):
    path = os.path.abspath(os.fspath(path))
    if os.path.basename(path).casefold() != "window_presentation.json":
        raise ValueError("Window recovery requires the task's window_presentation.json")
    if os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path):
        raise ValueError("Window recovery journal cannot be a symbolic link")
    return path


def _read_journal(path):
    path = _journal_path(path)
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read(8193)
    if len(text) > 8192:
        raise ValueError("Window recovery journal is too large")
    record = json.loads(text)
    if not isinstance(record, dict) or set(record) != {"version", "runner_pid", "mark", "window"}:
        raise ValueError("Invalid window recovery record")
    window = record["window"]
    if (type(record["version"]) is not int or record["version"] != 1
            or type(record["runner_pid"]) is not int or not 0 < record["runner_pid"] <= 0xffffffff
            or not isinstance(record["mark"], str)
            or re.fullmatch(re.escape(_MARK_PREFIX) + r"[0-9a-f]{32}", record["mark"]) is None
            or not isinstance(window, dict) or set(window) != {"hwnd", "pid", "topmost"}
            or type(window["hwnd"]) is not int or not 0 < window["hwnd"] < 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)
            or type(window["pid"]) is not int or not 0 < window["pid"] <= 0xffffffff
            or type(window["topmost"]) is not bool):
        raise ValueError("Invalid window recovery identity or style")
    return record


def _native_process_dead(pid):
    """Return True only when Win32 confirms absence or a signalled process handle."""
    if sys.platform != "win32":
        return None
    kernel32 = None
    handle = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE, no termination rights.
        if not handle:
            return True if getattr(ctypes, "get_last_error", lambda: 0)() == 87 else None
        result = kernel32.WaitForSingleObject(handle, 0)
        return True if result == 0 else False if result == 258 else None
    except Exception:
        return None
    finally:
        if handle and kernel32 is not None:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass


def recover_window_presentation(path, expected_runner_pid=None) -> bool:
    """Recover a marked window only after its recorded runner has exited."""
    with _RECOVERY_LOCK:
        try:
            path = _journal_path(path)
            if not os.path.exists(path):
                return True
            record = _read_journal(path)
            if (expected_runner_pid is not None
                    and (type(expected_runner_pid) is not int
                         or expected_runner_pid != record["runner_pid"])):
                return False
            if _native_process_dead(record["runner_pid"]) is not True:
                return False
            presenter = WindowPresenter()
            presenter._mark = record["mark"]
            presenter._state = record["window"]
            presenter._recovery_path = path
            presenter._journal_record = record
            return presenter.restore()
        except Exception:
            return False


def _load_apis():
    if sys.platform != "win32":
        raise OSError("Window presentation requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hwnd = ctypes.c_void_p
    signatures = {
        "IsWindow": ([hwnd], ctypes.c_int),
        "IsIconic": ([hwnd], ctypes.c_int),
        "ShowWindow": ([hwnd, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([hwnd, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32),
        "SetWindowPos": ([hwnd, hwnd, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_uint32], ctypes.c_int),
        "SetForegroundWindow": ([hwnd], ctypes.c_int),
        "GetForegroundWindow": ([], hwnd),
        "AttachThreadInput": ([ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int], ctypes.c_int),
        "SetPropW": ([hwnd, ctypes.c_wchar_p, hwnd], ctypes.c_int),
        "GetPropW": ([hwnd, ctypes.c_wchar_p], hwnd),
        "RemovePropW": ([hwnd, ctypes.c_wchar_p], hwnd),
    }
    for name, (args, result) in signatures.items():
        api = getattr(user32, name)
        api.argtypes, api.restype = args, result
    style = getattr(user32, "GetWindowLongPtrW", None)
    if style is None:  # On 32-bit Windows the header aliases this to GetWindowLongW.
        style = user32.GetWindowLongW
    style.argtypes, style.restype = [hwnd, ctypes.c_int], ctypes.c_ssize_t
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
    return user32, kernel32, style


class WindowPresenter:
    """Keep one explicitly selected window above normal windows until restored.

    A private Win32 property distinguishes the original HWND from a subsequently
    reused handle, even if that handle belongs to the same process.
    """

    def __init__(self, recovery_path=None):
        self._mark = _MARK_PREFIX + uuid.uuid4().hex
        self._state = None
        self._recovery_path = None
        self._journal_record = None
        if recovery_path is not None:
            with _RECOVERY_LOCK:
                path = _journal_path(recovery_path)
                if os.path.exists(path) and not recover_window_presentation(path):
                    raise OSError("Previous task window presentation cannot be safely recovered")
                self._recovery_path = path

    def _save_journal(self, state):
        if self._recovery_path is None:
            return
        path = _journal_path(self._recovery_path)
        if os.path.exists(path) and _read_journal(path) != self._journal_record:
            raise OSError("Window presentation journal belongs to another task")
        record = {"version": 1, "runner_pid": os.getpid(), "mark": self._mark,
                  "window": dict(state)}
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".window_presentation.", suffix=".tmp", dir=os.path.dirname(path))
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            _journal_path(path)
            os.replace(temporary, path)
            self._journal_record = record
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _clear_journal(self):
        if self._recovery_path is None or self._journal_record is None:
            return True
        try:
            path = _journal_path(self._recovery_path)
            if os.path.exists(path):
                if _read_journal(path) != self._journal_record:
                    return False
                _journal_path(path)
                os.unlink(path)
            self._journal_record = None
            return True
        except Exception:
            return False

    def _restored(self):
        if not self._clear_journal():
            return False
        self._state = None
        return True

    @staticmethod
    def _identity(user32, hwnd, pid):
        owner = ctypes.c_uint32()
        return (bool(user32.IsWindow(hwnd))
                and bool(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)))
                and owner.value == pid)

    @staticmethod
    def _allowed(validate):
        try:
            return validate is None or bool(validate())
        except Exception:
            return False

    @staticmethod
    def _is_topmost(style, hwnd):
        clear = getattr(ctypes, "set_last_error", None)
        if clear:
            clear(0)
        value = style(hwnd, -20)  # GWL_EXSTYLE
        if value == 0 and getattr(ctypes, "get_last_error", lambda: 0)():
            raise OSError("Cannot read target window style")
        return bool(value & _TOPMOST)

    def _marked_identity(self, user32, state):
        return (self._identity(user32, state["hwnd"], state["pid"])
                and user32.GetPropW(state["hwnd"], self._mark) == 1)

    def restore(self) -> bool:
        """Restore the original topmost state without changing foreground focus."""
        with _RECOVERY_LOCK:
            return self._restore()

    def _restore(self):
        state = self._state
        if state is None:
            return self._clear_journal()
        try:
            user32, _, style = _load_apis()
            if not self._marked_identity(user32, state):
                return self._restored()  # Destroyed/reused HWND: never modify it.
            hwnd = state["hwnd"]
            original = state["topmost"]
            if self._is_topmost(style, hwnd) != original:
                if not self._marked_identity(user32, state):
                    return self._restored()
                if not user32.SetWindowPos(hwnd, -1 if original else -2,
                                          0, 0, 0, 0, _POSITION_FLAGS):
                    return False
                if not self._marked_identity(user32, state):
                    return self._restored()
                if self._is_topmost(style, hwnd) != original:
                    return False
            if not self._marked_identity(user32, state):
                return self._restored()
            if user32.RemovePropW(hwnd, self._mark) != 1:
                return False
            return self._restored()
        except Exception:
            # Keep the record after an API failure so close()/reset() can retry.
            return False

    def activate(self, hwnd: int, pid: int, *, validate=None) -> dict:
        """Present an exact HWND/PID; reject input when native focus is unconfirmed."""
        with _RECOVERY_LOCK:
            return self._activate(hwnd, pid, validate=validate)

    def _activate(self, hwnd, pid, *, validate=None):
        dispatched = False
        user32 = style = None
        try:
            hwnd, pid = int(hwnd), int(pid)
            if hwnd <= 0 or pid <= 0:
                raise ValueError("Invalid target HWND/PID")
            user32, kernel32, style = _load_apis()

            def guard():
                if not self._allowed(validate) or not self._identity(user32, hwnd, pid):
                    raise OSError("Target window identity or ownership is no longer valid")
                if (self._state is not None and self._state["hwnd"] == hwnd
                        and self._state["pid"] == pid
                        and not self._marked_identity(user32, self._state)):
                    raise OSError("Target window task marker is no longer valid")

            def mutate(api, *args):
                nonlocal dispatched
                guard()
                dispatched = True
                return api(*args)

            guard()
            state = self._state
            if state is not None and (state["hwnd"], state["pid"]) != (hwnd, pid):
                dispatched = True
                if not self.restore():
                    raise OSError("Cannot restore the previous task window")
                guard()
            if self._state is None:
                original = self._is_topmost(style, hwnd)
                guard()
                state = {"hwnd": hwnd, "pid": pid, "topmost": original}
                self._save_journal(state)  # Must be durable before setting the task marker.
                guard()
                # SetProp itself must not require a marker that is not installed yet.
                dispatched = True
                self._state = state
                if not user32.SetPropW(hwnd, self._mark, 1):
                    raise OSError("Cannot mark target window for task-local presentation")
                guard()
            if user32.IsIconic(hwnd):
                mutate(user32.ShowWindow, hwnd, 9)  # SW_RESTORE; return is prior visibility.
                if user32.IsIconic(hwnd):
                    raise OSError("Target window remains minimized")
            if not mutate(user32.SetWindowPos, hwnd, -1, 0, 0, 0, 0, _POSITION_FLAGS):
                raise OSError("Cannot place target window in the topmost group")
            guard()
            if not self._is_topmost(style, hwnd):
                raise OSError("Target window topmost state was not established")
            mutate(user32.SetForegroundWindow, hwnd)
            if user32.GetForegroundWindow() != hwnd:
                current = kernel32.GetCurrentThreadId()
                foreground = user32.GetForegroundWindow()
                thread_ids = [user32.GetWindowThreadProcessId(foreground, None) if foreground else 0,
                              user32.GetWindowThreadProcessId(hwnd, None)]
                attached = []
                detach_failed = False
                try:
                    for thread in dict.fromkeys(thread_ids):
                        if current and thread and current != thread:
                            if mutate(user32.AttachThreadInput, current, thread, True):
                                attached.append((current, thread))
                    mutate(user32.SetForegroundWindow, hwnd)
                finally:
                    for pair in reversed(attached):
                        try:
                            if not user32.AttachThreadInput(*pair, False):
                                detach_failed = True
                        except Exception:
                            detach_failed = True
                if detach_failed:
                    raise OSError("Cannot detach temporary window input queues")
            guard()
            if user32.GetForegroundWindow() != hwnd:
                raise OSError("Target window did not obtain foreground focus")
            if not self._is_topmost(style, hwnd):
                raise OSError("Target window no longer has topmost state")
            guard()
            return {"status": "executed", "dispatched": True,
                    "hwnd": hwnd, "pid": pid, "topmost": True}
        except Exception as exc:
            restored = True
            if dispatched and self._state is not None:
                if (self._state["hwnd"], self._state["pid"]) == (hwnd, pid):
                    restored = self.restore()
                else:
                    restored = False  # A failed previous-target restore is retried by cleanup.
            elif self._state is None:
                restored = self._clear_journal()
            topmost = False
            try:
                if user32 is not None and self._identity(user32, hwnd, pid):
                    topmost = self._is_topmost(style, hwnd)
            except Exception:
                pass
            error = f"无法置顶并激活目标窗口: {exc}"
            if not restored:
                error += "；窗口原置顶状态未恢复，将在清理时重试"
            return {"status": "uncertain" if dispatched else "rejected",
                    "dispatched": dispatched, "hwnd": hwnd, "pid": pid,
                    "topmost": topmost, "error": error}
