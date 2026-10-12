"""Local Chrome lifecycle regressions; no desktop, browser or model is started.

The pytest launcher must bind ``osworld_agent`` to this checkout before collection.
The source-path assertion below prevents accidentally testing the sibling 1009 copy.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osworld_agent import run_windows_local as runner
from osworld_agent import utils


@pytest.fixture
def profile_root(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    return tmp_path


def _profile(root, port=9222, suffix="owned-session"):
    return str(root / f"osworld_agent_chrome_incognito_{port}_{suffix}")


def _process(pid, profile, ports=(), quoted_value=True):
    arg = (f'--user-data-dir="{profile}"' if quoted_value
           else f'"--user-data-dir={profile}"')
    return {"pid": pid,
            "command_line": f'"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" {arg}',
            "ports": list(ports)}


def test_runner_import_is_explicitly_1010():
    assert Path(runner.__file__).resolve().parent == Path(__file__).resolve().parents[1]
    assert Path(utils.__file__).resolve().parent == Path(__file__).resolve().parents[1]


def test_confirmed_incognito_is_reused_without_starting(monkeypatch):
    monkeypatch.setattr(utils, "list_cdp_pages", lambda *_args: [{"id": "page"}])
    monkeypatch.setattr(utils, "cdp_pages_are_incognito", lambda *_args: True)
    popen = Mock(side_effect=AssertionError("reuse must not launch Chrome"))
    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    assert runner.ensure_chrome(9222, "unused.exe") == (False, None)
    popen.assert_not_called()


@pytest.mark.parametrize("mode", [False, None])
def test_existing_normal_or_unconfirmed_browser_is_rejected(monkeypatch, mode):
    monkeypatch.setattr(utils, "list_cdp_pages", lambda *_args: [{"id": "page"}])
    monkeypatch.setattr(utils, "cdp_pages_are_incognito", lambda *_args: mode)
    popen = Mock(side_effect=AssertionError("occupied port must not launch Chrome"))
    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    with pytest.raises(RuntimeError, match="9222"):
        runner.ensure_chrome(9222, "unused.exe")
    popen.assert_not_called()


def test_new_sessions_use_incognito_flags_and_distinct_owned_profiles(monkeypatch, profile_root):
    calls = []
    page_calls = iter([[], [{"id": "page"}], [], [{"id": "page"}]])
    monkeypatch.setattr(utils, "list_cdp_pages", lambda *_args: next(page_calls))
    monkeypatch.setattr(utils, "cdp_pages_are_incognito", lambda *_args: True)
    monkeypatch.setattr(runner.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda args, **kwargs: calls.append((args, kwargs)) or Mock(poll=lambda: None))
    def processes():
        profile = next(arg.split("=", 1)[1] for arg in calls[-1][0]
                       if arg.startswith("--user-data-dir="))
        return [_process(80101, profile, [9222])]
    monkeypatch.setattr(runner, "_chrome_processes", processes)

    first = runner.ensure_chrome(9222, "fake-chrome.exe")
    second = runner.ensure_chrome(9222, "fake-chrome.exe")

    assert first[0] is True and second[0] is True
    assert first[1] != second[1]
    for (args, _kwargs), (_started, profile) in zip(calls, [first, second]):
        assert "--incognito" in args and "--new-window" in args
        assert "--remote-debugging-port=9222" in args
        assert f"--user-data-dir={profile}" in args
        assert args[-1] == "about:blank"
        assert Path(profile).parent == profile_root
        assert runner._validated_profile(9222, profile) == os.path.realpath(profile)


def test_foreign_incognito_listener_cannot_complete_fresh_startup(monkeypatch, profile_root):
    page_calls = [0]
    def pages(*_args):
        page_calls[0] += 1
        return [{"id": "foreign-incognito-page"}] if page_calls[0] > 1 else []
    monkeypatch.setattr(utils, "list_cdp_pages", pages)
    monkeypatch.setattr(utils, "cdp_pages_are_incognito", lambda *_args: True)
    monkeypatch.setattr(runner.os.path, "isfile", lambda _path: True)
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    foreign_profile = _profile(profile_root, suffix="foreign-incognito-session")
    monkeypatch.setattr(runner, "_chrome_processes",
                        lambda: [_process(80201, foreign_profile, [9222])])
    cleanup = Mock()
    monkeypatch.setattr(runner, "close_test_chrome", cleanup)

    with pytest.raises(RuntimeError, match="9222"):
        runner.ensure_chrome(9222, "fake-chrome.exe")

    own_profile = next(arg.split("=", 1)[1] for arg in popen.call_args.args[0]
                       if arg.startswith("--user-data-dir="))
    assert own_profile != foreign_profile
    process.terminate.assert_called_once()
    cleanup.assert_called_once_with(9222, own_profile)


@pytest.mark.parametrize("pages_ready", [False, True])
def test_startup_without_confirmed_incognito_cleans_up_and_raises(monkeypatch, profile_root,
                                                               pages_ready):
    page_calls = [0]
    def pages(*_args):
        page_calls[0] += 1
        return [{"id": "page"}] if pages_ready and page_calls[0] > 1 else []
    monkeypatch.setattr(utils, "list_cdp_pages", pages)
    monkeypatch.setattr(utils, "cdp_pages_are_incognito", lambda *_args: False)
    monkeypatch.setattr(runner.os.path, "isfile", lambda _path: True)
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    cleanup = Mock()
    monkeypatch.setattr(runner, "close_test_chrome", cleanup)

    with pytest.raises(RuntimeError, match="9222"):
        runner.ensure_chrome(9222, "fake-chrome.exe")

    cleanup.assert_called_once()
    port, profile = cleanup.call_args.args
    assert port == 9222
    assert runner._validated_profile(port, profile) == os.path.realpath(profile)


def test_process_launch_exception_cleans_allocated_profile(monkeypatch, profile_root):
    monkeypatch.setattr(utils, "list_cdp_pages", lambda *_args: [])
    monkeypatch.setattr(runner.os.path, "isfile", lambda _path: True)
    error = OSError("fake process creation failure")
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(side_effect=error))
    cleanup = Mock()
    monkeypatch.setattr(runner, "close_test_chrome", cleanup)

    with pytest.raises(OSError) as caught:
        runner.ensure_chrome(9222, "fake-chrome.exe")
    assert caught.value is error
    cleanup.assert_called_once()
    port, profile = cleanup.call_args.args
    assert port == 9222
    assert runner._validated_profile(port, profile) == os.path.realpath(profile)


def test_profile_validation_rejects_unowned_and_non_directories(profile_root):
    valid = _profile(profile_root)
    assert runner._validated_profile(9222, valid) == os.path.realpath(valid)
    invalid = [None, str(profile_root), str(profile_root / "normal-profile"),
               str(profile_root / "osworld_agent_chrome_incognito_9222_"),
               _profile(profile_root, port=9223),
               _profile(profile_root / "nested"),
               _profile(profile_root.parent)]
    for path in invalid:
        assert runner._validated_profile(9222, path) is None, path


def test_process_matching_handles_spaces_quotes_case_and_same_prefix(profile_root):
    # Use an extra space even if the pytest temp root has none.
    profile = _profile(profile_root, suffix="session with spaces")
    processes = [_process(80101, profile, [9222]),
                 _process(80102, profile, quoted_value=False),
                 _process(80103, profile.upper()),
                 _process(80201, profile + "-different", [9223]),
                 _process(80202, _profile(profile_root, port=9223), [9223]),
                 {"pid": 80203, "command_line": "chrome.exe --type=renderer", "ports": []},
                 {"pid": 80204, "command_line": None, "ports": []},
                 {"pid": 80205, "command_line": f"chrome.exe --user-data-dir={profile}",
                  "ports": []}]
    assert {proc["pid"] for proc in runner._profile_processes(profile, processes)} == {
        80101, 80102, 80103}


def _fake_playwright(monkeypatch):
    session = SimpleNamespace(send=Mock())
    browser = SimpleNamespace(new_browser_cdp_session=Mock(return_value=session), close=Mock())
    chromium = SimpleNamespace(connect_over_cdp=Mock(return_value=browser))
    runtime = SimpleNamespace(chromium=chromium)
    context = Mock()
    context.__enter__ = Mock(return_value=runtime)
    context.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=context)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=factory))
    return factory, chromium, session


@pytest.mark.parametrize("inventory", [None, [], "foreign"])
def test_cdp_close_refuses_unknown_or_foreign_listener(monkeypatch, profile_root, inventory):
    profile = _profile(profile_root)
    if inventory == "foreign":
        inventory = [_process(80101, profile, []),
                     _process(80201, _profile(profile_root, port=9223), [9222])]
    monkeypatch.setattr(runner, "_chrome_processes", lambda: inventory)
    factory, _chromium, session = _fake_playwright(monkeypatch)

    assert runner._close_owned_cdp(9222, profile) is False
    factory.assert_not_called()
    session.send.assert_not_called()


def test_cdp_close_refuses_port_shared_by_owned_and_foreign_pids(monkeypatch, profile_root):
    profile = _profile(profile_root)
    monkeypatch.setattr(runner, "_chrome_processes",
                        lambda: [_process(80101, profile, [9222]),
                                 _process(80201, _profile(profile_root, port=9223), [9222])])
    factory, _chromium, session = _fake_playwright(monkeypatch)

    assert runner._close_owned_cdp(9222, profile) is False
    factory.assert_not_called()
    session.send.assert_not_called()


def test_cdp_close_sends_browser_close_only_to_owned_listener(monkeypatch, profile_root):
    profile = _profile(profile_root)
    monkeypatch.setattr(runner, "_chrome_processes",
                        lambda: [_process(80101, profile, [9222]),
                                 _process(80201, _profile(profile_root, port=9223), [9223])])
    _factory, chromium, session = _fake_playwright(monkeypatch)

    assert runner._close_owned_cdp(9222, profile) is True
    chromium.connect_over_cdp.assert_called_once()
    assert chromium.connect_over_cdp.call_args.args == ("http://localhost:9222",)
    session.send.assert_called_once_with("Browser.close")


def test_cdp_close_rechecks_listener_before_sending(monkeypatch, profile_root):
    profile = _profile(profile_root)
    inventories = iter([[_process(80101, profile, [9222])],
                        [_process(80201, _profile(profile_root, port=9223), [9222])]])
    monkeypatch.setattr(runner, "_chrome_processes", lambda: next(inventories))
    _factory, _chromium, session = _fake_playwright(monkeypatch)

    assert runner._close_owned_cdp(9222, profile) is False
    session.send.assert_not_called()


def test_cdp_close_refuses_foreign_listener_appearing_after_connect(monkeypatch, profile_root):
    profile = _profile(profile_root)
    owned = _process(80101, profile, [9222])
    inventories = iter([[owned], [owned, _process(80201, _profile(profile_root, port=9223), [9222])]])
    monkeypatch.setattr(runner, "_chrome_processes", lambda: next(inventories))
    _factory, _chromium, session = _fake_playwright(monkeypatch)

    assert runner._close_owned_cdp(9222, profile) is False
    session.send.assert_not_called()


def test_force_stop_targets_only_exact_profile_pids(monkeypatch, profile_root):
    profile = _profile(profile_root, suffix="session with spaces")
    owned = _process(80101, profile, [9222])
    other = _process(80201, profile + "-other", [9223])
    stopped = []
    monkeypatch.setattr(runner, "_chrome_processes", lambda: [other] if stopped else [owned, other])
    def run(command, **kwargs):
        # Emulate only the process selection performed by this native PowerShell call.
        pattern = kwargs["env"]["OSWORLD_AGENT_PROFILE_PATTERN"]
        stopped.extend(proc["pid"] for proc in [owned, other]
                       if re.search(pattern, proc["command_line"], flags=re.IGNORECASE))
        assert profile not in command[-1]  # profile data is never interpolated into shell code
        assert "-like" not in command[-1] and "*osworld_agent_chrome*" not in command[-1]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr(runner.subprocess, "run", run)

    assert runner._stop_profile_processes(profile) is True
    assert stopped == [80101]


def test_unknown_process_inventory_never_force_stops(monkeypatch, profile_root):
    monkeypatch.setattr(runner, "_chrome_processes", lambda: None)
    stop = Mock()
    monkeypatch.setattr(runner.subprocess, "run", stop)

    assert runner._stop_profile_processes(_profile(profile_root)) is False
    stop.assert_not_called()


@pytest.mark.parametrize("inventory", [None, "owned", []])
def test_profile_removal_requires_confirmed_process_exit(monkeypatch, profile_root, inventory):
    profile = _profile(profile_root)
    Path(profile).mkdir()
    marker = Path(profile) / "Cookies"
    marker.write_text("test cookie", encoding="utf-8")
    if inventory == "owned":
        inventory = [_process(80101, profile, [9222])]
    monkeypatch.setattr(runner, "_chrome_processes", lambda: inventory)

    removed = runner._remove_profile(9222, profile)
    assert removed is (inventory == [])
    assert marker.exists() is (inventory != [])


def test_profile_removal_rejects_unowned_directory_before_process_probe(monkeypatch, profile_root):
    unowned = profile_root / "normal-chrome-profile"
    unowned.mkdir()
    marker = unowned / "Cookies"
    marker.write_text("keep this profile", encoding="utf-8")
    probe = Mock(side_effect=AssertionError("unowned directory must not reach process lookup"))
    monkeypatch.setattr(runner, "_chrome_processes", probe)

    assert runner._remove_profile(9222, str(unowned)) is False
    assert marker.read_text(encoding="utf-8") == "keep this profile"
    probe.assert_not_called()


@pytest.mark.parametrize("invalid", [None, "root", "normal", "other-port"])
def test_close_without_valid_owned_profile_does_nothing(monkeypatch, profile_root, invalid):
    profiles = {None: None, "root": str(profile_root), "normal": str(profile_root / "Chrome"),
                "other-port": _profile(profile_root, port=9223)}
    probes = [Mock(side_effect=AssertionError("invalid profile must not trigger cleanup"))
              for _ in range(4)]
    for name, probe in zip(["_close_owned_cdp", "_chrome_processes",
                            "_stop_profile_processes", "_remove_profile"], probes):
        monkeypatch.setattr(runner, name, probe)

    runner.close_test_chrome(9222, profiles[invalid])
    for probe in probes:
        probe.assert_not_called()


@pytest.mark.parametrize("cdp_succeeded", [False, True])
def test_close_falls_back_when_cdp_fails_or_owned_process_survives(monkeypatch, profile_root,
                                                               cdp_succeeded):
    profile = _profile(profile_root)
    owned = _process(80101, profile, [9222])
    other = _process(80201, _profile(profile_root, port=9223), [9223])
    stopped = []
    monkeypatch.setattr(runner, "_close_owned_cdp", lambda port, path: cdp_succeeded)
    monkeypatch.setattr(runner, "_chrome_processes", lambda: [other] if stopped else [owned, other])
    def stop(path):
        stopped.append(path)
        return True
    monkeypatch.setattr(runner, "_stop_profile_processes", stop)
    remove = Mock(return_value=True)
    monkeypatch.setattr(runner, "_remove_profile", remove)

    runner.close_test_chrome(9222, profile)
    assert stopped == [profile]
    remove.assert_called_once_with(9222, profile)


def test_successful_close_does_not_force_stop_remaining_foreign_browser(monkeypatch, profile_root):
    profile = _profile(profile_root)
    monkeypatch.setattr(runner, "_close_owned_cdp", lambda *_args: True)
    monkeypatch.setattr(runner, "_chrome_processes",
                        lambda: [_process(80201, _profile(profile_root, port=9223), [9223])])
    stop = Mock(side_effect=AssertionError("only the foreign browser remains"))
    monkeypatch.setattr(runner, "_stop_profile_processes", stop)
    remove = Mock(return_value=True)
    monkeypatch.setattr(runner, "_remove_profile", remove)

    runner.close_test_chrome(9222, profile)
    stop.assert_not_called()
    remove.assert_called_once_with(9222, profile)


def _stub_main(monkeypatch, profile, *, started=True, failing=None, browser_requested=False):
    events = []
    error = RuntimeError(f"test failure in {failing}")
    result = SimpleNamespace(score=1, steps=1, success=True, final_answer="done",
                             trajectory=[], screenshots={})
    def checkpoint(name):
        events.append(name)
        if failing == name:
            raise error
    class Env:
        def __init__(self, **kwargs):
            checkpoint("environment")
            self.browser_launcher = kwargs["browser_launcher"]
        def close(self):
            checkpoint("env_close")
    class Agent:
        def __init__(self, **kwargs):
            checkpoint("agent")
            self.env = kwargs["env"]
        def run(self, _task):
            checkpoint("run")
            if browser_requested:
                self.env.browser_launcher()
                checkpoint("after_navigation")
            return result
    def model(_config):
        checkpoint("model")
        return object()
    def grounding(**_kwargs):
        checkpoint("grounding")
        return object()
    monkeypatch.setattr(sys, "argv", ["run_windows_local.py", "--task", "test task",
                                     "--result-dir", str(Path(profile).parent / "task-results")])
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(size=lambda: (1280, 720)))
    launch = Mock(return_value=(started, profile if started else None))
    monkeypatch.setattr(runner, "ensure_chrome", launch)
    cleanup = Mock(side_effect=lambda *_args: events.append("chrome_close"))
    monkeypatch.setattr(runner, "close_test_chrome", cleanup)
    monkeypatch.setattr(runner, "ModelConfig", lambda **_kwargs: object())
    monkeypatch.setattr(runner, "ChatModel", model)
    monkeypatch.setattr(runner, "LoggingChatModel", lambda model: model)
    monkeypatch.setattr(runner, "UIVenus2Grounding", grounding)
    monkeypatch.setattr(runner, "LoggingGrounding", lambda model: model)
    monkeypatch.setattr(runner, "WindowsEnvironment", Env)
    monkeypatch.setattr(runner, "Agent", Agent)
    monkeypatch.setattr(runner, "reset_collected", lambda: checkpoint("reset"))
    monkeypatch.setattr(runner, "log_status", lambda _status: checkpoint("log"))
    return events, launch, cleanup, error


@pytest.mark.parametrize("failing", ["model", "grounding", "environment", "agent", "reset",
                                     "log", "run", "env_close"])
def test_main_does_not_start_or_close_browser_on_desktop_task_errors(monkeypatch, profile_root,
                                                                  failing):
    profile = _profile(profile_root)
    events, launch, cleanup, error = _stub_main(monkeypatch, profile, failing=failing)

    with pytest.raises(RuntimeError) as caught:
        runner.main()
    assert caught.value is error
    launch.assert_not_called()
    cleanup.assert_not_called()
    if failing not in {"model", "grounding", "environment"}:
        assert "env_close" in events


@pytest.mark.parametrize("started", [True, False])
@pytest.mark.parametrize("failing", [None, "after_navigation", "env_close"])
def test_main_keeps_on_demand_browser_on_success_or_exception(monkeypatch, profile_root,
                                                            started, failing):
    _events, launch, cleanup, error = _stub_main(monkeypatch, _profile(profile_root), started=started,
                                               failing=failing, browser_requested=True)
    if failing:
        with pytest.raises(RuntimeError) as caught:
            runner.main()
        assert caught.value is error
    else:
        assert runner.main() == 0
    launch.assert_called_once_with(9222, runner.CHROME_DEFAULT)
    cleanup.assert_not_called()


def test_main_does_not_start_or_close_browser_for_desktop_task(monkeypatch, profile_root):
    profile = _profile(profile_root)
    events, launch, cleanup, _error = _stub_main(monkeypatch, profile)

    assert runner.main() == 0
    launch.assert_not_called()
    cleanup.assert_not_called()
    assert "env_close" in events
