"""Exercise the complete desktop loop and preserve evidence when execution is interrupted."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from osworld_agent.actions import Action
from osworld_agent.agent import Agent
from osworld_agent.adapters.windows_env import WindowsEnvironment
from osworld_agent.env import Observation, StepResult
from osworld_agent.model.grounding import GroundingResult
from osworld_agent.pipeline import Execution, Pipeline, StepState
from osworld_agent.run_recording import LocalRunRecorder
from osworld_agent.viz.logging import LoggingGrounding


class DecisionModel:
    vision = True

    def __init__(self, actions):
        self.responses = iter(["{}", *(json.dumps({"next_action": action,
                               "action_status": "success"}, ensure_ascii=False)
                                     for action in actions), "CONFIRM"])
        self.inputs = []

    def chat(self, messages, **kwargs):
        self.inputs.append(messages)
        return next(self.responses)

    def build_vision_message(self, text, images):
        return {"role": "user", "content": text}


@pytest.fixture
def desktop(monkeypatch):
    gui = SimpleNamespace(click=Mock(), write=Mock(), hotkey=Mock(), press=Mock())
    monkeypatch.setitem(sys.modules, "pyautogui", gui)
    launch = Mock(side_effect=AssertionError("desktop tasks must never launch browsers"))
    env = WindowsEnvironment(browser_launcher=launch, action_settle_seconds=0)
    monkeypatch.setattr(env, "_focus_is_browser", lambda: False)
    frames = iter(Image.new("RGB", (800, 600), color) for color in ("black", "white", "gray"))
    monkeypatch.setattr(env, "_take_desktop_screenshot", lambda: next(frames))
    monkeypatch.setattr("osworld_agent.pipeline._viz_log_status", None)
    monkeypatch.setattr("osworld_agent.viz.logging._post", lambda _data: None)
    return env, gui, launch


def test_complete_computer_use_loop_has_no_cdp_or_browser_start(desktop, tmp_path):
    env, gui, launch = desktop
    model = DecisionModel([{"type": "click", "target": "the editor area in Notepad"},
                           {"type": "type", "target": "the text area", "text": "hello"},
                           {"type": "request_finish", "answer": "Typed hello"}])
    inner = SimpleNamespace(width=1920, height=1080)
    inner.locate = Mock(side_effect=lambda shot, target:
                        GroundingResult(inner.width // 4, inner.height // 2))
    recorder = LocalRunRecorder(tmp_path, "Type hello in Notepad")
    agent = Agent(model, LoggingGrounding(inner), env, max_steps=3,
                  progress_callback=recorder.on_progress)
    result = agent.run("Type hello in Notepad")
    recorder.finish(result)
    assert result.success is True
    assert result.steps == 3
    assert inner.width == 800 and inner.height == 600
    assert [call.args for call in gui.click.call_args_list] == [(200, 300), (200, 300)]
    gui.write.assert_called_once_with("hello")
    launch.assert_not_called()
    assert env._browser_session is None
    manifest = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert manifest["outcome"]["status"] == "completed"
    assert len(manifest["steps"]) == 3
    assert manifest["pending_step"] is None
    for step in manifest["steps"]:
        assert step["observation"]["info"]["scene"]["browser_use"] == 0
        assert step["observation"]["info"]["screenshot_coordinate_space"] == "screen"


def test_interruption_inside_gui_leaves_action_and_screenshot(desktop, tmp_path):
    env, gui, launch = desktop
    gui.click.side_effect = KeyboardInterrupt("user stopped task")
    model = DecisionModel([{"type": "click", "target": "the editor area in Notepad"}])
    grounding = Mock()
    grounding.locate.return_value = GroundingResult(200, 300)
    recorder = LocalRunRecorder(tmp_path, "Click the Notepad editor")
    agent = Agent(model, grounding, env, max_steps=1, progress_callback=recorder.on_progress)
    with pytest.raises(KeyboardInterrupt):
        agent.run("Click the Notepad editor")
    manifest = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert manifest["outcome"]["phase"] == "executing"
    assert manifest["pending_step"]["action"]["action"] == "click"
    assert manifest["pending_step"]["action"]["args"]["x"] == 200
    assert (tmp_path / "step_1.png").is_file()
    journal = [json.loads(line) for line in
               (tmp_path / "run_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[-1]["phase"] == "executing"
    launch.assert_not_called()


@pytest.mark.parametrize("info, expected", [
    ({"status": "rejected", "dispatched": False, "error": "visual target missing"}, "未执行"),
    ({"status": "uncertain", "error": "mouse failed"}, "不确定"),
    ({"error": "legacy executor failed"}, "执行失败"),
])
def test_execution_errors_are_visible_to_the_next_decision(info, expected):
    step_result = StepResult(observation=Observation(), info=info)
    assert expected in Pipeline._describe_outcome(step_result)
    assert info["error"] in Pipeline._describe_outcome(step_result)
    pipeline = Pipeline(model=Mock(), grounding=Mock(), env=Mock(), system_prompt="")
    state = StepState()
    pipeline._update_program_info(Action("click"), Execution(kind="env", info=info), state)
    assert state.execution_status != "executed"


def test_unavailable_uno_action_is_rejected_before_execution(desktop):
    env, _gui, launch = desktop
    pipeline = Pipeline(model=Mock(), grounding=Mock(), env=env, system_prompt="")
    assert "不支持" in pipeline._validate_action(Action("save"))
    assert "不支持" in pipeline._validate_action(Action("set_cell_values", {
        "new_cell_values": {"A1": 1}, "app_name": "test.xlsx", "sheet_name": "Sheet1"}))
    launch.assert_not_called()
