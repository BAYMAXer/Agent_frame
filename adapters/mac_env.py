"""macOS local environment: shared browser CDP path plus native desktop operations."""
from __future__ import annotations

import subprocess
import sys
import time

from ..actions import Action
from ..env import Observation, StepResult
from .windows_env import WindowsEnvironment


class MacEnvironment(WindowsEnvironment):
    """Reuse browser observation/routing; isolate macOS desktop input and evidence."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.os_name = 'mac'

    def _runtime_state(self):
        from ..mac_desktop import runtime_state
        return runtime_state()

    def _take_screenshot(self):
        from ..mac_desktop import capture_virtual_desktop
        shot, self._desktop_capture = capture_virtual_desktop()
        return shot

    def _step_desktop(self, action: Action) -> StepResult:
        from .. import mac_desktop
        name = action.action
        if name == 'switch_applications':
            try:
                result = mac_desktop.switch_application(action.args.get('app_code'))
                if result.get('status') == 'uncertain' and result.get('dispatched'):
                    deadline = time.monotonic() + min(self.window_timeout_s, 3.0)
                    while time.monotonic() < deadline:
                        time.sleep(.15)
                        state = mac_desktop.runtime_state()
                        front = next((w for w in state['applications'] if w.get('foreground')), None)
                        target = mac_desktop.application_process(action.args.get('app_code'))
                        if front and front.get('process_name') == target:
                            result.update(status='executed', window=front)
                            result.pop('error_code', None)
                            break
            except Exception as exc:
                result = {'status': 'failed', 'dispatched': False, 'channel': 'desktop',
                          'error_code': 'application_switch_failed', 'reason': type(exc).__name__}
            return StepResult(observation=self.observe(), info=result)

        if name == 'wait':
            time.sleep(float(action.args.get('seconds', 1)))
            return StepResult(observation=self.observe(), info={
                'status': 'executed', 'dispatched': True, 'channel': 'desktop'})

        if sys.platform != 'darwin':
            return StepResult(observation=self.observe(), info={
                'status': 'rejected', 'dispatched': False, 'channel': 'desktop',
                'error_code': 'wrong_host_platform'})
        captured = self._desktop_capture
        if not captured:
            return StepResult(observation=self.observe(), info={
                'status': 'rejected', 'dispatched': False, 'channel': 'desktop',
                'error_code': 'desktop_capture_missing', 'reason': '请先 observe_desktop 获取当前桌面截图'})

        observed = self._last_observation_info
        def guard():
            mac_desktop.check_keyboard_focus(observed.get('runtime_state', {}),
                mac_desktop.runtime_state(), capture_stable=observed.get('desktop_focus_stable') is True)
        dispatch_started = False
        try:
            guard()  # Window movement/focus can invalidate image-local coordinates too.
            mapped, origin = mac_desktop.validate_desktop_action(
                action, captured, mac_desktop.virtual_geometry())
            if not mac_desktop._accessibility_trusted():
                raise PermissionError('请在 macOS 系统设置中为运行 Python 的程序开启「辅助功能」权限')
            dispatch_started = True
            mac_desktop.execute_action(mapped, origin, keyboard_guard=guard)
        except mac_desktop.DesktopFocusError as exc:
            return StepResult(observation=self.observe(), info={
                'status': 'uncertain' if dispatch_started else 'rejected',
                'dispatched': dispatch_started, 'channel': 'desktop',
                'error_code': 'desktop_focus_unconfirmed', 'reason': str(exc)})
        except (PermissionError, ValueError, OSError) as exc:
            return StepResult(observation=self.observe(), info={
                'status': 'uncertain' if dispatch_started else 'rejected',
                'dispatched': dispatch_started, 'channel': 'desktop',
                'error_code': 'desktop_input_unavailable', 'reason': str(exc)})
        except Exception as exc:
            return StepResult(observation=self.observe(), info={
                'status': 'uncertain', 'dispatched': True, 'channel': 'desktop',
                'error_code': 'desktop_input_failed', 'reason': type(exc).__name__})
        return StepResult(observation=self.observe(), info={
            'status': 'executed', 'dispatched': True, 'channel': 'desktop'})

    def run_command(self, command: str) -> str:
        try:
            result = subprocess.run(['/bin/zsh', '-c', command], capture_output=True,
                                    text=True, timeout=120, errors='replace')
        except Exception as exc:
            return f'命令执行失败: {exc}'
        return (result.stdout or '').strip() or (result.stderr or '').strip() or '(无输出)'
