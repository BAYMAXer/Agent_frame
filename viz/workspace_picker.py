"""Local-only native folder picker for the Studio workspace preference."""
from __future__ import annotations

import asyncio
import subprocess
import sys

from fastapi import APIRouter, HTTPException, Request

from ..workspace_config import default_workspace_root, save_workspace_root

router = APIRouter(prefix='/workspace', tags=['Workspace'])


def _require_json(request: Request):
    if not request.headers.get('content-type', '').lower().startswith('application/json'):
        raise HTTPException(415, '需要 JSON 请求')


def choose_directory() -> str | None:
    if sys.platform == 'darwin':
        command = ['osascript', '-e', 'POSIX path of (choose folder with prompt "选择 Agent Frame 工程文件夹")']
    elif sys.platform == 'win32':
        script = ('Add-Type -AssemblyName System.Windows.Forms; '
                  '$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; '
                  '$dialog.Description = "选择 Agent Frame 工程文件夹"; '
                  'if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) '
                  '{ [Console]::Out.WriteLine($dialog.SelectedPath) }')
        command = ['powershell.exe', '-NoProfile', '-STA', '-Command', script]
    else:
        raise RuntimeError('文件夹选择器目前支持 macOS 和 Windows')
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if completed.returncode:
        if not completed.stdout.strip():
            return None  # User cancelled the native dialog.
        raise RuntimeError(completed.stderr.strip() or '文件夹选择失败')
    return completed.stdout.strip() or None


@router.get('/default')
def get_default():
    return {'ok': True, 'workspace_dir': default_workspace_root()}


@router.post('/default')
async def set_default(request: Request):
    _require_json(request)
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(422, '需要 JSON 对象') from None
    if not isinstance(data, dict) or set(data) != {'workspace_dir'}:
        raise HTTPException(422, '需要 workspace_dir')
    try:
        root = await asyncio.to_thread(save_workspace_root, data['workspace_dir'])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {'ok': True, 'workspace_dir': root}


@router.post('/choose')
async def choose_default(request: Request):
    _require_json(request)
    try:
        selected = await asyncio.to_thread(choose_directory)
        if selected is None:
            return {'ok': False, 'cancelled': True}
        root = await asyncio.to_thread(save_workspace_root, selected)
    except subprocess.TimeoutExpired:
        raise HTTPException(422, '文件夹选择对话框超时（可能未弹出或被忽略）；请直接在输入框手动输入绝对路径。')
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    return {'ok': True, 'workspace_dir': root}
