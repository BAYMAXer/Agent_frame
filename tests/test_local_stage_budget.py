"""Stage timeouts stop the task and leave an inspectable partial run."""
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from osworld_agent.actions import Action
from osworld_agent.adapters.windows_env import WindowsEnvironment
from osworld_agent.model.request_deadline import ModelRequestTimeout
from osworld_agent.pipeline import Pipeline, StepState
from osworld_agent.run_recording import LocalRunRecorder
from osworld_agent.viz import server
from osworld_agent.viz.logging import LoggingChatModel, LoggingGrounding


@pytest.mark.parametrize("stage", ["deciding", "grounding"])
def test_stage_timeout_aborts_without_executing_gui(monkeypatch, tmp_path, stage, capsys):
    from test_desktop_pipeline import DecisionModel
    env = WindowsEnvironment(action_settle_seconds=0)
    monkeypatch.setattr(env, "_focus_is_browser", lambda: False)
    monkeypatch.setattr(env, "_take_desktop_screenshot", lambda: Image.new("RGB", (800, 600)))
    monkeypatch.setattr("osworld_agent.pipeline._viz_log_status", None)
    gui = SimpleNamespace(click=Mock(), write=Mock())
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    model = DecisionModel([{"type": "click", "target": "Notepad editor"}])
    if stage == "deciding":
        model.chat = Mock(side_effect=["{}", ModelRequestTimeout("decision wall-clock deadline")])
    grounding = Mock()
    grounding.locate.side_effect = ModelRequestTimeout("grounding wall-clock deadline")
    recorder = LocalRunRecorder(tmp_path, "Edit Notepad")
    pipeline = Pipeline(model=model, grounding=grounding, env=env, system_prompt="",
                        step_timeout=120, progress_callback=recorder.on_progress)
    with pytest.raises(ModelRequestTimeout) as caught:
        pipeline.run("Edit Notepad")
    recorder.finish(status="failed", error=str(caught.value))
    manifest = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert manifest["outcome"]["status"] == "failed"
    assert manifest["outcome"]["phase"] == stage
    assert manifest["pending_step"]["phase"] == stage
    gui.click.assert_not_called()
    assert f"[phase] {stage}" in capsys.readouterr().out


def test_wrapper_forwards_remaining_step_budget():
    deadline = time.monotonic() + 10
    model_inner = SimpleNamespace(vision=True)
    ground_inner = SimpleNamespace()
    model, grounding = LoggingChatModel(model_inner), LoggingGrounding(ground_inner)
    model.request_deadline = grounding.request_deadline = deadline
    assert model_inner.request_deadline == deadline
    assert ground_inner.request_deadline == deadline


@pytest.mark.parametrize("method", ["run_command", "run_python"])
def test_native_command_timeout_uses_remaining_step_budget(monkeypatch, method):
    env = WindowsEnvironment()
    env.request_deadline = time.monotonic() + 1
    run = Mock(side_effect=subprocess.TimeoutExpired("command", 1))
    monkeypatch.setattr("osworld_agent.adapters.windows_env.subprocess.run", run)
    with pytest.raises(ModelRequestTimeout):
        getattr(env, method)("test command")
    assert 0 < run.call_args.kwargs["timeout"] <= 1


def test_expired_budget_does_not_start_commands(monkeypatch):
    env = WindowsEnvironment()
    env.request_deadline = time.monotonic() - 1
    run = Mock()
    monkeypatch.setattr("osworld_agent.adapters.windows_env.subprocess.run", run)
    with pytest.raises(ModelRequestTimeout):
        env.run_command("must not execute")
    run.assert_not_called()


@pytest.mark.parametrize("entry", ["route", "browser_execute", "desktop_execute"])
def test_timeout_is_not_converted_to_an_ordinary_action_failure(entry):
    env = Mock()
    timeout = ModelRequestTimeout("request budget exhausted")
    pipeline = Pipeline(model=Mock(), grounding=Mock(), env=env, system_prompt="")
    state = StepState()
    if entry == "route":
        env.route_action.side_effect = timeout
        call = lambda: pipeline.ground(Action("click", {"target": "editor"}), state)
    else:
        if entry == "browser_execute":
            state.route = {"channel": "browser_dom"}
            env.execute_routed.side_effect = timeout
        else:
            env.step.side_effect = timeout
        call = lambda: pipeline.execute(Action("click", {"x": 10, "y": 20}), state)
    with pytest.raises(ModelRequestTimeout) as caught:
        call()
    assert caught.value is timeout


def test_model_selection_and_timeouts_are_passed_to_local_runner(monkeypatch, tmp_path):
    state = {"proc": None, "status": "idle"}
    monkeypatch.setattr(server, "_agent_state", state)
    monkeypatch.setattr(server, "LOCAL_RUNS_DIR", str(tmp_path))
    process = Mock()
    launch = Mock(return_value=process)
    monkeypatch.setattr(server.subprocess, "Popen", launch)
    monkeypatch.setattr(server.threading, "Thread", Mock())
    cfg = {"mode": "local", "task": "Edit Notepad", "model": "selected-decision",
           "model_url": "http://gateway.test/v1", "ground_model": "selected-grounding",
           "ground_url": "http://ground.test/v1", "ground_type": "ui-tars",
           "model_api_key": "test-decision-key", "ground_api_key": "test-ground-key",
           "model_timeout": 30, "ground_timeout": 20, "step_timeout": 80}
    assert server._start_local(cfg)["ok"] is True
    command = launch.call_args.args[0]
    assert command[command.index("--model") + 1] == "selected-decision"
    assert command[command.index("--ground-type") + 1] == "ui-tars"
    assert command[command.index("--model-timeout") + 1] == "30"
    assert command[command.index("--ground-timeout") + 1] == "20"
    assert command[command.index("--step-timeout") + 1] == "80"
    child_env = launch.call_args.kwargs["env"]
    assert child_env["OSWORLD_LOCAL_MODEL_API_KEY"] == "test-decision-key"
    assert child_env["OSWORLD_LOCAL_GROUND_API_KEY"] == "test-ground-key"
    assert "test-decision-key" not in command
    assert state["config"]["result_dir"]
