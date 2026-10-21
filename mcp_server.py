"""Task-level MCP adapter; model credentials and launch options stay server-side."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .local_runtime import build_local_launch
from .remote_runtime import build_remote_launch, validate_remote_profile
from .run_service import RunService, RunServiceError


ROOT = Path(__file__).resolve().parent
MAX_STEPS = 200
MAX_TIMEOUT_S = 3600
REQUIRED_PROFILE_FIELDS = ("model", "model_url", "ground_model", "ground_url")


@dataclass(frozen=True)
class ServerConfig:
    profiles: dict[str, dict[str, Any]]
    runs_root: str | None = None
    runner_python: str | None = None


def load_config(path: str | Path) -> ServerConfig:
    """Resolve paths relative to the config, never the MCP host's working directory."""
    source = Path(path).expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Cannot read MCP config {source}. Copy config/mcp.example.json to "
            "config/mcp.local.json and configure your models."
        ) from exc
    if not isinstance(data, dict) or set(data) - {"profiles", "runs_root", "runner_python"}:
        raise ValueError("MCP config supports only profiles, runs_root and runner_python")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("MCP config needs a nonempty profiles object")

    def resolve_path(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Configuration paths must be nonempty strings")
        candidate = Path(value).expanduser()
        return str((candidate if candidate.is_absolute() else source.parent / candidate).resolve())

    normalized = {}
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(profile, dict):
            raise ValueError("Each profile must have a nonempty name and an object value")
        profile = dict(profile)
        for field in REQUIRED_PROFILE_FIELDS:
            if not isinstance(profile.get(field), str) or not profile[field].strip():
                raise ValueError(f"Profile {name!r} needs a nonempty {field}")
        mode = profile.get('execution_mode', 'local')
        if mode not in {'local', 'remote_browser'}:
            raise ValueError(f'Profile {name!r} has an unsupported execution_mode')
        if mode == 'remote_browser':
            profile['browser_executor'] = validate_remote_profile(profile)
        for field in ("workspace_dir", "browser_profile_dir"):
            if profile.get(field):
                profile[field] = resolve_path(profile[field])
        normalized[name] = profile
    return ServerConfig(
        profiles=normalized,
        runs_root=resolve_path(data["runs_root"]) if data.get("runs_root") else None,
        runner_python=resolve_path(data["runner_python"]) if data.get("runner_python") else None,
    )


def _profile_problem(profile: dict) -> str | None:
    if profile.get('execution_mode', 'local') == 'remote_browser':
        try:
            remote = validate_remote_profile(profile)
        except ValueError as exc:
            return str(exc)
        if len(os.environ.get(remote['token_env'], '')) < 16:
            return 'Executor token environment variable on the server host must contain at least 16 characters'
    for field in REQUIRED_PROFILE_FIELDS:
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip() or value.startswith("YOUR_"):
            return f"Configure {field} in the server profile before starting a task"
    # Validate credential references without returning their values or sending a request.
    from .config import resolve_secret
    for field in ("model_api_key", "ground_api_key"):
        value = profile.get(field)
        if value:
            try:
                resolve_secret(value)
            except (TypeError, RuntimeError):
                return f"Credential reference for {field} is unavailable in the server environment"
    return None


def create_server(
    config: ServerConfig,
    *,
    service: RunService | None = None,
    launch_builder=None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> FastMCP:
    """Factory also used by tests with a harmless subprocess launch builder."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This version serves HTTP on loopback only; use an authenticated tunnel remotely")
    service = service or RunService(runs_root=config.runs_root)
    # All IDs remain in one durable registry; exclusion is keyed by the bound
    # executor device, so A's local desktop is never leased by a remote run.
    profile_services = {}
    for name, profile in config.profiles.items():
        if profile.get('execution_mode', 'local') == 'remote_browser':
            remote = validate_remote_profile(profile)
            profile_services[name] = RunService(runs_root=service.runs_root,
                                                desktop_id='remote:' + remote['device_id'])
        else:
            profile_services[name] = service

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            # Independent supervisors retain execution and the desktop lease.
            service.close()
            for selected in profile_services.values():
                if selected is not service:
                    selected.close()

    mcp = FastMCP(
        "yaero",
        instructions=(
            "Delegate complete goals to the selected Yaero profile. Remote browser profiles "
            "keep planning and verification on the server and operate only their bound executor device. "
            "Start returns a run_id; poll that ID and read get_result. Do not operate that browser concurrently. "
            "Reuse request_id when retrying the same submission. Completed process status is "
            "not proof of success: inspect success and verification. Check ok before using results."
        ),
        host=host,
        port=port,
        json_response=True,
        stateless_http=True,
        lifespan=lifespan,
    )

    def invoke(operation, *args, **kwargs) -> dict:
        try:
            return {"ok": True, **operation(*args, **kwargs)}
        except RunServiceError as exc:
            return {"ok": False, "error": {"code": getattr(exc, "code", "run_error"), "message": str(exc)}}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}}
        except OSError:
            # OS error strings can contain a command line or private launch paths.
            return {"ok": False, "error": {"code": "io_error", "message": "Cannot access the task store or launch the runner"}}

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @mcp.tool(annotations=read_only)
    def get_capabilities() -> dict[str, Any]:
        """Inspect desktop availability and configured profile names without launching a task."""
        profiles = []
        for name, profile in config.profiles.items():
            problem = _profile_problem(profile)
            mode = profile.get('execution_mode', 'local')
            profiles.append({"name": name, "configured": problem is None,
                             "configuration_issue": problem, 'execution_mode': mode,
                             'device_id': profile.get('browser_executor', {}).get('device_id') if mode == 'remote_browser' else None,
                             'browser_only': mode == 'remote_browser',
                             'execution_location': 'bound_executor' if mode == 'remote_browser' else 'server_host',
                             'availability': profile_services[name].capabilities(),
                             'executor_connectivity_checked': False})
        modes = {p['execution_mode'] for p in profiles}
        representative = next(iter(profile_services.values()), service)
        return invoke(lambda: {
            **representative.capabilities(),
            "service": "yaero",
            "platform": sys.platform,
            "profiles": profiles,
            "limits": {"max_steps": MAX_STEPS, "max_timeout_s": MAX_TIMEOUT_S},
            "poll_after_s": 3,
            "planning_location": "server_host",
            "execution_location": "bound_executor" if modes == {'remote_browser'} else "server_host" if modes <= {'local'} else "profile_dependent",
            "artifact_access": "executor_originals_and_server_evidence" if 'remote_browser' in modes else "paths_on_server_host",
            "model_connectivity_checked": False,
        })

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
    ))
    def start_task(
        task: Annotated[str, Field(min_length=1, max_length=20000, description="Complete goal, exact targets, output requirements and user constraints")],
        request_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$", description="Unique idempotency key; reuse only for identical submission retries")],
        profile: str = "default-browser",
        max_steps: Annotated[int, Field(ge=1, le=MAX_STEPS, strict=True)] = 40,
        timeout_s: Annotated[int, Field(ge=5, le=MAX_TIMEOUT_S, strict=True)] = 600,
        completion_spec: dict | None = None,
    ) -> dict[str, Any]:
        """Start one goal asynchronously. Save run_id and poll it; desktop_busy means no new run was started."""
        if profile not in config.profiles:
            return {"ok": False, "error": {"code": "unknown_profile", "message": "Choose a profile returned by get_capabilities"}}
        if not task.strip():
            return {"ok": False, "error": {"code": "invalid_request", "message": "Task must not be blank"}}
        cfg = {
            **config.profiles[profile],
            "task": task.strip(),
            "max_steps": max_steps,
            "task_timeout_s": timeout_s,
            "completion_spec": completion_spec,
        }
        # Hash trusted profile configuration so an old request cannot silently change its execution.
        signature = hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        def prepare(_run_id, result_dir):
            problem = _profile_problem(cfg)
            if problem:
                raise ValueError(problem)
            builder = launch_builder or (build_remote_launch if cfg.get('execution_mode') == 'remote_browser' else build_local_launch)
            return builder(cfg, result_dir, python_executable=config.runner_python)

        return invoke(
            profile_services[profile].start,
            task=cfg["task"],
            request_id=request_id,
            max_steps=max_steps,
            timeout_s=timeout_s,
            signature=signature,
            launch_factory=prepare,
        )

    @mcp.tool(annotations=read_only)
    def get_task(run_id: str) -> dict[str, Any]:
        """Read a previously returned run_id. Poll after poll_after_s; connection loss does not cancel the job."""
        return invoke(service.get_task, run_id)

    @mcp.tool(annotations=read_only)
    def get_result(run_id: str) -> dict[str, Any]:
        """Read verdict and artifacts; distinguish B delivery originals from A evidence paths. Check success and verification."""
        return invoke(service.get_result, run_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True))
    def cancel_task(run_id: str) -> dict[str, Any]:
        """Request cancellation of exactly this run. Poll until terminal; cancellation cannot undo completed actions."""
        return invoke(service.cancel_task, run_id)

    return mcp


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Yaero task-level MCP server")
    parser.add_argument("--config", default=os.getenv("YAERO_MCP_CONFIG", str(ROOT / "config" / "mcp.local.json")))
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        config = load_config(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    server = create_server(config, host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
