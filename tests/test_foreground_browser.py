"""Windows foreground process detection regressions; all Win32 calls are mocked."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osworld_agent import utils


@pytest.fixture
def win32(monkeypatch):
    def configure(executable=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  title="A page without a browser name"):
        def read_pid(_hwnd, pid):
            pid._obj.value = 123
            return 456

        def read_path(_process, _flags, path, length):
            path.value = executable
            length._obj.value = len(executable)
            return True

        def read_title(_hwnd, buffer, _length):
            buffer.value = title
            return len(title)

        user32 = SimpleNamespace(
            GetForegroundWindow=Mock(return_value=0x100000001),
            GetWindowThreadProcessId=Mock(side_effect=read_pid),
            GetWindowTextLengthW=Mock(return_value=len(title)),
            GetWindowTextW=Mock(side_effect=read_title),
        )
        kernel32 = SimpleNamespace(
            OpenProcess=Mock(return_value=0x200000002),
            QueryFullProcessImageNameW=Mock(side_effect=read_path),
            CloseHandle=Mock(return_value=True),
        )
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32, kernel32=kernel32),
                            raising=False)
        return user32, kernel32

    return configure


def test_utils_import_is_explicitly_1010():
    assert Path(utils.__file__).resolve().parent == Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("executable", ["code.exe", "mychrome.exe", "chrome-helper.exe", "browser.exe"])
def test_browser_words_in_editor_title_do_not_enable_browser_use(win32, executable):
    user32, kernel32 = win32("C:\\Tools\\" + executable,
                            title="chrome firefox edge browser notes - Visual Studio Code")

    assert utils.detect_foreground_browser("windows") is False
    user32.GetWindowTextLengthW.assert_not_called()
    user32.GetWindowTextW.assert_not_called()
    kernel32.CloseHandle.assert_called_once_with(0x200000002)


@pytest.mark.parametrize("executable", [
    "CHROME.EXE", "chromium.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "safari.exe", "iexplore.exe",
])
def test_browser_process_is_detected_without_browser_words_in_title(win32, executable):
    user32, kernel32 = win32("C:\\Program Files\\Browser\\" + executable)

    assert utils.detect_foreground_browser("windows") is True
    user32.GetWindowTextLengthW.assert_not_called()
    kernel32.OpenProcess.assert_called_once_with(0x1000, False, 123)
    kernel32.CloseHandle.assert_called_once_with(0x200000002)
    assert user32.GetForegroundWindow.restype is wintypes.HWND
    assert user32.GetWindowThreadProcessId.argtypes == [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.QueryFullProcessImageNameW.argtypes[0] is wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]


@pytest.mark.parametrize("failure", ["no_window", "no_thread", "no_pid", "open", "query", "query_exception"])
def test_probe_failure_returns_false_and_closes_only_opened_handles(win32, failure):
    user32, kernel32 = win32(title="Google Chrome")
    if failure == "no_window":
        user32.GetForegroundWindow.return_value = 0
    elif failure == "no_thread":
        user32.GetWindowThreadProcessId.side_effect = None
        user32.GetWindowThreadProcessId.return_value = 0
    elif failure == "no_pid":
        user32.GetWindowThreadProcessId.side_effect = None
        user32.GetWindowThreadProcessId.return_value = 456
    elif failure == "open":
        kernel32.OpenProcess.return_value = 0
    elif failure == "query":
        kernel32.QueryFullProcessImageNameW.side_effect = None
        kernel32.QueryFullProcessImageNameW.return_value = False
    else:
        kernel32.QueryFullProcessImageNameW.side_effect = OSError("process disappeared")

    assert utils.detect_foreground_browser("windows") is False
    user32.GetWindowTextLengthW.assert_not_called()
    if failure in ("query", "query_exception"):
        kernel32.CloseHandle.assert_called_once_with(0x200000002)
    else:
        kernel32.CloseHandle.assert_not_called()


def test_missing_process_query_api_fails_without_title_fallback(win32):
    user32, kernel32 = win32(title="Google Chrome")
    del kernel32.QueryFullProcessImageNameW

    assert utils.detect_foreground_browser("windows") is False
    user32.GetWindowTextLengthW.assert_not_called()
    kernel32.CloseHandle.assert_not_called()
