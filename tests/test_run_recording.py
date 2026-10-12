"""Partial local runs remain inspectable even if execution never returns."""
import base64
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from osworld_agent import run_recording
from osworld_agent.env import Observation
from osworld_agent.run_recording import LocalRunRecorder


def _result(**kwargs):
    values = dict(trajectory=[], screenshots={}, steps=0, success=False,
                  score=0.0, final_answer="")
    values.update(kwargs)
    return SimpleNamespace(**values)


def _manifest(path):
    return json.loads((path / "trajectory.json").read_text(encoding="utf-8"))


def _events(path):
    return [json.loads(line) for line in
            (path / "run_events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_constructor_publishes_running_empty_trajectory(tmp_path):
    path = tmp_path / "run"
    LocalRunRecorder(path, "Edit a local document")
    trajectory = _manifest(path)
    assert trajectory["version"] == "2.0"
    assert trajectory["task"]["instruction"] == "Edit a local document"
    assert trajectory["outcome"]["status"] == "running"
    assert trajectory["outcome"]["phase"] == "starting"
    assert trajectory["steps"] == []


def test_before_execution_journal_and_pending_action_survive_abandonment(tmp_path):
    recorder = LocalRunRecorder(tmp_path, "Write text")
    state = SimpleNamespace(step_id=1, obs=Observation(
        screenshot=Image.new("RGB", (4, 3), "red")))
    event = {"event": "executing", "phase": "executing", "step": 1,
             "action": {"action": "type", "args": {"text": "你好"}},
             "raw": "model output", "route": {"channel": "gui"}}
    recorder.on_progress(event, state, _result())
    # No finish callback: the runner may have been killed during the action.
    trajectory = _manifest(tmp_path)
    assert trajectory["outcome"]["status"] == "running"
    assert trajectory["pending_step"]["action"] == event["action"]
    assert trajectory["pending_step"]["raw"] == "model output"
    assert trajectory["pending_step"]["route"] == {"channel": "gui"}
    assert _events(tmp_path)[0]["action"] == event["action"]
    assert (tmp_path / "step_1.png").is_file()


def test_completed_steps_and_images_are_saved_immediately(tmp_path):
    image = Image.new("RGB", (4, 3), "red")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    result = _result(steps=1, screenshots={1: image}, trajectory=[{
        "step": 1, "action": {"action": "click", "args": {"x": 1, "y": 2}},
        "observation": {"id": "o_1", "context": [], "screenshot": data_url},
        "policy_input": [{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": data_url}}]}],
        "policy_output": "click", "valid_action": True,
        "outcome": "Clicked", "execution_info": {"status": "executed"},
    }])
    recorder = LocalRunRecorder(tmp_path, "Click a button")
    recorder.on_progress({"phase": "step_complete", "step": 1}, None, result)
    trajectory = _manifest(tmp_path)
    assert len(trajectory["steps"]) == 1
    assert trajectory["steps"][0]["execution_info"]["status"] == "executed"
    assert trajectory["pending_step"] is None
    assert (tmp_path / "step_1.png").is_file()
    assert trajectory["steps"][0]["screenshot"].startswith("assets/")
    assert len(list((tmp_path / "assets").iterdir())) == 1
    assert "data:image/" not in (tmp_path / "trajectory.json").read_text(encoding="utf-8")


def test_parse_failure_still_persists_step_and_screenshot(tmp_path):
    recorder = LocalRunRecorder(tmp_path, "Click")
    result = _result(steps=1, screenshots={1: Image.new("RGB", (4, 3), "red")},
                     trajectory=[{
                         "step": 1, "error": "动作解析失败", "raw": "invalid output",
                         "policy_input": [{"role": "user", "content": "Click"}],
                         "policy_output": "invalid output", "valid_action": False,
                     }])
    recorder.on_progress({"phase": "step_complete", "step": 1,
                          "status": "rejected"}, None, result)
    trajectory = _manifest(tmp_path)
    assert len(trajectory["steps"]) == 1
    assert trajectory["steps"][0]["valid_action"] is False
    assert trajectory["steps"][0]["policy_output"] == "invalid output"
    assert trajectory["steps"][0]["trainable"] is False
    assert (tmp_path / "step_1.png").is_file()
    assert trajectory["pending_step"] is None


def test_progress_keeps_pending_action_until_completed_step(tmp_path):
    recorder = LocalRunRecorder(tmp_path, "Click")
    recorder.on_progress({"phase": "executing", "step": 1,
                          "action": {"action": "click", "args": {}}}, None, _result())
    recorder.on_progress({"phase": "executed", "step": 1}, None, None)
    assert _manifest(tmp_path)["pending_step"]["action"]["action"] == "click"
    recorder.on_progress({"phase": "step_complete", "step": 1}, None, None)
    assert _manifest(tmp_path)["pending_step"] is None


def test_before_and_after_images_do_not_overwrite_decision_screenshot(tmp_path):
    recorder = LocalRunRecorder(tmp_path, "Click")
    state = SimpleNamespace(step_id=1, obs=Observation(
        screenshot=Image.new("RGB", (4, 3), "red")))
    recorder.on_progress({"phase": "executing", "step": 1}, state, _result())
    state.obs.screenshot = Image.new("RGB", (4, 3), "blue")
    recorder.on_progress({"phase": "step_complete", "step": 1}, state, _result())
    with Image.open(tmp_path / "step_1.png") as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_failed_or_interrupted_run_retains_last_pending_action(tmp_path, status):
    recorder = LocalRunRecorder(tmp_path, "Click")
    recorder.on_progress({"phase": "executing", "step": 2,
                          "action": {"action": "click", "args": {}}}, None, _result())
    recorder.finish(status=status, error="execution stopped")
    trajectory = _manifest(tmp_path)
    assert trajectory["outcome"]["status"] == status
    assert trajectory["outcome"]["error"] == "execution stopped"
    assert trajectory["outcome"]["phase"] == "executing"
    assert trajectory["pending_step"]["step"] == 2
    assert _events(tmp_path)[-1]["status"] == status


def test_successful_finish_clears_pending_and_uses_latest_result(tmp_path):
    recorder = LocalRunRecorder(tmp_path, "Click")
    recorder.on_progress({"phase": "executing", "step": 1}, None, _result())
    recorder.finish(_result(success=True, steps=1, final_answer="Done"))
    trajectory = _manifest(tmp_path)
    assert trajectory["outcome"]["status"] == "completed"
    assert trajectory["outcome"]["success"] is True
    assert trajectory["outcome"]["final_answer"] == "Done"
    assert trajectory["pending_step"] is None


def test_atomic_replacement_failure_preserves_previous_manifest(tmp_path, monkeypatch):
    recorder = LocalRunRecorder(tmp_path, "Click")
    before = (tmp_path / "trajectory.json").read_bytes()
    replacements = []

    def fail_replace(source, target):
        replacements.append((source, target))
        assert source.parent == target.parent == tmp_path
        assert source != target
        json.loads(source.read_text(encoding="utf-8"))
        raise OSError("replace failed")

    monkeypatch.setattr(run_recording.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        recorder.on_progress({"phase": "executing", "step": 1}, None, _result())
    assert replacements
    assert (tmp_path / "trajectory.json").read_bytes() == before
    assert _events(tmp_path)[-1]["phase"] == "executing"
    assert not list(tmp_path.glob(".recording_*"))


def test_invalid_event_data_is_not_silently_discarded(tmp_path):
    recorder = LocalRunRecorder(tmp_path, "Click")
    with pytest.raises(TypeError):
        recorder.on_progress({"phase": "executing", "action": object()}, None, _result())
    assert _manifest(tmp_path)["outcome"]["phase"] == "starting"
