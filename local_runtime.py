"""Build local task launches for Studio and MCP without importing either server."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LocalLaunch:
    command: list[str]
    cwd: str
    env: dict[str, str]
    metadata: dict


def public_config(value):
    """Keep configuration useful for display without copying credentials."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
            if (normalized in {'authorization', 'secret', 'cookie', 'setcookie', 'password', 'passwd'}
                    or normalized.endswith(('apikey', 'token', 'password', 'clientsecret'))):
                continue
            result[key] = public_config(item)
        return result
    if isinstance(value, (tuple, list)):
        return [public_config(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', value)
        return re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[redacted]', value)
    return value


def judge_cli_args(cfg):
    """Both runners use the same validated optional System One configuration."""
    from .model.decision_judge import DecisionJudgeConfig
    data = cfg.get('decision_judge') or {}
    if not isinstance(data, dict):
        raise ValueError('Decision 评估器配置必须是对象')
    if data.get('api_key'):
        raise ValueError('Decision 认证请在服务端设置 DECISION_JUDGE_API_KEY')
    explicit_selection = 'selection_policy' in data
    data = dict(data)
    selection_policy = data.pop('selection_policy', 'maximum_probability')
    if not isinstance(selection_policy, str) or selection_policy not in {'threshold', 'maximum_probability'}:
        raise ValueError('Jev 标签选择必须为 threshold 或 maximum_probability')
    try:
        value = DecisionJudgeConfig(**data)
    except (TypeError, ValueError) as exc:
        raise ValueError('Decision 评估器配置无效') from exc
    if not value.url:
        return []
    selection_args = ['--judge-selection-policy', selection_policy] if explicit_selection else []
    return ['--judge-url', value.url, '--judge-model', value.model, '--judge-revision', value.revision,
            '--judge-threshold', str(value.threshold), '--judge-margin', str(value.margin),
            '--judge-timeout', str(value.timeout), '--judge-review-mode', value.review_mode
            ] + selection_args + ([] if value.recheck_unknown else ['--judge-no-recheck'])


def wait_cli_args(cfg):
    from .knowledge_selection import selection_cli_args
    result = judge_cli_args(cfg) + selection_cli_args(cfg)
    for field, flag, default in (('operation_timeout_s', '--operation-timeout', 300),
                                ('task_timeout_s', '--task-timeout', 1800)):
        try:
            value = float(cfg.get(field, default))
        except (TypeError, ValueError):
            raise ValueError('等待时限必须是大于零的秒数') from None
        if isinstance(cfg.get(field), bool) or not math.isfinite(value) or value <= 0:
            raise ValueError('等待时限必须是大于零的秒数')
        result += [flag, str(value)]
    if 'download_max_mib' in cfg:
        value = cfg['download_max_mib']
        if type(value) is not int or value < 0:
            raise ValueError('下载大小上限必须为非负整数 MiB，0 表示不限')
        result += ['--download-max-mib', str(value)]
    return result


def build_local_launch(cfg, result_dir, python_executable=None, viz_port=None):
    """Validate a task and snapshot its inputs; never launch or operate the GUI.

    The caller allocates a unique result directory and owns desktop exclusion.
    Browser preferences are read, but only Studio explicitly saves changes.
    """
    from .persistent_chrome import browser_settings, _profile_path
    from .prompt_settings import prompt_cli_args
    from .workspace_config import default_workspace_root, normalize_workspace_root

    if not isinstance(cfg, dict):
        raise ValueError('任务配置必须为对象')
    task = cfg.get('task')
    if not isinstance(task, str) or not task.strip():
        raise ValueError('请输入任务描述')
    task = task.strip()
    max_steps = cfg.get('max_steps', 40)
    if isinstance(max_steps, str) and max_steps.isdecimal():
        max_steps = int(max_steps)
    if type(max_steps) is not int or max_steps <= 0:
        raise ValueError('最大步数必须为正整数')
    saved_browser = browser_settings()
    session_mode = cfg.get('browser_session_mode', saved_browser['browser_session_mode'])
    profile_dir = cfg.get('browser_profile_dir', saved_browser['browser_profile_dir']) or ''
    if not isinstance(session_mode, str) or session_mode not in {'persistent', 'isolated'}:
        raise ValueError('浏览器会话模式必须是 persistent 或 isolated')
    if not isinstance(profile_dir, str):
        raise ValueError('浏览器配置目录必须是绝对路径')
    if profile_dir:
        profile_dir = str(_profile_path(profile_dir))
    workspace = normalize_workspace_root(cfg.get('workspace_dir') or default_workspace_root())
    result_path = Path(result_dir).expanduser().resolve()
    package_root = Path(__file__).resolve().parent
    child_secrets = {}

    def credential_argument(field, variable):
        value = str(cfg.get(field) or 'EMPTY')
        if value == 'EMPTY' or re.fullmatch(r'(?:env:[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})', value.strip()):
            return value
        child_secrets[variable] = value
        return 'env:' + variable

    command = [
        str(python_executable or os.getenv('OSWORLD_RUNNER_PYTHON') or sys.executable),
        str(package_root / 'launch.py'), 'osworld_agent.run_local',
        '--task', task,
        '--model', str(cfg.get('model', 'Qwen3.6-27B')),
        '--model-url', str(cfg.get('model_url', 'http://10.44.101.62:8094/v1')),
        '--model-api-key', credential_argument('model_api_key', 'YAERO_CHILD_MODEL_API_KEY'),
        '--ground-model', str(cfg.get('ground_model', 'UI-Venus-2-9B')),
        '--ground-type', str(cfg.get('ground_type', 'qwen-plus')),
        '--ground-url', str(cfg.get('ground_url', 'http://7.246.80.189:8001/v1')),
        '--ground-api-key', credential_argument('ground_api_key', 'YAERO_CHILD_GROUND_API_KEY'),
        '--max-steps', str(max_steps),
        '--browser-session-mode', session_mode,
        '--result-dir', str(result_path),
    ]
    if cfg.get('cli_only') is True:
        command += ['--cli-only']
    if session_mode == 'persistent' and profile_dir:
        command += ['--browser-profile-dir', profile_dir]
    if cfg.get('browser_start_url'):
        start_url = str(cfg['browser_start_url'])
        parsed = urlsplit(start_url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
                or parsed.username or parsed.password
                or parsed.path.rstrip('/') != '/demo/paper-search' or parsed.query or parsed.fragment):
            raise ValueError('演示起始页只能使用本机论文搜索回放页')
        command += ['--browser-start-url', start_url]
    command += wait_cli_args(cfg)
    command += prompt_cli_args(cfg.get('mode_prompts'), result_path / 'inputs')
    command += ['--workspace-dir', workspace]
    if cfg.get('completion_spec') is not None:
        from .verifiers import CompletionVerifier
        try:
            contract = CompletionVerifier(task, cfg['completion_spec']).spec
        except ValueError as exc:
            raise ValueError(f'验收条件无效：{exc}') from exc
        result_path.mkdir(parents=True, exist_ok=True)
        contract_path = result_path / 'completion_spec.json'
        contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding='utf-8')
        command += ['--completion-spec', str(contract_path)]
    env = os.environ.copy()
    env.update(child_secrets)
    env.update(PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
    if viz_port is not None:
        env['AGENTS_VIZ_SERVER_URL'] = f'http://127.0.0.1:{viz_port}'
    else:
        # MCP runs must not send task details to an unrelated Studio listener.
        env['AGENTS_VIZ_SERVER_URL'] = ''
    env['AGENTS_RESULTS_DIR'] = os.getenv('AGENTS_RESULTS_DIR', str(package_root / 'viz' / 'runs'))
    no_proxy = [part.strip() for part in env.get('NO_PROXY', env.get('no_proxy', '')).split(',') if part.strip()]
    for part in ('192.168.0.0/16', '10.0.0.0/8', '7.0.0.0/8', 'localhost', '127.0.0.1'):
        if part not in no_proxy:
            no_proxy.append(part)
    env['NO_PROXY'] = ','.join(no_proxy)
    env['no_proxy'] = env['NO_PROXY']
    metadata = public_config({**cfg, 'task': task, 'max_steps': max_steps,
                              'browser_session_mode': session_mode, 'browser_profile_dir': profile_dir,
                              'workspace_dir': workspace, 'result_dir': str(result_path)})
    return LocalLaunch(command=command, cwd=os.getenv('OSWORLD_AGENT_ROOT', str(package_root.parent)),
                       env=env, metadata=metadata)
