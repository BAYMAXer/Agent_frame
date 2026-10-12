"""Stopping a local task keeps its browser subprocess alive; no processes are started."""
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from osworld_agent.viz import server
from osworld_agent.run_recording import LocalRunRecorder


@pytest.fixture
def runner_state(monkeypatch):
    assert Path(server.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]
    proc = Mock(pid=1010)
    proc.poll.return_value = None
    state = {"proc": proc, "status": "running", "task_name": "test", "config": None}
    monkeypatch.setattr(server, "_agent_state", state)
    return proc, state


@pytest.mark.parametrize("config, stop_tree", [
    ({"mode": "local"}, False),
    ({"mode": "vm"}, True),
    ({}, True),
    (None, True),
])
def test_stop_preserves_local_browser_children(monkeypatch, runner_state, config, stop_tree):
    proc, state = runner_state
    state["config"] = config
    stop = Mock()
    monkeypatch.setattr(server.subprocess, "run", stop)

    assert asyncio.run(server.stop_run()) == {"ok": True}

    command = ["taskkill"] + (["/T"] if stop_tree else []) + ["/F", "/PID", str(proc.pid)]
    stop.assert_called_once_with(command, capture_output=True, timeout=15, check=True)
    proc.terminate.assert_not_called()
    assert state["status"] == "idle"
    assert state["proc"] is None
    assert state["task_name"] is None


def test_stop_fallback_terminates_only_runner(monkeypatch, runner_state):
    proc, state = runner_state
    state["config"] = {"mode": "local"}
    monkeypatch.setattr(server.subprocess, "run", Mock(side_effect=OSError("taskkill unavailable")))

    assert asyncio.run(server.stop_run()) == {"ok": True}

    proc.terminate.assert_called_once_with()


def test_failed_stop_keeps_process_handle_and_running_status(monkeypatch, runner_state):
    proc, state = runner_state
    state["config"] = {"mode": "local"}
    monkeypatch.setattr(server.subprocess, "run", Mock(side_effect=OSError("taskkill failed")))
    proc.wait.side_effect = TimeoutError("runner still active")
    result = asyncio.run(server.stop_run())
    assert result["ok"] is False
    assert state["proc"] is proc
    assert state["status"] == "running"


@pytest.mark.parametrize("finished", [True, False])
def test_stop_does_not_kill_an_absent_or_finished_runner(monkeypatch, runner_state, finished):
    proc, state = runner_state
    if finished:
        proc.poll.return_value = 0
    else:
        state["proc"] = None
    stop = Mock()
    monkeypatch.setattr(server.subprocess, "run", stop)

    assert asyncio.run(server.stop_run()) == {"ok": True}

    stop.assert_not_called()
    proc.terminate.assert_not_called()


def test_stop_keeps_pending_action_and_lists_partial_trajectory(monkeypatch, runner_state, tmp_path):
    _proc, state = runner_state
    run_dir = tmp_path / "interrupted-task"
    recorder = LocalRunRecorder(run_dir, "Edit a local document")
    recorder.on_progress({"phase": "executing", "step": 1,
                          "action": {"action": "click", "args": {"x": 50, "y": 60}}}, None, None)
    state["config"] = {"mode": "local", "result_dir": str(run_dir)}
    monkeypatch.setattr(server.subprocess, "run", Mock())
    monkeypatch.setattr(server, "LOCAL_RUNS_DIR", str(tmp_path))
    asyncio.run(server.stop_run())
    manifest = json.loads((run_dir / "trajectory.json").read_text(encoding="utf-8"))
    assert manifest["outcome"]["status"] == "interrupted"
    assert manifest["pending_step"]["action"]["action"] == "click"
    listing = asyncio.run(server.local_runs())["runs"]
    assert listing[0]["status"] == "interrupted"
    assert listing[0]["phase"] == "executing"
    detail = asyncio.run(server.local_traj("interrupted-task"))
    assert detail["traj"][0]["pending"] is True
    assert json.loads(detail["traj"][0]["action"])["action"] == "click"
    assert "执行" in manifest["outcome"]["error"] or "终止" in manifest["outcome"]["error"]
