"""Windows 本机环境：pyautogui 控制桌面 + CDP 控制浏览器。

用于在 Windows 本机（非 VM）上跑 GUI agent 任务，重点支持 browser use：
- 观察：后台 CDP 页面截图与 AX；显式桌面观察使用 Win32/Pillow 全虚拟桌面截图；
- 执行：桌面 GUI 动作（click/type/hotkey 等）走 pyautogui（坐标定位）；
        浏览器 DOM 动作（带 target_ref / 语义兜底命中）走 CDP（DOM 定位，跳过视觉定位）；
- 焦点检测走 Windows GetForegroundWindow。

与 VMwareEnvironment 的对齐点：browser_ax 快照进 context、target_ref 路由 + 语义兜底、
execute_routed 走 CDP。差异点：CDP host 是 localhost，桌面使用物理像素坐标，无 AT-SPI。
"""
from __future__ import annotations

import re
import subprocess
import sys
import math
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

from ..env import Environment, Observation, StepResult
from ..actions import Action, get_action_spec, BASIC_ACTIONS
from .vmware_env import to_pyautogui


class WindowsEnvironment(Environment):
    cli_target = "local"

    """Windows 本机环境（GUI + browser use）。"""

    evaluation_available = False

    def __init__(self, browser_port: int = 9222, width: int = 1920, height: int = 1080,
                 downloads_dir: Optional[str] = None, workspace_dir: Optional[str] = None,
                 window_timeout_s: float = 15.0, browser_start_url: Optional[str] = None,
                 browser_session_mode: str = "isolated"):
        if not isinstance(browser_session_mode, str) or browser_session_mode not in {"isolated", "persistent"}:
            raise ValueError('browser_session_mode must be isolated or persistent')
        self.browser_session_mode = browser_session_mode
        if type(window_timeout_s) not in (int, float) or not math.isfinite(window_timeout_s) or window_timeout_s <= 0:
            raise ValueError('window_timeout_s must be finite and positive')
        self.window_timeout_s = window_timeout_s
        self._window_wait_emit = None
        self._window_wait_remaining = None
        self._window_wait_cancel = threading.Event()
        self._pending_window = None
        self._window_launch_inflight = False
        self._last_window_wait = None
        self.browser_port = browser_port
        self.width = width
        self.height = height
        self.os_name = "windows"
        self.browser_only = True  # 浏览器专用：用精简 system prompt + 强化 DOM 定位
        self.downloads_dir = downloads_dir
        self.workspace_dir = workspace_dir
        self.browser_start_url = browser_start_url
        if downloads_dir is not None and workspace_dir is not None:
            raise ValueError('workspace_dir 与 downloads_dir 不能同时指定')
        self._browser_session = None
        self._last_observation_info = {}
        self._observation_surface = "browser"
        self._episode_prepared = False
        self.initial_state = {}
        self._desktop_capture = {}

    def prepare_workspace(self, task_id=None):
        if self._episode_prepared:
            return dict(self.initial_state)
        self.initial_state = self._get_browser_session().prepare_workspace(task_id)
        self.initial_state['workspace_location'] = 'target_desktop'
        return dict(self.initial_state)

    def begin_episode(self, task_id=None, isolated=True):
        session = self._get_browser_session()
        self.initial_state = session.begin_episode(task_id=task_id, isolated=isolated,
            viewport={"width": self.width, "height": self.height}, start_url=self.browser_start_url)
        if (not self.initial_state.get("isolated") and not
                (self.browser_session_mode == "persistent"
                 and self.initial_state.get("isolation_scope") == "task_tabs"
                 and self.initial_state.get("session_mode") == "persistent")):
            raise RuntimeError("Browser episode isolation was not established")
        self.initial_state['workspace_location'] = 'target_desktop'
        session.initial_state = dict(self.initial_state)
        self._episode_prepared = True
        self._observation_surface = "browser"
        self._desktop_capture = {}
        self._last_observation_info = {}
        self._pending_window = None
        self._window_launch_inflight = False
        self._last_window_wait = None
        self._window_wait_cancel.clear()
        return dict(self.initial_state)

    @contextmanager
    def framework_wait_context(self, emit, remaining):
        old = self._window_wait_emit, self._window_wait_remaining
        self._window_wait_emit, self._window_wait_remaining = emit, remaining
        try:
            yield
        finally:
            self._window_wait_emit, self._window_wait_remaining = old

    def cancel_pending_wait(self):
        self._window_wait_cancel.set()

    def _wait_for_desktop_window(self, target, *, dispatched):
        from ..desktop_readiness import wait_for_window
        self._pending_window = target
        report = wait_for_window(target, timeout_s=self.window_timeout_s,
            emit=self._window_wait_emit, remaining=self._window_wait_remaining,
            cancelled=self._window_wait_cancel.is_set)
        self._last_window_wait = report
        if report['status'] == 'completed':
            self._pending_window = None
            self._window_launch_inflight = False
        return {'status': 'executed' if report['status'] == 'completed' else 'uncertain',
                'dispatched': dispatched, 'channel': 'desktop', 'framework_wait': report,
                'reason': report['detail'],
                **({'window': report['window']} if report.get('window') else {}),
                **({'error_code': 'desktop_window_' + report['status']} if report['status'] != 'completed' else {})}

    # ---- 观察 ----
    def _take_screenshot(self):
        """Desktop fallback; browser screenshots are collected atomically with AX."""
        from ..desktop_capture import capture_virtual_desktop
        self._desktop_capture = {}
        shot, self._desktop_capture = capture_virtual_desktop()
        return shot

    def _runtime_state(self):
        from ..desktop_state import windows_runtime_state
        return windows_runtime_state()

    def _focus_is_browser(self) -> bool:
        """本机浏览器模式：以「CDP 有可用页面」为准，不依赖前台焦点。

        用户可同时用电脑（切到别的窗口），智能体照常通过 CDP 虚拟控制浏览器。
        """
        from ..utils import list_cdp_pages
        try:
            pages = list_cdp_pages("localhost", self.browser_port)
            return any(p.get("url") and not p.get("url", "").startswith(
                ("devtools://", "chrome-extension://")) for p in pages)
        except Exception:  # noqa: BLE001
            return False

    def _get_browser_session(self):
        """懒加载 BrowserSession（保持快照，跨步复用）。连不上返回 None。

        require_focus=False：本机模式用 CDP 虚拟输入，不要求浏览器前台聚焦——
        这样智能体操作浏览器时，你可以照常用自己的鼠标键盘。
        """
        if self._browser_session is None:
            from ..browser_session import BrowserSession, BrowserOptions
            self._browser_session = BrowserSession(
                options=BrowserOptions(endpoint=f"http://localhost:{self.browser_port}",
                                       require_focus=False, session_mode=self.browser_session_mode),
                foreground_check=None,
                downloads_dir=self.downloads_dir,
                workspace_dir=self.workspace_dir,
            )
        return self._browser_session

    def _collect_browser_snapshot(self):
        """抓当前浏览器页面的结构化 AX/DOM 快照（browser_ax）。非浏览器场景返回 None。"""
        if not self._focus_is_browser():
            return None
        try:
            session = self._get_browser_session()
            if session is None:
                return None
            return session.collect()
        except Exception:  # noqa: BLE001
            return None

    def _build_observation(self) -> Observation:
        """Collect screenshot and AX on one page, with an explicit coordinate space.

        浏览器模式（后台独立浏览器）只取 CDP 截图，绝不回退到截取用户整屏；
        虚拟桌面截图仅用于显式 observe_desktop。这样即使用户把浏览器窗口挡在后面，
        截图仍是该浏览器页面的完整内容，不受遮挡/前台影响。
        """
        from ..model.grounding import screenshot_size
        from ..scene import Scene, browser_scene

        from ..desktop_readiness import foreground, window_identity
        desktop_before = self._runtime_state() if self._observation_surface == 'desktop' else None
        screenshot, snapshot, metadata = None, None, {}
        error = ""
        if self._observation_surface == "browser":
            # 页面仍在导航/渲染时截图会瞬时失败，重试几次再放弃。
            import time as _time
            for _ in range(4):
                try:
                    session = self._get_browser_session()
                    if session is not None:
                        screenshot, snapshot, metadata = session.collect_observation()
                        metadata = dict(metadata or {})
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                if screenshot is not None and metadata.get("screenshot_space") == "css_viewport":
                    break
                _time.sleep(0.25)
        else:
            try:
                session = self._get_browser_session()
                if session is not None and hasattr(session, "event_metadata"):
                    metadata = session.event_metadata()
            except Exception as exc:  # noqa: BLE001
                error = str(exc)

        if screenshot is not None and metadata.get("screenshot_space") == "css_viewport":
            info = metadata
            info["scene"] = browser_scene(
                snapshot, screenshot, page_id=metadata.get("page_id", ""),
                snapshot_id=metadata.get("snapshot_id", ""),
            ).to_dict()
            context = [snapshot] if snapshot else []
        elif self._observation_surface == "browser":
            # 浏览器模式：CDP 截图失败 → 返回错误观测（无截图），不截取用户桌面。
            info = {"screenshot_space": "css_viewport",
                    "scene": Scene(browser_use=1, structure_available=bool(snapshot),
                                   mode="browser_dom" if snapshot else "visual",
                                   reason="browser_observation_failed").to_dict(),
                    "observation_error": error or str(metadata.get("error") or "浏览器截图/快照采集失败")}
            for key in ("artifacts", "download_events", "pending_downloads", "dialogs", "dialog",
                        "initial_state", "tabs", "artifact_events"):
                if key in metadata:
                    info[key] = metadata[key]
            context = [snapshot] if snapshot else []
        else:
            # 桌面模式：显式 observe_desktop 才截取全部显示器。
            screenshot = self._take_screenshot()
            size = screenshot_size(screenshot)
            info = {"screenshot_space": "screen_pixels",
                    "screenshot_size": list(size) if size else None,
                    **self._desktop_capture,
                    "scene": Scene(browser_use=0, structure_available=False,
                                   mode="visual", reason="desktop_fallback").to_dict()}
            for key in ("artifacts", "download_events", "pending_downloads", "dialogs", "dialog",
                        "initial_state", "tabs", "artifact_events"):
                if key in metadata:
                    info[key] = metadata[key]
            if error or metadata.get("error") or metadata.get("observation_errors"):
                info["observation_error"] = error or metadata.get("error") or "; ".join(metadata["observation_errors"])
            context = []
        if self.initial_state:
            info.setdefault('initial_state', dict(self.initial_state))
        info["active_surface"] = "browser" if info.get("screenshot_space") == "css_viewport" else "desktop"
        info["requested_surface"] = self._observation_surface
        info['control_mode'] = 'background_browser' if info['active_surface'] == 'browser' else 'desktop_takeover'
        info["runtime_state"] = self._runtime_state()
        if desktop_before is not None:
            before, after = foreground(desktop_before), foreground(info['runtime_state'])
            info['desktop_focus_stable'] = bool(before and after and window_identity(before) == window_identity(after))
            if self._last_window_wait:
                info['desktop_window_wait'] = dict(self._last_window_wait)
        self._last_observation_info = dict(info)
        return Observation(screenshot=screenshot, text="", context=context, info=info)

    def _derive_scene(self, snapshot, screenshot=None):
        """Derive browser mode from whether page content is actionable in AX."""
        from ..scene import Scene, browser_scene
        if snapshot:
            return browser_scene(snapshot, screenshot).to_dict()
        if self._focus_is_browser():
            return Scene(browser_use=1, structure_available=False, mode="visual",
                         reason="no_structured_page").to_dict()
        return Scene(browser_use=0, structure_available=False, mode="visual",
                     reason="not_browser").to_dict()

    def _fresh_obs_after_action(self) -> Observation:
        """动作执行后重新观察：新截图 + 新 browser 快照。"""
        return self.observe()

    # ---- 路由 + 执行（browser_dom）----
    # 浏览器场景里全部走 CDP 的动作（虚拟鼠标键盘，不碰真实外设）
    _BROWSER_CDP_ACTIONS = ("goto", "download", "click", "double_click", "right_click",
                            "type", "select", "scroll", "press", "hotkey")

    def route_action(self, action, obs):
        """浏览器场景的动作 → browser_dom（CDP 虚拟输入）；结构解析失败 → None（视觉定位）。

        流程：DOM（element_id / target_ref / 语义匹配）→ 命中走 CDP；都没命中返回 None，
        交给 pipeline 用 grounding 模型定坐标，再由 step() 用 CDP 虚拟坐标点击（仍不动真实鼠标）。
        """
        if obs is None or action.action not in self._BROWSER_CDP_ACTIONS:
            return None
        # Route against the actual observation, not a fresh CDP page guess.
        from ..scene import observation_scene
        if not observation_scene(obs).get("browser_use"):
            return None
        session = self._get_browser_session()
        if session is None:
            return None
        # 地址栏输入 URL → 转 goto（地址栏是浏览器 chrome UI，不在 DOM）
        url = self._address_bar_url(action)
        if url:
            return {"channel": "browser_dom", "goto_url": url, "coordinate_space": "css_viewport"}
        # A scroll with an explicit node refers to that container, not the page.
        if action.action == "scroll" and (action.args.get("target_ref") or action.args.get("element_id")):
            return session.route(action, obs)
        # goto / scroll / press / hotkey：无 target，直接 browser_dom
        if action.action in ("goto", "download", "scroll", "press", "hotkey"):
            return {"channel": "browser_dom", "target": action.args.get("target"),
                    "coordinate_space": "css_viewport", **self._route_identity(obs)}
        # click / type / select / double / right：优先 DOM 定位
        try:
            route = session.route(action, obs)  # 处理 target_ref / element_id
        except Exception:  # noqa: BLE001
            route = None
        if route is not None:
            return route  # browser_dom 或 reobserve
        # A content-heavy AX snapshot may expose only navigation/filter controls.
        # Do not resolve a card description against those unrelated controls;
        # let screenshot grounding choose the card while explicit refs still work.
        selected_mode = ((obs.info or {}).get('decision_mode') or {}).get('mode')
        if ((selected_mode == 'gui' or (selected_mode is None and observation_scene(obs).get("mode") == "browser_gui"))
                and action.action in {"click", "double_click", "right_click"}):
            return None
        # 语义兜底：target 描述 → ref（仍在结构层）
        target = str(action.args.get("target") or "")
        if target:
            exposed = obs.info.get("exposed_refs") if isinstance(obs.info, dict) else None
            if exposed is not None and not exposed:
                return None
            allowed = set(exposed) if exposed else None
            try:
                ref = session.resolve_target(target, allowed=allowed)
            except Exception:  # noqa: BLE001
                ref = None
            if not ref and getattr(session, 'last_resolution_error', None) == 'ambiguous':
                return {'channel': 'reobserve', 'error_code': 'ambiguous',
                        'reason': '多个 AX 元素匹配此描述，请结合标题和链接选择当前 target_ref，不能通过视觉猜选。'}
            if ref:
                action.args["target_ref"] = ref
                return session.route(action, obs)
        # 结构解析失败：返回 None → 视觉定位（grounding 定坐标 → CDP 虚拟点击）
        return None

    @staticmethod
    def _route_identity(obs):
        return {key: obs.info[key] for key in ("page_id", "document_id", "snapshot_id")
                if key in obs.info}

    @staticmethod
    def _address_bar_url(action):
        """模型往地址栏 type 网址（常见误操作）→ 提取 URL 用于 goto；否则返回 None。

        地址栏是浏览器 chrome UI、不在页面 DOM 里，type/click 定位不到；识别后直接转 goto。
        """
        if action.action != "type":
            return None
        text = str(action.args.get("text", "")).strip()
        target = str(action.args.get("target", "")).lower()
        if not re.search(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", text):
            return None
        if any(k in target for k in ("address bar", "omnibox", "url bar", "地址栏", "导航栏", "网址栏")):
            return text
        return None

    def prevent_duplicate_download(self, action, observation, route=None):
        session = self._get_browser_session()
        if session is None:
            return None
        return session.prevent_duplicate_download(action, observation, route)

    def execute_routed(self, action, route):
        """执行已路由的 browser_dom 动作：goto / DOM(ref) / scroll/press/hotkey 全部走 CDP。"""
        if action.action in {"download", "rename_artifact", "run_in_terminal", "call_code_agent", "set_cell_values", "save"}:
            return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                "error_code": "non_basic_action", "action": action.action})
        session = self._get_browser_session()
        if session is None:
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False, "channel": "browser_dom",
                "error": "browser session unavailable"})
        if action.action == "goto" or route.get("goto_url"):
            result = session.goto(str(action.args.get("url") or route.get("goto_url")))
        elif route.get("target_ref"):
            result = session.execute(action, route)
        elif action.action in ("scroll", "press", "hotkey"):
            result = session.execute_viewport_action(
                action, metadata=action.args.get("observation_metadata") or self._last_observation_info)
        else:
            result = {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                      "error": "no routable target"}
        obs = self._fresh_obs_after_action()
        info = dict(result)
        info["artifacts"] = obs.info.get("artifacts", info.get("artifacts", []))
        return StepResult(observation=obs, info=info)

    # ---- Environment 接口 ----
    def reset(self, task: str) -> Observation:
        if not self._episode_prepared:
            self.begin_episode()
        self._episode_prepared = False
        return self.observe()

    def observe(self) -> Observation:
        return self._build_observation()

    def step(self, action: Action) -> StepResult:
        name = action.action
        if name not in BASIC_ACTIONS:
            return StepResult(self.observe(), info={"status": "rejected", "dispatched": False,
                "error_code": "non_basic_action", "action": name})
        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"
        if name in {"observe_desktop", "observe_browser"}:
            self._observation_surface = "desktop" if name == "observe_desktop" else "browser"
            obs = self.observe()
            return StepResult(observation=obs, info={"status": "executed", "dispatched": False,
                "action": name, "artifacts": obs.info.get("artifacts", [])})
        if name in {"handle_dialog", "list_tabs", "switch_tab", "close_tab"}:
            session = self._get_browser_session()
            kwargs = {k: v for k, v in action.args.items() if k in {
                "accept", "prompt_text", "dialog_id", "tab_id", "artifact_id", "filename", "url"}}
            result = getattr(session, name)(**kwargs)
            obs = self.observe()
            result["artifacts"] = obs.info.get("artifacts", [])
            return StepResult(observation=obs, info=result)

        coordinate_space = action.args.get("coordinate_space")
        coordinate_actions = ("click", "double_click", "right_click", "type", "select", "drag_and_drop")
        if action.args.get("grounding_failed"):
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False, "error": "grounding failed"})

        if coordinate_space == "css_viewport" and kind == "env" and name != "wait":
            session = self._get_browser_session()
            if session is None:
                result = {"status": "rejected", "dispatched": False,
                          "channel": "browser_cdp", "error": "browser session unavailable"}
            else:
                result = session.execute_viewport_action(
                    action, metadata=action.args.get("observation_metadata") or self._last_observation_info)
            obs = self.observe()
            result = dict(result)
            result["artifacts"] = obs.info.get("artifacts", [])
            return StepResult(observation=obs, info=result)

        if name in coordinate_actions and coordinate_space != "screen_pixels":
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False,
                "error": "coordinate action requires css_viewport or screen_pixels"})

        if kind == "browser":
            result = self.browser_action(name, action.args)
            return StepResult(observation=self._build_observation(), info={"browser_result": result})

        if kind == "shell":
            output = self.run_command(str(action.args.get("command", "")))
            return StepResult(observation=self._build_observation(), info={"terminal_result": output})

        # 浏览器表面使用 CDP；显式切到桌面并重新观察后才允许桌面输入。
        if self.browser_only and self._observation_surface != "desktop" and name != "wait":
            return StepResult(observation=self.observe(), info={
                "status": "rejected", "dispatched": False, "channel": "desktop",
                "error": "请先 observe_desktop 获取桌面截图后再执行 GUI 操作；网页操作使用 DOM 或 observe_browser"})
        if self._window_wait_cancel.is_set() or (self._window_wait_remaining and self._window_wait_remaining() <= 0):
            return StepResult(observation=self.observe(), info={'status': 'rejected', 'dispatched': False,
                'channel': 'desktop', 'error_code': 'desktop_operation_cancelled', 'reason': '操作已取消或任务时间预算已耗尽'})
        return self._step_desktop(action)

    def _step_desktop(self, action: Action) -> StepResult:
        name = action.action
        if name == 'switch_applications':
            from ..desktop_state import switch_windows_application, windows_application_process
            process = windows_application_process(action.args.get('app_code'))
            if sys.platform == 'win32' and process:
                self._pending_window = {'process_name': process + '.exe'}
                self._window_launch_inflight = False
            result = switch_windows_application(action.args.get('app_code'))
            if sys.platform == 'win32' and result.get('window') and result.get('status') in {'executed', 'observed', 'uncertain'}:
                result.update(self._wait_for_desktop_window(result['window'], dispatched=result['dispatched']))
                if result['framework_wait']['status'] == 'completed':
                    result.pop('error_code', None)
            return StepResult(observation=self.observe(), info=result)
        from ..desktop_readiness import launch_target, global_shortcut, check_keyboard_focus, DesktopFocusError
        from ..desktop_state import windows_runtime_state
        target = launch_target(action) if sys.platform == 'win32' else None
        if target and self._window_launch_inflight and self._pending_window and self._pending_window.get('process_name') == target['process_name']:
            # A previous launch may still be completing. Never create more windows.
            result = self._wait_for_desktop_window(target, dispatched=False)
            return StepResult(observation=self.observe(), info=result)
        keyboard_guard = (sys.platform == 'win32' and name in {'type', 'select', 'press', 'hotkey', 'goto'}
                          and not global_shortcut(action))
        observed = self._last_observation_info
        def guard_input():
            check_keyboard_focus(observed.get('runtime_state', {}), windows_runtime_state(),
                pending_target=self._pending_window, capture_stable=observed.get('desktop_focus_stable') is True)
        if keyboard_guard:
            try:
                guard_input()
            except DesktopFocusError as exc:
                return StepResult(observation=self.observe(), info={'status': 'rejected', 'dispatched': False,
                    'channel': 'desktop', 'error_code': 'desktop_focus_unconfirmed', 'reason': str(exc)})
        # 坐标化后执行 pyautogui 命令
        try:
            if sys.platform == 'win32' and not self._desktop_capture and name != 'wait':
                raise ValueError('缺少有效虚拟桌面截图，请重新 observe_desktop')
            if self._desktop_capture:
                from ..desktop_capture import validate_desktop_action, virtual_geometry
                mapped, origin = validate_desktop_action(action, self._desktop_capture, virtual_geometry())
                cmd = to_pyautogui(mapped, desktop_origin=origin, keyboard_guard=keyboard_guard)
            else:
                cmd = to_pyautogui(action, keyboard_guard=keyboard_guard)
        except (ValueError, OSError) as exc:
            return StepResult(observation=self.observe(), info={'status': 'rejected', 'dispatched': False,
                'channel': 'desktop', 'error_code': 'desktop_geometry_changed', 'error': str(exc)})
        if cmd:
            try:
                from ..desktop_capture import physical_dpi_context
                with physical_dpi_context():
                    exec(cmd, {"__name__": "__main__", "desktop_input_guard": guard_input})  # noqa: S102
            except DesktopFocusError as exc:
                return StepResult(observation=self.observe(), info={'status': 'uncertain', 'dispatched': True,
                    'channel': 'desktop', 'error_code': 'desktop_focus_changed_during_action', 'reason': str(exc)})
            except Exception as exc:  # noqa: BLE001
                return StepResult(observation=self._build_observation(),
                                  info={"status": "failed", "dispatched": True,
                                        "channel": "desktop", "error": f"执行异常: {exc}"})
            if target:
                self._window_launch_inflight = True
                result = self._wait_for_desktop_window(target, dispatched=True)
                return StepResult(observation=self.observe(), info=result)
            return StepResult(observation=self.observe(), info={
                "status": "executed", "dispatched": True, "channel": "desktop"})
        return StepResult(observation=self.observe(), info={
            "status": "rejected", "dispatched": False, "channel": "desktop",
            "error": "unsupported action or invalid coordinates"})

    # ---- 浏览器操作 ----
    def browser_action(self, action_name: str, args: Dict[str, Any]) -> str:
        """执行 browser kind 动作。"""
        if action_name == "get_page_source":
            return self.get_page_source(target_ref=args.get("target_ref"), max_chars=args.get("max_chars", 20000))
        if action_name == "run_browser_js":
            return self.evaluate_js(args.get("js", ""), max_chars=args.get("max_chars", 12000))
        return f"未知浏览器动作 {action_name}"

    def get_page_source(self, target_ref=None, max_chars=20000) -> str:
        """Read the same task page used by observations and actions."""
        session = self._get_browser_session()
        if session is None:
            return "浏览器会话不可用，请重新观察。"
        return session.get_page_source(target_ref=target_ref, max_chars=max_chars)

    def evaluate_js(self, js: str, max_chars=12000) -> str:
        """在任务浏览器里执行一段 JavaScript（run_browser_js 动作）。"""
        if not js or not js.strip():
            return "run_browser_js 需要提供 js 代码"
        session = self._get_browser_session()
        if session is None:
            return "浏览器会话不可用，请重新观察。"
        return session.evaluate_js(js, max_chars=max_chars)

    def run_command(self, command: str) -> str:
        stripped = (command or "").strip()
        # `start` 用于启动 GUI 程序/打开文件：分离进程、不捕获输出、立即返回。
        # 否则 subprocess.run(capture_output=True) 会等被启动的 GUI 进程关闭其
        # 继承的 stdout/stderr 管道才结束，导致 run_in_terminal 卡到 120s 超时。
        if re.match(r'^start\b', stripped, re.IGNORECASE):
            try:
                subprocess.Popen(["cmd", "/c", command],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                return "已启动（后台进程，不等待退出）：" + command[:120]
            except Exception as exc:  # noqa: BLE001
                return f"命令执行失败: {exc}"
        try:
            r = subprocess.run(["cmd", "/c", command], capture_output=True, text=True,
                               timeout=120, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"命令执行失败: {exc}"
        return (r.stdout or "").strip() or (r.stderr or "").strip() or "(无输出)"

    def run_python(self, code: str) -> str:
        try:
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               timeout=120, encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"python 执行失败: {exc}"
        return (r.stdout or "").strip() or (r.stderr or "").strip() or "(无输出)"

    def evaluate(self) -> float:
        # 本地手动任务没有独立评测器；框架通过任务契约和执行证据验证。
        return 0.0

    def close(self) -> None:
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser_session = None
