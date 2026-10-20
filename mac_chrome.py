"""Task-owned macOS Chrome without a launcher window; never close unrelated browsers."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

import httpx

CHROME_DEFAULT = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
_PREFIX = 'osworld_agent_chrome_incognito_'


def _validated_profile(port, path):
    if not path or os.path.islink(path):
        return None
    root = os.path.realpath(tempfile.gettempdir())
    resolved = os.path.realpath(path)
    name = os.path.basename(resolved)
    if os.path.dirname(resolved) != root or not name.startswith(f'{_PREFIX}{port}_'):
        return None
    return resolved


def _listener_pids(port):
    result = subprocess.run(['lsof', '-nP', f'-iTCP:{int(port)}', '-sTCP:LISTEN', '-t'],
                            capture_output=True, text=True, timeout=5)
    return {int(line) for line in result.stdout.splitlines() if line.isdigit()}


def _profile_pids(profile):
    profile = os.path.realpath(profile)
    result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True,
                            text=True, timeout=5)
    owned = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        # ps renders unquoted argument spaces, including Application Support.
        match = re.search(r'(?:^|\s)--user-data-dir=(?:"([^"]+)"|\'([^\']+)\'|(.+?))(?=\s--[A-Za-z]|$)', fields[1])
        if match and os.path.realpath(next(value for value in match.groups() if value)) == profile:
            owned.add(int(fields[0]))
    return owned


def _cdp_ready(port):
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            response = client.get(f'http://localhost:{port}/json/version')
            response.raise_for_status()
            return bool(response.json().get('webSocketDebuggerUrl'))
    except (httpx.HTTPError, ValueError):
        return False


def ensure_chrome(port: int, chrome_path: str = CHROME_DEFAULT, *, pdf_downloads=False):
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('浏览器调试端口必须在 1 到 65535 之间')
    if _listener_pids(port):
        raise RuntimeError(f'端口 {port} 已被占用，请选择空闲浏览器端口')
    if not os.path.isfile(chrome_path):
        raise FileNotFoundError(f'找不到 Chrome：{chrome_path}')
    profile = tempfile.mkdtemp(prefix=f'{_PREFIX}{port}_', dir=tempfile.gettempdir())
    proc = None
    try:
        if pdf_downloads:
            from .browser_profile import prepare_pdf_download_profile
            prepare_pdf_download_profile(profile)
        proc = subprocess.Popen([chrome_path, f'--remote-debugging-port={port}',
                                 f'--user-data-dir={profile}', '--no-startup-window',
                                 '--no-first-run', '--no-default-browser-check'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):
            time.sleep(.25)
            listeners = _listener_pids(port)
            if listeners and listeners.issubset(_profile_pids(profile)) and _cdp_ready(port):
                return True, profile
        raise RuntimeError(f'本次 Chrome 未在端口 {port} 建立可确认的专用 CDP 会话')
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        close_test_chrome(port, profile)
        raise


def close_test_chrome(port: int, user_data_dir: str | None):
    profile = _validated_profile(port, user_data_dir)
    if profile is None:
        return
    # Chrome may stop listening on its CDP port before the window exits, or another
    # process may acquire it. Only the unique task profile determines what to kill.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        remaining = _profile_pids(profile)
        if not remaining:
            break
        for pid in remaining:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for _ in range(30):
            if not _profile_pids(profile):
                break
            time.sleep(.1)
    if not _profile_pids(profile):
        shutil.rmtree(profile)
