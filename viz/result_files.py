"""Open only files named by framework download receipts, on the Studio host."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def download_files(outcome):
    verification = (outcome or {}).get('verification') or {}
    # The final code snapshot takes precedence over an earlier model review.
    completion = verification.get('completion') or verification
    facts = completion.get('download_facts') or []
    files = {}
    for fact in facts:
        if (isinstance(fact, dict) and fact.get('completed') is True
                and fact.get('transport') == 'browser' and isinstance(fact.get('path'), str)):
            key = fact.get('artifact_id') or fact['path']
            files[key] = {**fact, 'id': key}
    return list(files.values())


def open_download(outcome, artifact_id, operation):
    if operation not in {'open', 'reveal'}:
        return {'ok': False, 'error': '不支持的文件操作'}
    record = next((f for f in download_files(outcome) if f['id'] == artifact_id), None)
    if record is None:
        return {'ok': False, 'error': '该文件不在本次任务的下载记录中'}
    path = Path(record['path'])
    try:
        if not path.is_absolute() or not path.is_file():
            return {'ok': False, 'error': '当前执行端找不到该文件，可能已移动或删除。请在原执行电脑连接文鳐后打开。'}
        stat = path.stat()
        identity = record.get('file_identity')
        if (path.is_symlink() or (identity and list(identity) != [stat.st_dev, stat.st_ino])
                or (record.get('bytes') is not None and stat.st_size != record['bytes'])):
            return {'ok': False, 'error': '当前文件与下载记录不一致，未打开。请检查原执行电脑及文件路径。'}
        if operation == 'open' and sys.platform == 'win32':
            os.startfile(str(path))
        else:
            if sys.platform == 'win32':
                command = ['explorer.exe', '/select,', str(path)]
            elif sys.platform == 'darwin':
                command = ['open', '-R', str(path)] if operation == 'reveal' else ['open', str(path)]
            else:
                command = ['xdg-open', str(path.parent if operation == 'reveal' else path)]
            # macOS/Linux launchers return after handing off, not after the user closes the file.
            if sys.platform == 'win32':
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, check=True)
        return {'ok': True, 'message': '已向执行电脑发送' + ('打开文件' if operation == 'open' else '显示所在文件夹') + '请求'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': '打开程序未及时返回，请检查执行电脑是否已弹出窗口。'}
    except (OSError, subprocess.SubprocessError):
        return {'ok': False, 'error': '执行电脑未能打开文件，请检查桌面会话及默认打开程序。'}
