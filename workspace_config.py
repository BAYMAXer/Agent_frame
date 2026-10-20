"""Shared local workspace root; tasks on the same day share one dated folder."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import uuid


def _settings_path() -> Path:
    return Path(__file__).resolve().parent / 'viz' / 'studio_settings.json'


def default_local_runs_root() -> str:
    """Logs belong to the application, independently of the selected workspace."""
    root = os.environ.get('OSWORLD_LOCAL_RUNS_DIR') or Path(__file__).resolve().parent / 'local_runs'
    return str(Path(root).expanduser().resolve())


def allocate_local_result_dir(root=None) -> str:
    """Reserve a unique run directory; never overwrite another run's logs."""
    base = Path(root or default_local_runs_root()).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        run_id = 'ui_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
        result = base / run_id
        try:
            result.mkdir()
            return str(result)
        except FileExistsError:
            continue
    raise RuntimeError('无法分配独立运行日志目录，请重试')


def normalize_workspace_root(value: str, *, require_existing: bool = True) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError('请选择有效的工程文件夹')
    expanded = os.path.expanduser(value.strip())
    if not os.path.isabs(expanded):
        raise ValueError('工程文件夹必须使用绝对路径')
    root = os.path.normpath(expanded)
    if require_existing and not os.path.isdir(root):
        raise ValueError('工程文件夹不存在或不是文件夹')
    return root


def default_workspace_root(*, prefer_env: bool = False) -> str:
    """Use a saved choice when present; keep CLI environment overrides available."""
    try:
        saved = json.loads(_settings_path().read_text(encoding='utf-8')).get('workspace_root')
    except (OSError, ValueError, AttributeError):
        saved = None
    env = os.environ.get('OSWORLD_WORKSPACE_DIR')
    choices = ((env, False), (saved, True)) if prefer_env else ((saved, True), (env, False))
    for candidate, require_existing in choices:
        if candidate:
            try:
                return normalize_workspace_root(candidate, require_existing=require_existing)
            except ValueError:
                pass
    documents = Path.home() / 'Documents'
    return str(documents if documents.is_dir() else Path.home())


def save_workspace_root(value: str) -> str:
    root = normalize_workspace_root(value)
    settings = _settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=settings.parent,
                                         prefix='.studio_settings_', delete=False) as file:
            temporary = file.name
            json.dump({'workspace_root': root}, file, ensure_ascii=False)
            file.write('\n')
        os.replace(temporary, settings)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return root
