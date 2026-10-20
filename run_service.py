"""Durable, single-desktop task execution shared by Studio and MCP.

Only the standard library is imported here. A small detached supervisor owns the
desktop lease and runner lifetime, so closing an MCP connection does not abandon
the timeout or accidentally free the desktop while its runner is still active.

Cancellation is cooperative with this supervisor, which terminates the runner
process tree and records ``cancelled`` even if a success trajectory was already
written. If the supervisor itself is forcibly killed, a live child keeps the
desktop blocked through the owner marker; the run becomes ``interrupted`` with
``supervisor_lost`` and is never restarted automatically. Such an orphan needs
operator inspection/termination before another desktop task can run.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out", "interrupted"})


class RunServiceError(RuntimeError):
    code = "run_service_error"


class DesktopBusyError(RunServiceError):
    code = "desktop_busy"


class RequestConflictError(RunServiceError):
    code = "request_conflict"


class RunNotFoundError(RunServiceError):
    code = "run_not_found"


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        # On Windows a concurrent reader or scanner can briefly hold a handle
        # that disallows replacement. Keep the old complete snapshot visible
        # and retry instead of converting successful work into worker_error.
        deadline = time.monotonic() + 2
        delay = 0.01
        while True:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
    finally:
        tmp.unlink(missing_ok=True)


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: conservatively alive.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self, timeout=0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise DesktopBusyError("The desktop or task registry is busy.")
                time.sleep(0.04)

    def close(self):
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _desktop_paths(desktop_id=None):
    # Independent of checkout and runs_root: Studio and MCP share this lease.
    root = Path(os.getenv("YAERO_LOCK_DIR", str(Path.home() / ".yaero" / "locks"))).resolve()
    key = hashlib.sha256((desktop_id or os.getenv("YAERO_DESKTOP_ID", "local")).encode()).hexdigest()[:24]
    return root / (key + ".lock"), root / (key + ".owner.json"), root / (key + ".launch.lock")


class DesktopLease:
    """A desktop lease for existing workflows that do not use RunService.start."""

    def __init__(self, owner, desktop_id=None):
        self.lock_path, self.owner_path, _ = _desktop_paths(desktop_id)
        self.lock = _FileLock(self.lock_path).acquire()
        previous = _read_json(self.owner_path, {}) or {}
        # If a supervisor was killed abruptly, never overlap its surviving child.
        if _pid_alive(previous.get("child_pid")):
            self.lock.close()
            raise DesktopBusyError("A previous desktop runner is still alive.")
        self.owner = {"owner": str(owner), "pid": os.getpid(), "started_at": time.time()}
        _write_json(self.owner_path, self.owner)

    def set_child(self, pid):
        self.owner["child_pid"] = pid
        _write_json(self.owner_path, self.owner)

    def close(self):
        if self.lock.handle is not None:
            self.owner_path.unlink(missing_ok=True)
            self.lock.close()

    release = close

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _spawn_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if proc.poll() is None:
            proc.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


def _outcome(meta):
    value = _read_json(Path(meta["result_dir"]) / "trajectory.json", {})
    result = value.get("outcome") if isinstance(value, dict) else None
    return result if isinstance(result, dict) else {}


def _download_evidence(verification):
    """Only follow documented verifier fields, never search arbitrary paths."""
    report = verification
    for _ in range(4):
        if not isinstance(report, dict):
            return
        checks = report.get("checks", [])
        if isinstance(checks, list):
            for check in checks[:200]:
                if (not isinstance(check, dict) or check.get("status") != "passed"
                        or check.get("kind") not in {"download", "paper_download"}
                        or check.get("source") not in {"artifact", "artifact_transport"}):
                    continue
                if isinstance(check.get("path"), str):
                    yield check, check["source"]
                children = check.get("artifacts", [])
                if isinstance(children, list):
                    for child in children[:200]:
                        if (isinstance(child, dict) and isinstance(child.get("path"), str)
                                and child.get("status", "passed") == "passed"):
                            yield child, check["source"]
        facts = report.get("download_facts", [])
        if isinstance(facts, list):
            for fact in facts[:200]:
                if (isinstance(fact, dict) and fact.get("status") == "passed"
                        and fact.get("transport") == "browser" and fact.get("completed") is True
                        and isinstance(fact.get("path"), str)):
                    yield fact, "browser_receipt"
        report = report.get("completion")


def _artifact_inventory(root, verification, limit=200):
    """Bound the response and filesystem walk, prioritizing useful evidence."""
    root = Path(root).resolve()
    artifacts, seen = [], set()
    truncated = False

    def add(path, source="run_directory", evidence=None, evidence_source=None):
        nonlocal truncated
        try:
            path = Path(path)
            if not path.is_absolute() or path.is_symlink():
                return
            actual = path.resolve()
            if not actual.is_file():
                return
            size = actual.stat().st_size
            identity = os.path.normcase(str(actual))
            if identity in seen:
                return
            if source == "run_directory":
                relative = actual.relative_to(root)
                name = relative.as_posix()
            else:
                name = actual.name
        except (ValueError, OSError):
            return
        if len(artifacts) >= limit:
            truncated = True
            return
        seen.add(identity)
        item = {
            "name": name, "path": str(actual), "uri": actual.as_uri(), "size_bytes": size,
            "kind": "download" if evidence is not None else
                    "trajectory" if actual.name == "trajectory.json" else
                    "events" if actual.name == "events.jsonl" else
                    "image" if actual.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else "file",
            "source": source,
        }
        if evidence is not None:
            # A read of the MCP result inventories existing paths, not file
            # contents. Keep the run-end evidence distinct from a fresh hash.
            item["evidence"] = {
                "status_at_run_end": "passed", "source": evidence_source,
                "artifact_id": evidence.get("artifact_id"),
                "sha256_at_run_end": evidence.get("sha256"),
            }
        artifacts.append(item)

    add(root / "trajectory.json")
    add(root / "events.jsonl")
    for index, (evidence, source) in enumerate(_download_evidence(verification)):
        if index >= 1000:
            truncated = True
            break
        add(evidence["path"], source="verification", evidence=evidence, evidence_source=source)
    visited = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not (Path(folder) / name).is_symlink())
        visited += len(dirs) + len(files)
        if visited > 4096:
            truncated = True
            break
        for name in sorted(files):
            add(Path(folder) / name)
            if truncated and len(artifacts) >= limit:
                return artifacts, truncated
    return artifacts, truncated


def _worker(run_dir):
    run_dir = Path(run_dir)
    meta_path = run_dir / "run.json"
    meta = _read_json(meta_path)
    if not isinstance(meta, dict) or meta.get("status") != "starting":
        return 1  # Never rerun a recorded task.
    proc = None
    lease = None
    try:
        lease = DesktopLease(meta["run_id"], meta["desktop_id"])
        meta.update(worker_pid=os.getpid(), status="running", started_at=time.time())
        _write_json(meta_path, meta)
        with (run_dir / "stdout.log").open("ab") as stdout, (run_dir / "stderr.log").open("ab") as stderr:
            proc = subprocess.Popen(meta["command"], cwd=meta["cwd"], env=os.environ.copy(),
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    **_spawn_options())
            meta["pid"] = proc.pid
            lease.set_child(proc.pid)
            _write_json(meta_path, meta)
            deadline = time.monotonic() + meta["timeout_s"]
            reason = None
            while proc.poll() is None:
                if (run_dir / "cancel.json").exists():
                    reason = "cancelled"
                elif time.monotonic() >= deadline:
                    reason = "timed_out"
                if reason:
                    _stop_process(proc)
                    break
                time.sleep(0.1)
            # Cancellation that raced the last runner write still wins.
            if (run_dir / "cancel.json").exists():
                reason = "cancelled"
            result = _outcome(meta)
            status = reason or ("completed" if proc.returncode == 0 and result.get("success") is True else "failed")
            meta.update(status=status, finished_at=time.time(), exit_code=proc.returncode,
                        termination_reason=reason or result.get("termination_reason") or
                        ("missing_outcome" if not result else "runner_failed"),
                        success=status == "completed" and result.get("success") is True)
            if not result and not reason:
                meta["error"] = "Runner ended without a valid trajectory outcome."
            _write_json(meta_path, meta)
    except BaseException as exc:
        if proc is not None:
            with contextlib.suppress(Exception):
                _stop_process(proc)
        meta.update(status="failed", success=False, finished_at=time.time(),
                    termination_reason=getattr(exc, "code", "worker_error"),
                    error=str(exc), worker_pid=os.getpid())
        _write_json(meta_path, meta)
    finally:
        if lease is not None:
            # Retain the owner marker if killing the child failed.
            if proc is not None and proc.poll() is None:
                lease.lock.close()
            else:
                lease.close()
    return 0


class RunService:
    def __init__(self, runs_root=None, desktop_id=None):
        self.runs_root = Path(runs_root or os.getenv("YAERO_RUNS_ROOT") or
                              Path(__file__).resolve().parent / ".runtime" / "tasks").resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.desktop_id = desktop_id or os.getenv("YAERO_DESKTOP_ID", "local")
        self._workers = {}  # Reap supervisors started by this service instance.

    def _path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"run_[0-9a-f]{32}", run_id):
            raise RunNotFoundError("Unknown run_id.")
        path = self.runs_root / run_id
        if not (path / "run.json").is_file():
            raise RunNotFoundError("Unknown run_id.")
        return path

    def _metadata(self, run_id):
        path = self._path(run_id)
        meta = _read_json(path / "run.json")
        if not isinstance(meta, dict):
            raise RunServiceError("Task metadata is unreadable.")
        worker = self._workers.get(run_id)
        if worker is not None and worker.poll() is not None:
            self._workers.pop(run_id, None)
        if meta["status"] not in TERMINAL_STATUSES and not _pid_alive(meta.get("worker_pid")):
            # Metadata is initially saved before Popen. Give that small window
            # a grace period, then report interruption without restarting.
            if time.time() - meta["created_at"] > 10:
                meta.update(status="interrupted", success=False, finished_at=time.time(),
                            termination_reason="supervisor_lost",
                            error="Task supervisor is unavailable; this task was not restarted.")
                _write_json(path / "run.json", meta)
        return meta

    def acquire_external(self, owner):
        return DesktopLease(owner, self.desktop_id)

    def start(self, task, request_id, command=None, cwd=None, env=None, max_steps=40,
              timeout_s=600, result_dir=None, signature=None, launch_factory=None):
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string.")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
            raise ValueError("request_id must contain 1–256 characters.")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("max_steps must be a positive integer.")
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be positive and finite.")
        if command is None and launch_factory is None:
            raise ValueError("command or launch_factory is required.")
        fingerprint = hashlib.sha256(json.dumps({
            "task": task, "max_steps": max_steps, "timeout_s": timeout_s,
            "signature": signature,
            # A factory's caller supplies its semantic settings in signature.
            "command": command if launch_factory is None else None,
            "cwd": str(cwd) if cwd is not None and launch_factory is None else None,
            "env": env if launch_factory is None else None,
            "result_dir": str(result_dir) if result_dir is not None else None,
        }, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        request_key = hashlib.sha256(request_id.encode()).hexdigest()
        _, _, launch_lock = _desktop_paths(self.desktop_id)
        with _FileLock(self.runs_root / ".registry.lock").acquire(timeout=10):
            index_path = self.runs_root / "requests" / (request_key + ".json")
            old = _read_json(index_path)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise RequestConflictError("request_id was already used with different task parameters.")
                return self.get_task(old["run_id"])
            # Serialize the probe -> supervisor handoff across all runs roots.
            with _FileLock(launch_lock).acquire(timeout=10):
                with self.acquire_external("launch-probe"):
                    pass
                run_id = "run_" + uuid.uuid4().hex
                run_dir = self.runs_root / run_id
                run_dir.mkdir()
                results = Path(result_dir).resolve() if result_dir else run_dir / "results"
                results.mkdir(parents=True, exist_ok=True)
                # Reusing results could let stale trajectory.json spoof success.
                if any(results.iterdir()):
                    raise ValueError("result_dir must be empty for a new task.")
                metadata = {}
                if launch_factory is not None:
                    launch = launch_factory(run_id, str(results))
                    def field(key, default=None):
                        return launch.get(key, default) if isinstance(launch, dict) else getattr(launch, key, default)
                    command, cwd, env = field("command"), field("cwd"), field("env")
                    metadata = field("metadata", {})
                if (not isinstance(command, (list, tuple)) or not command
                        or any(not isinstance(part, str) for part in command)):
                    raise ValueError("command must be a non-empty list of strings.")
                child_env = os.environ.copy()
                if env is not None:
                    child_env.update(env)
                child_env["YAERO_RUN_ID"] = run_id
                child_env["YAERO_RESULT_DIR"] = str(results)
                meta = {
                    "run_id": run_id, "status": "starting", "task": task, "created_at": time.time(),
                    "started_at": None, "finished_at": None, "worker_pid": None, "pid": None,
                    "max_steps": max_steps, "timeout_s": timeout_s, "success": False,
                    "result_dir": str(results), "command": list(command),
                    "cwd": str(Path(cwd or os.getcwd()).resolve()), "desktop_id": self.desktop_id,
                    # Stored for local troubleshooting only, never exposed by get_task.
                    "metadata": metadata,
                }
                _write_json(run_dir / "run.json", meta)
                _write_json(index_path, {"run_id": run_id, "fingerprint": fingerprint})
                try:
                    with (run_dir / "supervisor.log").open("ab") as log:
                        worker = subprocess.Popen(
                            [sys.executable, str(Path(__file__).resolve()), "--worker", str(run_dir)],
                            cwd=str(Path(__file__).resolve().parent), env=child_env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, **_spawn_options())
                    self._workers[run_id] = worker
                except OSError as exc:
                    meta.update(status="failed", finished_at=time.time(), error=str(exc),
                                termination_reason="launch_failed")
                    _write_json(run_dir / "run.json", meta)
                    raise RunServiceError("Unable to launch task supervisor.") from exc
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    current = _read_json(run_dir / "run.json", meta)
                    if current.get("status") != "starting":
                        break
                    if worker.poll() is not None:
                        meta.update(status="failed", finished_at=time.time(),
                                    termination_reason="supervisor_start_failed",
                                    error="Task supervisor exited before starting.")
                        _write_json(run_dir / "run.json", meta)
                        break
                    time.sleep(0.03)
                return self.get_task(run_id)

    def _progress(self, meta):
        result = {"current_step": 0, "phase": meta["status"], "last_event_at": None}
        path = Path(meta["result_dir"]) / "events.jsonl"
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                offset = max(0, size - 131072)
                handle.seek(offset)
                if offset:
                    handle.readline()
                lines = handle.read().splitlines(keepends=True)
            for line in lines:
                if not line.endswith(b"\n"):
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                step = event.get("step", event.get("decision_step"))
                if isinstance(step, int):
                    result["current_step"] = max(result["current_step"], step)
                result["phase"] = event.get("event_type", result["phase"])
                result["last_event_at"] = event.get("timestamp")
        except OSError:
            pass
        return result

    def _task_view(self, meta):
        public_keys = ("run_id", "status", "task", "created_at", "started_at", "finished_at",
                       "max_steps", "timeout_s", "result_dir", "success", "exit_code",
                       "termination_reason", "error")
        result = {key: meta[key] for key in public_keys if key in meta}
        result["cancel_requested"] = (self._path(meta["run_id"]) / "cancel.json").exists()
        result["progress"] = self._progress(meta)
        result["poll_after_s"] = 2 if meta["status"] not in TERMINAL_STATUSES else None
        return result

    def get_task(self, run_id):
        return self._task_view(self._metadata(run_id))

    def get_result(self, run_id):
        meta = self._metadata(run_id)
        result = self._task_view(meta)
        outcome = _outcome(meta)
        run_dir = self._path(run_id)
        result.update(
            success=meta["status"] == "completed" and outcome.get("success") is True,
            final_answer=outcome.get("final_answer", ""),
            verification=outcome.get("verification", {}),
            termination_reason=meta.get("termination_reason") or outcome.get("termination_reason"),
            num_steps=outcome.get("num_steps", result["progress"]["current_step"]),
            diagnostics={
                "stdout_log": str(run_dir / "stdout.log"),
                "stderr_log": str(run_dir / "stderr.log"),
                "supervisor_log": str(run_dir / "supervisor.log"),
            },
        )
        result["artifacts"], result["artifacts_truncated"] = _artifact_inventory(
            meta["result_dir"], outcome.get("verification", {}))
        return result

    def cancel_task(self, run_id):
        meta = self._metadata(run_id)
        if meta["status"] not in TERMINAL_STATUSES:
            _write_json(self._path(run_id) / "cancel.json", {"requested_at": time.time()})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = self.get_task(run_id)
                if result["status"] in TERMINAL_STATUSES:
                    return result
                time.sleep(0.05)
        return self.get_task(run_id)

    def capabilities(self):
        busy = False
        try:
            # A read must not slip between start's probe and its worker taking
            # the desktop lease. During that handoff simply report busy.
            _, _, launch_lock = _desktop_paths(self.desktop_id)
            with _FileLock(launch_lock).acquire():
                with self.acquire_external("capabilities-probe"):
                    pass
        except DesktopBusyError:
            busy = True
        return {"desktop_id": self.desktop_id, "busy": busy, "max_concurrent_tasks": 1,
                "runs_root": str(self.runs_root), "poll_after_s": 2,
                "terminal_statuses": sorted(TERMINAL_STATUSES)}

    def close(self):
        """Detach without cancelling work; supervisors keep their lease/timeout."""
        for run_id, worker in list(self._workers.items()):
            if worker.poll() is not None:
                self._workers.pop(run_id, None)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker(sys.argv[2]))
    raise SystemExit("This module's command-line entrypoint is reserved for the task supervisor.")
