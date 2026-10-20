"""任务配置加载：从 OSWorld 的 evaluation_examples 里按 task_id 找 task_config。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def find_task_config_path(task_id: str, examples_dir: str) -> Optional[Path]:
    """按 task_id 找配置 json：先直连 {task_id}.json，再递归找 {task_id}/{task_id}.json 或 {task_id}.json。"""
    base = Path(examples_dir)
    if not base.exists():
        return None
    direct = base / f"{task_id}.json"
    if direct.exists():
        return direct
    matches = list(base.rglob(f"{task_id}.json"))
    if matches:
        return matches[0]
    return None


def load_task_config(task_id: str, examples_dir: str) -> Dict[str, Any]:
    """加载 task 配置 dict（含 instruction / evaluator / setup 等）。"""
    p = find_task_config_path(task_id, examples_dir)
    if p is None:
        raise FileNotFoundError(f"找不到任务配置: {task_id}（在 {examples_dir} 下）")
    with open(p, encoding="utf-8") as f:
        return json.load(f)
