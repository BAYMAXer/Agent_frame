"""Launch an explicitly owned, persistent task profile; never adopt daily Chrome."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

_MARKER = '.agent-frame-task-profile.json'
_KIND = 'agent-frame-persistent-task-profile-v1'
_LAUNCHES = {}


def browser_data_root(platform=None):
    platform = platform or ('windows' if sys.platform == 'win32' else 'mac' if sys.platform == 'darwin' else 'linux')
    if platform == 'windows':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'AgentFrame'
    if platform == 'mac':
        return Path.home() / 'Library' / 'Application Support' / 'AgentFrame'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'AgentFrame'


def default_profile_dir(platform=None):
    return str(browser_data_root(platform) / 'Browser' / 'TaskProfile')


def _settings_path():
    return browser_data_root() / 'browser-session.json'


def browser_settings():
    try:
        value = json.loads(_settings_path().read_text(encoding='utf-8'))
        mode = value.get('browser_session_mode', 'persistent')
        profile = value.get('browser_profile_dir') or ''
        if not isinstance(mode, str) or mode not in {'persistent', 'isolated'} or not isinstance(profile, str):
            raise ValueError('invalid saved browser settings')
    except (OSError, ValueError, AttributeError):
        mode, profile = 'persistent', ''
    return {'browser_session_mode': mode, 'browser_profile_dir': profile,
            'effective_profile_dir': profile or default_profile_dir()}


def save_browser_settings(mode, profile=''):
    if not isinstance(mode, str) or mode not in {'persistent', 'isolated'}:
        raise ValueError('浏览器会话模式必须是 persistent 或 isolated')
    if not isinstance(profile, str):
        raise ValueError('浏览器配置目录必须是绝对路径')
    profile = profile.strip()
    if profile:
        profile = str(_profile_path(profile))
    settings = _settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    value = {'browser_session_mode': mode, 'browser_profile_dir': profile}
    temporary = settings.with_name(settings.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temporary, settings)
    finally:
        temporary.unlink(missing_ok=True)
    return browser_settings()


def _profile_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
        raise ValueError('浏览器配置目录必须是绝对路径')
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('任务浏览器配置目录不能是符号链接')
    resolved = path.resolve()
    folded = str(resolved).replace('\\', '/').casefold()
    forbidden = ('/google/chrome/user data', '/google/chrome', '/chromium/user data', '/microsoft/edge/user data')
    if any(folded.endswith(part) or part + '/' in folded for part in forbidden):
        raise ValueError('不能接管日常 Chrome/Edge 配置；请选择独立的任务配置目录')
    if resolved == Path.home().resolve() or resolved == Path(resolved.anchor):
        raise ValueError('请选择独立的任务浏览器配置子目录')
    return resolved


def _marker(profile):
    try:
        value = json.loads((Path(profile) / _MARKER).read_text(encoding='utf-8'))
        if value.get('kind') == _KIND and isinstance(value.get('owner_id'), str) and len(value['owner_id']) == 32:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def prepare_persistent_profile(value=None, *, platform=None):
    profile = _profile_path(value or default_profile_dir(platform))
    profile.mkdir(parents=True, exist_ok=True)
    owner = _marker(profile)
    if owner is None:
        if any(profile.iterdir()):
            raise ValueError('配置目录已有数据且没有 AgentFrame 所有权标记，不能接管；请选择新的空目录')
        owner = {'kind': _KIND, 'owner_id': uuid.uuid4().hex}
        try:
            with (profile / _MARKER).open('x', encoding='utf-8') as stream:
                json.dump(owner, stream)
        except FileExistsError:
            if _marker(profile) is None:
                raise ValueError('配置目录所有权标记无效')
    return str(profile)


def _port_open(port):
    with socket.socket() as connection:
        connection.settimeout(.3)
        return connection.connect_ex(('127.0.0.1', int(port))) == 0


def _processes(platform, profile, port):
    """Return profile PIDs and port owners; uncertainty must fail closed."""
    if platform == 'windows':
        from .run_windows_local import _chrome_processes, _profile_processes
        rows = _chrome_processes()
        if rows is None:
            raise RuntimeError('无法核实任务浏览器进程归属')
        owned = {row['pid'] for row in _profile_processes(profile, rows)}
        listeners = {row['pid'] for row in rows if port in (row.get('ports') or [])}
        return owned, listeners
    from .mac_chrome import _listener_pids, _profile_pids
    return _profile_pids(profile), _listener_pids(port)


def _endpoint_owned(platform, profile, port):
    owned, listeners = _processes(platform, profile, port)
    return bool(listeners) and listeners.issubset(owned)


def ensure_persistent_chrome(port, chrome_path, *, profile_dir=None, platform=None):
    platform = platform or ('windows' if sys.platform == 'win32' else 'mac')
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('浏览器调试端口必须在 1 到 65535 之间')
    profile = prepare_persistent_profile(profile_dir, platform=platform)
    from .mac_chrome import _cdp_ready
    if _port_open(port):
        if not _endpoint_owned(platform, profile, port) or not _cdp_ready(port):
            raise RuntimeError(f'端口 {port} 不属于指定的固定任务配置，请使用空闲端口；不会接管其他浏览器')
        return False, profile
    owned, _ = _processes(platform, profile, port)
    if owned:
        raise RuntimeError('固定任务配置已在其他端口运行；请先关闭该任务浏览器或使用其调试端口')
    if not os.path.isfile(chrome_path):
        raise FileNotFoundError(f'找不到 Chrome：{chrome_path}')
    proc = subprocess.Popen([chrome_path, f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}', '--no-startup-window', '--no-first-run',
        '--no-default-browser-check'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, **({'start_new_session': True} if platform != 'windows' else {}))
    _LAUNCHES[(profile, port)] = {'pid': proc.pid, 'owner_id': _marker(profile)['owner_id'], 'platform': platform}
    try:
        for _ in range(60):
            time.sleep(.25)
            if proc.poll() is not None:
                break
            if _cdp_ready(port) and _endpoint_owned(platform, profile, port):
                return True, profile
        raise RuntimeError('固定任务浏览器未建立可确认的 CDP 连接')
    except BaseException:
        close_persistent_chrome(port, profile)
        raise


def close_persistent_chrome(port, profile):
    """Close only this process's launch; never erase its saved login state."""
    if not profile:
        return
    profile, port = str(Path(profile).resolve()), int(port)
    launch = _LAUNCHES.pop((profile, port), None)
    owner = _marker(profile)
    if not launch or not owner or owner['owner_id'] != launch['owner_id']:
        return
    platform, pid = launch['platform'], launch['pid']
    def still_owned():
        try:
            return pid in _processes(platform, profile, port)[0]
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False
    if not still_owned():
        return
    try:
        from playwright.sync_api import sync_playwright
        from .utils import cdp_websocket_endpoint
        if _endpoint_owned(platform, profile, port):
            with sync_playwright() as runtime:
                browser = runtime.chromium.connect_over_cdp(cdp_websocket_endpoint('localhost', port), timeout=3000)
                try:
                    if still_owned() and _endpoint_owned(platform, profile, port):
                        browser.new_browser_cdp_session().send('Browser.close')
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception:
        pass
    for _ in range(20):
        if not still_owned():
            return
        time.sleep(.1)
    if still_owned():
        if platform == 'windows':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=10)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def main():
    parser = argparse.ArgumentParser(description='打开固定任务浏览器，手动登录后保存 Cookie；不启动智能体')
    parser.add_argument('--browser-port', type=int, default=9222)
    parser.add_argument('--browser-profile-dir')
    parser.add_argument('--chrome')
    parser.add_argument('--url', default='about:blank')
    args = parser.parse_args()
    platform = 'windows' if sys.platform == 'win32' else 'mac'
    if args.url != 'about:blank' and not args.url.startswith(('https://', 'http://')):
        parser.error('--url 必须是 HTTP(S) 地址或 about:blank')
    if args.chrome:
        chrome = args.chrome
    elif platform == 'windows':
        from .run_windows_local import CHROME_DEFAULT as chrome
    else:
        from .mac_chrome import CHROME_DEFAULT as chrome
    created, profile = ensure_persistent_chrome(args.browser_port, chrome,
        profile_dir=args.browser_profile_dir or browser_settings()['browser_profile_dir'] or None,
        platform=platform)
    try:
        if args.browser_profile_dir:
            # Commit only after ownership and a working task endpoint are confirmed.
            save_browser_settings('persistent', profile)
        from playwright.sync_api import sync_playwright
        from .utils import cdp_websocket_endpoint
        with sync_playwright() as runtime:
            browser = runtime.chromium.connect_over_cdp(cdp_websocket_endpoint('localhost', args.browser_port))
            page = browser.contexts[0].new_page()
            page.goto(args.url, wait_until='domcontentloaded')
            print(f'固定任务配置：{profile}\n请在新页面手动登录；完成后回到终端按 Enter。'
                  '保留持久 Cookie 和站点存储；站点会话失效仍需重新登录。', flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
            if not created:
                page.close()
            browser.close()
    finally:
        if created:
            close_persistent_chrome(args.browser_port, profile)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
