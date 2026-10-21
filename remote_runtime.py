"""Launch the planning runtime on A with a browser environment bound to B."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from .local_runtime import LocalLaunch, public_config, wait_cli_args


def validate_remote_profile(cfg):
    """Only the operator's profile selects the executor; task text never can."""
    remote = cfg.get('browser_executor')
    allowed = {'endpoint', 'device_id', 'token_env', 'timeout_s', 'idle_timeout_s', 'width', 'height'}
    if not isinstance(remote, dict) or remote.keys() - allowed:
        raise ValueError('browser_executor must contain endpoint, device_id, token_env and optional timing/viewport settings')
    remote = dict(remote)
    endpoint = remote.get('endpoint')
    if not isinstance(endpoint, str):
        raise ValueError('browser_executor.endpoint is required')
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ValueError('Executor endpoint must be an HTTP(S) origin without credentials, path or query')
    if parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('HTTP executor must use loopback through an authenticated tunnel; direct access requires HTTPS')
    try:
        parsed.port
    except ValueError:
        raise ValueError('Invalid executor port') from None
    device = remote.get('device_id')
    if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', device):
        raise ValueError('browser_executor.device_id must be an explicit stable device name')
    remote.setdefault('token_env', 'YAERO_BROWSER_TOKEN')
    if not isinstance(remote['token_env'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', remote['token_env']):
        raise ValueError('browser_executor.token_env must name an environment variable')
    for field, default, low, high in [('timeout_s', 30, 1, 300), ('idle_timeout_s', 60, 10, 300),
                                      ('width', 1280, 320, 4096), ('height', 800, 240, 4096)]:
        value = remote.setdefault(field, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'browser_executor.{field} must be between {low} and {high}')
        if field in {'width', 'height'} and type(value) is not int:
            raise ValueError(f'browser_executor.{field} must be an integer')
    for field in ('workspace_dir', 'browser_profile_dir', 'browser_session_mode', 'browser_port', 'chrome', 'cli_only'):
        if cfg.get(field):
            raise ValueError(f'{field} is not an A-side remote profile setting; configure browser/workspace on B')
    remote['endpoint'] = endpoint.rstrip('/')
    return remote


def build_remote_launch(cfg, result_dir, python_executable=None):
    remote = validate_remote_profile(cfg)
    task = cfg.get('task')
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task must be nonempty')
    if type(cfg.get('max_steps', 40)) is not int or not 1 <= cfg.get('max_steps', 40) <= 200:
        raise ValueError('max_steps must be between 1 and 200')
    # Reuse framework validation, without importing any local GUI environment.
    wait_cli_args(cfg)
    from .prompt_settings import validate_mode_prompts
    from .verifiers import CompletionVerifier
    validate_mode_prompts(cfg.get('mode_prompts'))
    contract = CompletionVerifier(task, cfg.get('completion_spec')).spec
    if len(os.environ.get(remote['token_env'], '')) < 16:
        raise ValueError('Executor token environment variable on A must contain at least 16 characters')
    values = dict(cfg, browser_executor=remote, task=task.strip(), completion_spec=contract)
    child_env = os.environ.copy()
    for field in ('model_api_key', 'ground_api_key'):
        value = values.get(field) or 'EMPTY'
        if not isinstance(value, str):
            raise ValueError(f'{field} must be a credential or environment reference')
        if value != 'EMPTY' and not re.fullmatch(r'(?:env:[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})', value.strip()):
            variable = 'YAERO_CHILD_' + field.upper()
            child_env[variable] = value
            values[field] = 'env:' + variable
    result_path = Path(result_dir).expanduser().resolve()
    inputs = result_path / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    config_path = inputs / 'remote_run.json'
    config_path.write_text(json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    package_root = Path(__file__).resolve().parent
    command = [str(python_executable or os.getenv('OSWORLD_RUNNER_PYTHON') or sys.executable),
               str(package_root / 'launch.py'), 'osworld_agent.run_remote',
               '--config', str(config_path), '--result-dir', str(result_path)]
    child_env.update(PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', AGENTS_VIZ_SERVER_URL='')
    metadata = public_config({**values, 'execution_mode': 'remote_browser',
                              'planning_location': 'server_host', 'execution_device_id': remote['device_id'],
                              'artifact_access': 'executor_originals_and_server_evidence', 'result_dir': str(result_path)})
    return LocalLaunch(command, str(package_root.parent), child_env, metadata)
