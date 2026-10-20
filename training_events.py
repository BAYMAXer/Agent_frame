"""Durable training events; visualization logs are a separate best-effort stream.

Images are written before their referencing event. Every complete JSONL line is
flushed (and fsynced by default), so an interrupted episode remains inspectable.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid


def externalize_images(value, root):
    """Recursively store data-URL images in a content-addressed assets directory."""
    root = Path(root)
    if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
        header, encoded = value.split(",", 1)
        data = base64.b64decode(encoded, validate=True)
        digest = hashlib.sha256(data).hexdigest()
        suffix = ".jpg" if "jpeg" in header else ".png"
        folder = root / "assets"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (digest + suffix)
        if not path.exists():
            # Unique staging paths permit independent writers to share assets.
            temporary = folder / (digest + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return "assets/" + path.name
    if isinstance(value, dict):
        return {key: externalize_images(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [externalize_images(item, root) for item in value]
    return value


class TrainingEventWriter:
    """One writer per episode. Existing logs are never overwritten or mixed."""

    def __init__(self, path, run_id=None, manifest=None, *, fsync=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex
        self.manifest = copy.deepcopy(manifest or {})
        self.events = []
        self._lock = threading.Lock()
        self._fsync = fsync
        # Exclusive creation avoids accidentally appending a new run to old data.
        self._handle = self.path.open("x", encoding="utf-8")
        try:
            self.emit("episode_start", manifest=self.manifest)
        except BaseException:
            self._handle.close()
            raise

    def emit(self, event_type, *, actor_role="framework", source="framework",
             parent_decision_id=None, observation_id=None, **payload):
        with self._lock:
            if self._handle.closed:
                raise ValueError("Training event writer is closed")
            reserved = {"run_id", "event_id", "event_type", "timestamp", "actor_role",
                        "source", "parent_decision_id", "observation_id"}
            if reserved.intersection(payload):
                raise ValueError("Event identity fields cannot be overwritten")
            event = {"run_id": self.run_id, "event_id": f"{self.run_id}:e{len(self.events) + 1}",
                     "event_type": event_type, "timestamp": time.time(),
                     "actor_role": actor_role, "source": source,
                     "parent_decision_id": parent_decision_id, "observation_id": observation_id,
                     **copy.deepcopy(payload)}
            packed = externalize_images(event, self.path.parent)
            line = json.dumps(packed, ensure_ascii=False, allow_nan=False)
            self._handle.write(line + "\n")
            self._handle.flush()
            if self._fsync:
                os.fsync(self._handle.fileno())
            self.events.append(packed)
            return copy.deepcopy(packed)

    def close(self):
        with self._lock:
            if not self._handle.closed:
                self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_events(path):
    """Read complete events; tolerate only a final torn write after interruption."""
    lines = Path(path).read_bytes().splitlines(keepends=True)
    events = []
    for index, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                break
            raise
    return events
