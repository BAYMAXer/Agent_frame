"""Persist local task progress before an action can block or be interrupted."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from .trajectory import build_trajectory, dump_trajectory


class LocalRunRecorder:
    """Keep a readable partial trajectory and an append-only progress journal."""

    _STEP_COMPLETE_PHASES = {"step_complete", "step_completed", "completed_step"}

    def __init__(self, result_dir, task: str):
        self.result_dir = Path(result_dir)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.status = "running"
        self.phase = "starting"
        self.error = ""
        self.pending_step = None
        self._saved_screenshots = set()
        self._result = SimpleNamespace(trajectory=[], screenshots={}, steps=0,
                                       success=False, score=0.0, final_answer="")
        self._write_trajectory()

    def on_progress(self, event: dict, state, result) -> None:
        """Synchronously journal progress and publish all completed steps."""
        # Serialize before mutating state; malformed log data must be visible to
        # the caller instead of silently losing the only evidence of a hang.
        payload = json.loads(json.dumps({"ts": time.time(), **event}, ensure_ascii=False))
        self._append_event(payload)
        if result is not None:
            self._result = result
        self.phase = str(payload.get("phase") or payload.get("event") or "running")
        step = payload.get("step")
        if step is None and state is not None:
            step = getattr(state, "step_id", None)
        if self.phase in self._STEP_COMPLETE_PHASES:
            self.pending_step = None
        else:
            previous = self.pending_step or {}
            if previous.get("step") != step:
                previous = {}
            self.pending_step = {
                **previous, **payload, "phase": self.phase,
                "event": payload.get("event") or self.phase,
                "step": step,
            }
        self._save_screenshots(state, step)
        self._write_trajectory()

    def finish(self, result=None, status: str = "completed", error: str = "") -> None:
        """Finalize a run, retaining an unfinished action on failure or stop."""
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"Unknown local run status: {status}")
        if result is not None:
            self._result = result
        self.status = status
        self.error = error
        if status == "completed":
            self.pending_step = None
            self.phase = "completed"
        self._append_event({"ts": time.time(), "event": "task_end",
                            "phase": self.phase, "status": status, "error": error})
        self._save_screenshots(None, None)
        self._write_trajectory()

    def _append_event(self, event) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with (self.result_dir / "run_events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()

    def _save_screenshots(self, state, current_step) -> None:
        screenshots = getattr(self._result, "screenshots", {}) or {}
        for step, shot in screenshots.items():
            self._save_screenshot(step, shot)
        if state is not None and current_step is not None:
            obs = getattr(state, "obs", None)
            if obs is not None:
                self._save_screenshot(current_step, getattr(obs, "screenshot", None))

    def _save_screenshot(self, step, shot) -> None:
        if shot is None or step in self._saved_screenshots:
            return
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise ValueError(f"Invalid screenshot step: {step!r}")
        if isinstance(shot, (bytes, bytearray)):
            from PIL import Image
            shot = Image.open(io.BytesIO(shot))
        if not callable(getattr(shot, "save", None)):
            raise TypeError("Local screenshots must be images or encoded image bytes")
        target = self.result_dir / f"step_{step}.png"
        temp = self._temporary_path(".png")
        try:
            shot.save(temp, format="PNG")
            os.replace(temp, target)
            self._saved_screenshots.add(step)
        finally:
            temp.unlink(missing_ok=True)

    def _temporary_path(self, suffix):
        fd, name = tempfile.mkstemp(prefix=".recording_", suffix=suffix, dir=self.result_dir)
        os.close(fd)
        return Path(name)

    def _write_trajectory(self) -> None:
        trajectory = build_trajectory("local", "windows", self.task, self._result)
        trajectory["outcome"].update(status=self.status, phase=self.phase, error=self.error)
        trajectory["pending_step"] = self.pending_step
        target = self.result_dir / "trajectory.json"
        temp = self._temporary_path(".json")
        try:
            # Use the existing exporter so partial runs retain deduplicated,
            # external image assets and the same schema as completed runs.
            dump_trajectory(str(temp), trajectory)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
