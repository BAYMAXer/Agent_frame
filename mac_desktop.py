"""macOS desktop evidence and input. Browser actions continue through BrowserSession/CDP."""
from __future__ import annotations

import ctypes
import io
import math
import re
import time
import uuid

from .actions import Action
from .desktop_capture import validate_desktop_action


def _quartz():
    import Quartz
    return Quartz


def _accessibility_trusted() -> bool:
    framework = ctypes.cdll.LoadLibrary(
        '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
    check = framework.AXIsProcessTrusted
    check.restype = ctypes.c_bool
    return bool(check())


def _display_rects():
    q = _quartz()
    error, displays, count = q.CGGetActiveDisplayList(32, None, None)
    if error or not count:
        raise OSError('macOS 未提供有效显示器列表')
    rects = []
    for display in displays[:count]:
        bounds = q.CGDisplayBounds(display)
        left, top = bounds.origin.x, bounds.origin.y
        width, height = bounds.size.width, bounds.size.height
        values = (left, top, width, height)
        if any(not math.isfinite(v) for v in values) or width <= 0 or height <= 0:
            raise OSError('macOS 显示器坐标无效')
        if any(v != round(v) for v in values):
            raise OSError('macOS 显示器坐标不是整数点，无法可靠定位')
        rects.append((display, *(int(v) for v in values)))
    return rects


def virtual_geometry():
    rects = _display_rects()
    left = min(r[1] for r in rects)
    top = min(r[2] for r in rects)
    right = max(r[1] + r[3] for r in rects)
    bottom = max(r[2] + r[4] for r in rects)
    return {'left': left, 'top': top, 'width': right - left, 'height': bottom - top}


def capture_virtual_desktop():
    """Combine all displays in macOS logical points, matching CGEvent coordinates."""
    q = _quartz()
    if not q.CGPreflightScreenCaptureAccess():
        raise PermissionError('请在 macOS 系统设置中为运行 Python 的程序开启「屏幕录制」权限，然后重新启动程序')
    from AppKit import NSBitmapImageRep, NSPNGFileType
    from PIL import Image

    rects = _display_rects()
    geometry = virtual_geometry()
    shot = Image.new('RGB', (geometry['width'], geometry['height']), 'black')
    for display, left, top, width, height in rects:
        cg_image = q.CGDisplayCreateImage(display)
        if cg_image is None:
            raise OSError('macOS 显示器截图失败')
        rep = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
        png = rep.representationUsingType_properties_(NSPNGFileType, {})
        with Image.open(io.BytesIO(bytes(png))) as image:
            logical = image.convert('RGB').resize((width, height), Image.Resampling.LANCZOS)
        shot.paste(logical, (left - geometry['left'], top - geometry['top']))
    if virtual_geometry() != geometry:
        raise OSError('显示器布局在截图时发生变化，请重新 observe_desktop')
    return shot, {'screen_origin': [geometry['left'], geometry['top']],
                  'virtual_desktop': geometry, 'desktop_capture_id': uuid.uuid4().hex,
                  'captured_at': time.time(), 'desktop_capture_source': 'mac_quartz_logical_points',
                  'screenshot_size': list(shot.size)}


_ALIASES = {
    'file_manager': 'finder', 'file_explorer': 'finder', '文件管理器': 'finder',
    '资源管理器': 'finder', 'finder': 'finder', 'chrome': 'chrome',
    'google_chrome': 'chrome', 'google chrome': 'chrome', 'safari': 'safari',
    'terminal': 'terminal', '访达': 'finder',
}


def _process_name(name, bundle_id=''):
    bundle = str(bundle_id or '').casefold()
    if bundle == 'com.apple.finder':
        return 'finder'
    if bundle == 'com.google.chrome':
        return 'chrome'
    known = {'google chrome': 'chrome', 'finder': 'finder', 'safari': 'safari',
             'terminal': 'terminal'}
    if str(name or '').casefold() in known:
        return known[str(name).casefold()]
    return re.sub(r'[^a-z0-9_-]+', '_', str(name or '').casefold()).strip('_')


def application_process(app_code):
    code = str(app_code or '').strip().casefold()
    if code in _ALIASES:
        return _ALIASES[code]
    return code if re.fullmatch(r'[a-z0-9_-]+', code) else None


def runtime_state():
    """Read top-level windows; fail closed when the window server is unavailable."""
    try:
        q = _quartz()
        from AppKit import NSWorkspace
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        front_pid = int(front.processIdentifier()) if front else None
        windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID)
        if windows is None:
            raise OSError('无法读取 macOS 窗口列表')
        apps = []
        foreground_assigned = False
        for item in windows:
            if int(item.get(q.kCGWindowLayer, 1)) != 0:
                continue
            pid = int(item.get(q.kCGWindowOwnerPID, 0))
            hwnd = int(item.get(q.kCGWindowNumber, 0))
            bounds = item.get(q.kCGWindowBounds) or {}
            x, y = float(bounds.get('X', 0)), float(bounds.get('Y', 0))
            w, h = float(bounds.get('Width', 0)), float(bounds.get('Height', 0))
            if not pid or not hwnd or w <= 0 or h <= 0:
                continue
            name = str(item.get(q.kCGWindowOwnerName) or '')
            foreground = pid == front_pid and not foreground_assigned
            foreground_assigned |= foreground
            apps.append({'process_name': _process_name(name), 'pid': pid, 'hwnd': hwnd,
                         'title': str(item.get(q.kCGWindowName) or name), 'class_name': 'mac_window',
                         'bounds': [x, y, x + w, y + h], 'foreground': foreground,
                         'visible': True, 'minimized': False, 'cloaked': False,
                         'enabled': True, 'responsive': None})
        return {'applications': apps, 'complete': front_pid is not None,
                'source': 'mac_quartz_window_server'}
    except Exception as exc:
        return {'applications': [], 'complete': False, 'source': 'mac_quartz_window_server',
                'error': type(exc).__name__}


class DesktopFocusError(RuntimeError):
    pass


def check_keyboard_focus(observed, current, *, capture_stable=True):
    from .desktop_readiness import foreground, window_identity
    before, now = foreground(observed), foreground(current)
    if (not capture_stable or not before or not now
            or window_identity(before) != window_identity(now)
            or before.get('bounds') != now.get('bounds')):
        raise DesktopFocusError('桌面焦点与决策截图不一致；请重新 observe_desktop 后再操作')
    if current.get('complete') is not True or not now.get('visible') or not now.get('bounds'):
        raise DesktopFocusError('无法确认当前 macOS 窗口可接收输入')
    return now


def switch_application(app_code):
    from AppKit import NSWorkspace
    process = application_process(app_code)
    base = {'action': 'switch_applications', 'app_code': app_code,
            'channel': 'desktop', 'dispatched': False}
    if not process:
        return {**base, 'status': 'rejected', 'error_code': 'unknown_application'}
    matches = [a for a in NSWorkspace.sharedWorkspace().runningApplications()
               if _process_name(a.localizedName(), a.bundleIdentifier()) == process]
    if len(matches) != 1:
        return {**base, 'status': 'rejected', 'error_code': 'application_ambiguous_or_missing'}
    dispatched = bool(matches[0].activateWithOptions_(0))
    current = runtime_state()
    front = next((w for w in current['applications'] if w['foreground']), None)
    observed = bool(front and front['pid'] == int(matches[0].processIdentifier()))
    return {**base, 'dispatched': dispatched, 'status': 'executed' if observed else 'uncertain',
            **({'window': front} if observed else {}),
            **({'error_code': 'foreground_not_confirmed'} if not observed else {})}


def _mouse_event(event_type, x, y, button):
    q = _quartz()
    event = q.CGEventCreateMouseEvent(None, event_type, (x, y), button)
    q.CGEventPost(q.kCGHIDEventTap, event)


def _click(x, y, *, right=False, count=1):
    q = _quartz()
    button = q.kCGMouseButtonRight if right else q.kCGMouseButtonLeft
    down = q.kCGEventRightMouseDown if right else q.kCGEventLeftMouseDown
    up = q.kCGEventRightMouseUp if right else q.kCGEventLeftMouseUp
    for _ in range(count):
        _mouse_event(down, x, y, button)
        _mouse_event(up, x, y, button)
        if count > 1:
            time.sleep(.08)


def _type_unicode(text):
    q = _quartz()
    for char in str(text):
        down = q.CGEventCreateKeyboardEvent(None, 0, True)
        up = q.CGEventCreateKeyboardEvent(None, 0, False)
        q.CGEventKeyboardSetUnicodeString(down, len(char.encode('utf-16-le')) // 2, char)
        q.CGEventKeyboardSetUnicodeString(up, len(char.encode('utf-16-le')) // 2, char)
        q.CGEventPost(q.kCGHIDEventTap, down)
        q.CGEventPost(q.kCGHIDEventTap, up)


def _key(name):
    aliases = {'ctrl': 'command', 'control': 'command', 'cmd': 'command',
               'meta': 'command', 'option': 'alt', 'return': 'enter', 'escape': 'esc'}
    return aliases.get(str(name).lower(), str(name).lower())


def execute_action(action: Action, origin, keyboard_guard=None):
    """Execute an already validated image-local action in macOS screen points."""
    import pyautogui
    if not _accessibility_trusted():
        raise PermissionError('请在 macOS 系统设置中为运行 Python 的程序开启「辅助功能」权限')
    a, name = action.args, action.action
    def point(x='x', y='y'):
        return a[x] + origin[0], a[y] + origin[1]
    def guarded_input():
        if keyboard_guard:
            keyboard_guard()
    if name in {'click', 'double_click', 'right_click'}:
        _click(*point(), right=name == 'right_click', count=2 if name == 'double_click' else 1)
    elif name == 'type':
        if 'x' in a and 'y' in a:
            _click(*point())
        guarded_input()
        if a.get('overwrite'):
            pyautogui.hotkey('command', 'a')
            guarded_input()
            pyautogui.press('backspace')
        _type_unicode(a.get('text', ''))
    elif name == 'select':
        _click(*point())
        guarded_input()
        _type_unicode(a.get('option', ''))
        guarded_input()
        pyautogui.press('enter')
    elif name == 'drag_and_drop':
        q = _quartz()
        x1, y1 = point('x1', 'y1')
        x2, y2 = point('x2', 'y2')
        _mouse_event(q.kCGEventLeftMouseDown, x1, y1, q.kCGMouseButtonLeft)
        _mouse_event(q.kCGEventLeftMouseDragged, x2, y2, q.kCGMouseButtonLeft)
        _mouse_event(q.kCGEventLeftMouseUp, x2, y2, q.kCGMouseButtonLeft)
    elif name == 'scroll':
        q = _quartz()
        amount = int(a.get('amount', 1)) * (1 if a.get('direction') == 'up' else -1)
        event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, amount)
        q.CGEventPost(q.kCGHIDEventTap, event)
    elif name == 'press':
        guarded_input()
        pyautogui.press(_key(a.get('key', '')))
    elif name == 'hotkey':
        guarded_input()
        pyautogui.hotkey(*[_key(k) for k in a.get('keys', [])])
    elif name == 'goto':
        guarded_input()
        pyautogui.hotkey('command', 'l')
        guarded_input()
        _type_unicode(a.get('url', ''))
        guarded_input()
        pyautogui.press('enter')
    elif name == 'wait':
        time.sleep(float(a.get('seconds', 1)))
    else:
        raise ValueError(f'不支持的 macOS 桌面动作: {name}')
