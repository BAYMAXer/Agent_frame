"""Bounded OS window readiness and keyboard focus checks; no policy decisions."""
from __future__ import annotations

import math
import time

from .desktop_state import windows_runtime_state


def foreground(runtime):
    hits = [w for w in runtime.get('applications', []) if w.get('foreground')]
    return hits[0] if len(hits) == 1 else None


def window_identity(window):
    return tuple((window or {}).get(k) for k in ('hwnd', 'pid', 'process_name'))


def window_ready(window):
    """OS readiness only; it does not prove that folder contents have loaded."""
    if not window or not window.get('hwnd') or not window.get('pid'):
        return False
    bounds = window.get('bounds')
    return (window.get('visible') is True and window.get('foreground') is True
            and window.get('minimized') is False and window.get('cloaked') is False
            and window.get('enabled') is True and window.get('responsive') is True
            and isinstance(bounds, (list, tuple)) and len(bounds) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in bounds)
            and bounds[2] > bounds[0] and bounds[3] > bounds[1])


def matches_target(window, target):
    if not window:
        return False
    if target.get('hwnd') and window_identity(window) != window_identity(target):
        return False
    expected = str(target.get('process_name', '')).casefold().removesuffix('.exe')
    if str(window.get('process_name', '')).casefold().removesuffix('.exe') != expected:
        return False
    return expected != 'explorer' or window.get('class_name') in {'CabinetWClass', 'ExploreWClass'}


def launch_target(action):
    keys = {str(k).lower() for k in action.args.get('keys', [])} if action.action == 'hotkey' else set()
    if keys in ({'win', 'e'}, {'winleft', 'e'}, {'winright', 'e'}):
        return {'process_name': 'explorer.exe', 'label': '资源管理器'}
    return None


def global_shortcut(action):
    if action.action != 'hotkey':
        return False
    keys = {str(k).lower() for k in action.args.get('keys', [])}
    return bool(keys & {'win', 'winleft', 'winright'}) or keys in ({'alt', 'tab'}, {'alt', 'shift', 'tab'}, {'ctrl', 'shift', 'esc'})


def wait_for_window(target, *, timeout_s=15, poll_s=.25, emit=None, cancelled=None,
                    remaining=None, read_state=None, clock=None, sleep=None):
    """Require the target in the foreground with stable bounds on two polls.

    An existing target window is valid; desktop/taskbar windows never count as
    Explorer. Poll OS metadata rather than interpreting arbitrary pixel changes.
    """
    read_state, clock, sleep = read_state or windows_runtime_state, clock or time.monotonic, sleep or time.sleep
    start, stable, last_key, next_event = clock(), 0, None, 0
    base = {'reason': 'desktop_window', 'target': target.get('label') or target.get('process_name'),
            'timeout_s': timeout_s, 'source': 'windows_window_api'}
    def publish(status, **extra):
        event = dict(base, status=status, elapsed_s=round(clock() - start, 3), **extra)
        if emit:
            emit(event)
        return event
    publish('waiting')
    while True:
        elapsed = clock() - start
        if cancelled and cancelled():
            return publish('cancelled', detail='窗口等待已取消')
        if remaining and remaining() <= 0:
            return publish('task_timeout', detail='任务总时间预算已耗尽')
        if elapsed >= timeout_s:
            return publish('timeout', detail='未确认目标窗口在前台且可接收输入；不要继续发送地址栏或文字输入')
        runtime = read_state()
        current = foreground(runtime)
        if matches_target(current, target) and window_ready(current):
            key = (*window_identity(current), tuple(current['bounds']))
            stable = stable + 1 if key == last_key else 1
            last_key = key
            if stable >= 2:
                return publish('completed', window=current, detail='操作系统确认目标窗口在前台、未最小化且有响应；内容加载另行核查')
        else:
            stable, last_key = 0, None
        if elapsed >= next_event:
            publish('waiting', detail='等待目标窗口出现并稳定进入前台')
            next_event = elapsed + 1
        delay = min(poll_s, timeout_s - (clock() - start))
        if remaining:
            delay = min(delay, remaining())
        if delay > 0:
            sleep(delay)


class DesktopFocusError(RuntimeError):
    pass


def check_keyboard_focus(observed, current, *, pending_target=None, capture_stable=True):
    """Check OS evidence again immediately before keyboard input; never steal focus."""
    before, now = foreground(observed), foreground(current)
    if pending_target and not matches_target(now, pending_target):
        raise DesktopFocusError('目标应用尚未进入前台；请重新观察或选择目标窗口，不能继续输入')
    if (not capture_stable or not before or not now
            or window_identity(before) != window_identity(now)):
        raise DesktopFocusError('桌面焦点与决策截图不一致；请重新 observe_desktop 后再操作')
    if not window_ready(now):
        raise DesktopFocusError('当前窗口未确认可接收输入；请等待或重新观察')
    return now
