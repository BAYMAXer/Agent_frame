"""OS window evidence and targeted window activation, never file operations."""
import os
import sys
import re
import time


def windows_runtime_state():
    if sys.platform != "win32":
        return {"applications": [], "complete": False}
    try:
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        for name in ('IsIconic', 'IsWindowEnabled', 'IsHungAppWindow'):
            getattr(user, name).argtypes = [wintypes.HWND]
        dwm = ctypes.WinDLL('dwmapi', use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        foreground = user.GetForegroundWindow()
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        apps, errors = [], []

        @callback_type
        def visit(hwnd, _):
            handle = None
            try:
                if not user.IsWindowVisible(hwnd):
                    return True
                title = ctypes.create_unicode_buffer(1024)
                user.GetWindowTextW(hwnd, title, len(title))
                if not title.value:
                    return True
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                handle = kernel.OpenProcess(0x1000, False, pid.value)
                path = ctypes.create_unicode_buffer(32768)
                length = wintypes.DWORD(len(path))
                if not handle or not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                    errors.append(True)
                    return True
                class_name = ctypes.create_unicode_buffer(256)
                user.GetClassNameW(hwnd, class_name, len(class_name))
                rect, cloaked = wintypes.RECT(), wintypes.DWORD()
                bounds = ([rect.left, rect.top, rect.right, rect.bottom]
                          if user.GetWindowRect(hwnd, ctypes.byref(rect)) else None)
                cloak_ok = dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0
                apps.append({"process_name": os.path.basename(path.value), "pid": pid.value, "title": title.value,
                             "hwnd": int(hwnd), "class_name": class_name.value, "foreground": hwnd == foreground,
                             "bounds": bounds, "visible": True, "minimized": bool(user.IsIconic(hwnd)),
                             "enabled": bool(user.IsWindowEnabled(hwnd)), "responsive": not bool(user.IsHungAppWindow(hwnd)),
                             "cloaked": bool(cloaked.value) if cloak_ok else None})
            except Exception:
                errors.append(True)
            finally:
                if handle:
                    kernel.CloseHandle(handle)
            return True

        complete = bool(user.EnumWindows(visit, 0)) and not errors
        return {"applications": apps, "complete": complete, "source": "windows_window_api"}
    except Exception:
        return {"applications": [], "complete": False}


# Aliases identify applications; they are never interpreted as commands/paths.
_APP_ALIASES = {
    'file_explorer': 'explorer', 'file explorer': 'explorer', 'windows_explorer': 'explorer',
    'windows explorer': 'explorer', 'explorer': 'explorer', 'explorer.exe': 'explorer',
    'file_manager': 'explorer', 'file manager': 'explorer', '资源管理器': 'explorer', '文件管理器': 'explorer',
    '文件资源管理器': 'explorer', 'windows_file_explorer': 'explorer',
    'chrome': 'chrome', 'google_chrome': 'chrome', 'google chrome': 'chrome',
    'edge': 'msedge', 'microsoft_edge': 'msedge', 'microsoft edge': 'msedge',
    'firefox': 'firefox', 'notepad': 'notepad',
}


def windows_application_process(app_code):
    code = str(app_code or '').strip().casefold()
    if code in _APP_ALIASES:
        return _APP_ALIASES[code]
    if re.fullmatch(r'[a-z0-9_-]+(?:\.exe)?', code):
        return code.removesuffix('.exe')
    return None


def _activate_window(hwnd):
    """Return dispatch and observed foreground separately; Windows can deny focus.

    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow
    """
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    for name in ('IsWindow', 'IsIconic', 'SetForegroundWindow'):
        fn = getattr(user, name)
        fn.argtypes, fn.restype = [wintypes.HWND], wintypes.BOOL
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.GetForegroundWindow.restype = wintypes.HWND
    if not user.IsWindow(hwnd):
        return False, False
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)  # SW_RESTORE; keep already-visible window sizing.
    user.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + .5
    while True:
        if user.GetForegroundWindow() == hwnd:
            return True, True
        if time.monotonic() >= deadline:
            return True, False
        time.sleep(.05)


def switch_windows_application(app_code):
    """Activate one matching existing window, or report why no switch occurred."""
    process = windows_application_process(app_code)
    result = {'action': 'switch_applications', 'app_code': app_code, 'channel': 'desktop', 'dispatched': False}
    if not process:
        return {**result, 'status': 'rejected', 'error_code': 'unknown_application',
                'reason': 'app_code 必须是应用标识或进程名，不能是命令；也可通过任务栏或系统启动器操作。'}
    runtime = windows_runtime_state()
    matches = [a for a in runtime.get('applications', [])
               if str(a.get('process_name', '')).casefold().removesuffix('.exe') == process and a.get('hwnd')]
    if process == 'explorer':
        # explorer.exe also owns the desktop and taskbar; those are not folders.
        matches = [a for a in matches if a.get('class_name') in {'CabinetWClass', 'ExploreWClass'}]
    current = next((a for a in matches if a.get('foreground')), None)
    if current:
        return {**result, 'status': 'observed', 'reason': '目标应用已在前台，请继续操作窗口内容，不要反复切换。',
                'window': current}
    if not matches:
        reason = ('未找到已打开的资源管理器窗口；请使用 hotkey keys=["win","e"] 打开后，根据新截图继续。'
                  if process == 'explorer' else '未找到目标应用窗口；请通过系统启动器打开，或根据桌面截图选择应用。')
        return {**result, 'status': 'rejected', 'error_code': 'application_not_found', 'reason': reason}
    if len(matches) != 1:
        return {**result, 'status': 'rejected', 'error_code': 'ambiguous_application',
                'reason': '有多个目标应用窗口，请通过任务栏或任务切换界面按标题选择，不会盲按 Alt+Tab。',
                'candidates': [a.get('title', '') for a in matches[:8]]}
    target = matches[0]
    try:
        dispatched, focused = _activate_window(target['hwnd'])
    except Exception as exc:
        return {**result, 'status': 'uncertain', 'dispatched': True, 'error_code': 'activation_error',
                'reason': f'窗口激活结果不确定（{type(exc).__name__}）；请检查桌面截图。'}
    if not focused:
        return {**result, 'status': 'uncertain' if dispatched else 'rejected', 'dispatched': dispatched,
                'error_code': 'foreground_not_confirmed',
                'window': target,
                'reason': '目标窗口未确认进入前台；请用任务栏或系统快捷键选择窗口，不能当成切换成功。'}
    return {**result, 'status': 'executed', 'dispatched': dispatched,
            'reason': '已确认目标应用窗口进入前台。', 'window': target}
