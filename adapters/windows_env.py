"""Windows 本机环境：pyautogui 控制桌面 + CDP 控制浏览器。

用于在 Windows 本机运行 GUI agent 任务，重点支持 browser use：
- 观察：pyautogui 截图 + browser_ax 快照（焦点在浏览器时经 BrowserSession 抓取）；
- 执行：桌面 GUI 动作（click/type/hotkey 等）走 pyautogui（坐标定位）；
        浏览器 DOM 动作（带 target_ref / 语义兜底命中）走 CDP（DOM 定位，跳过视觉定位）；
- 浏览器焦点同时核对 Windows 前台窗口 PID 与本任务的 CDP 调试端口。

browser_first 使用运行专属 CDP 页面；桌面兼容模式使用 Windows 截图和原生输入。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional

from ..env import Environment, Observation, StepResult
from ..actions import ACTION_REGISTRY, Action, get_action_spec
from ..browser_session import BrowserActionError
from .windows_input import activate_application, foreground_window, write_text, _enumerate_windows, to_pyautogui
from .windows_window import WindowPresenter
from .windows_display import (WorkScreen, detect_primary_work_screen,
                              initialize_dpi_awareness, move_window_to_work_screen)
from ..model.request_deadline import ModelRequestTimeout


class WindowsEnvironment(Environment):
    """Windows 本机环境（GUI + browser use）。"""

    local_environment = True
    supported_action_names = frozenset(ACTION_REGISTRY)

    def __init__(self, browser_port: int = 9222, width: int = 1920, height: int = 1080,
                 downloads_dir: Optional[str] = None,
                 browser_launcher: Optional[Callable[[], Any]] = None,
                 action_settle_seconds: float = 0.3,
                 work_screen: Optional[WorkScreen] = None,
                 presentation_state_path: Optional[str] = None,
                 execution_mode: str = "desktop", screenshot_max_edge: int = 1280):
        if execution_mode not in ("desktop", "browser_first"):
            raise ValueError("execution_mode 必须是 desktop 或 browser_first")
        self.execution_mode = execution_mode
        self.screenshot_max_edge = max(0, int(screenshot_max_edge))
        self._browser_subtask_active = False
        self._desktop_subtask_active = False
        self._browser_observation_info = {}
        self.browser_port = browser_port
        self.width = width
        self.height = height
        self.os_name = "windows"
        self.browser_only = False  # 同时支持桌面任务；browser use 仅限已核验的无痕实例
        self.downloads_dir = downloads_dir
        self._browser_launcher = browser_launcher
        self._browser_ready = False
        self._browser_session = None
        self._window_presenter = WindowPresenter(recovery_path=presentation_state_path)
        self._foreground_before_input = None
        self._screenshot_is_viewport = False
        self.action_settle_seconds = max(0.0, float(action_settle_seconds))
        self.work_screen = work_screen
        self._fallback_size = (width, height)
        self.dpi_awareness = None

    # ---- 观察 ----
    def _take_screenshot(self):
        """聚焦网页用 CDP 截 viewport；桌面和浏览器 chrome UI 用全屏截图。"""
        self._screenshot_is_viewport = False
        if self.execution_mode == "browser_first" and self._desktop_subtask_active:
            return self._take_desktop_screenshot()
        if self._focus_is_browser():
            session = self._get_browser_session()
            if session is not None:
                shot = session.screenshot()
                if shot is not None and self._focus_is_browser():
                    self._screenshot_is_viewport = True
                    return shot
        return self._take_desktop_screenshot()

    def _take_desktop_screenshot(self):
        if self.work_screen and self.work_screen.detected and self.work_screen.monitor_count > 1:
            from PIL import ImageGrab
            screen = self.work_screen
            # bbox 使用虚拟桌面坐标；all_screens 支持左侧/上方副屏的负坐标布局。
            return ImageGrab.grab(bbox=(screen.left, screen.top,
                                        screen.left + screen.width, screen.top + screen.height),
                                  all_screens=True)
        import pyautogui
        return pyautogui.screenshot()

    def _screen_info(self):
        return self.work_screen.to_dict() if self.work_screen else None

    def _screenshot_origin(self):
        if self._screenshot_is_viewport or not self.work_screen:
            return [0, 0]
        return [self.work_screen.left, self.work_screen.top]

    def _position_browser_on_work_screen(self):
        """明确导航时才移动已激活的浏览器窗口，观察阶段不改变用户窗口。"""
        screen = self.work_screen
        if not screen or not screen.detected or screen.monitor_count <= 1:
            return True
        if not self._focus_is_browser():
            return False
        try:
            hwnd = foreground_window()
            if not hwnd or not self._focus_is_browser() or foreground_window() != hwnd:
                return False
            if not move_window_to_work_screen(hwnd, screen):
                return False
            self._invalidate_browser_snapshot()
            return foreground_window() == hwnd and self._focus_is_browser()
        except OSError:
            return False

    def _present_browser_window(self):
        """只置顶已核验端口 PID 所属的真实 Chrome HWND，不使用窗口标题猜测。"""
        from ..utils import _tcp_listener_pids
        try:
            if not self._browser_ready:
                raise RuntimeError("无痕浏览器尚未核验")
            owners = _tcp_listener_pids(self.browser_port)
            if not owners or len(owners) != 1:
                raise RuntimeError("无法确认调试端口的唯一浏览器 PID")
            pid = next(iter(owners))
            windows = [window for window in _enumerate_windows(include_untitled=True)
                       if window.get("pid") == pid and window.get("executable") == "chrome.exe"]
            focused = foreground_window()
            window = next((candidate for candidate in windows if candidate["hwnd"] == focused), None)
            if window is None:
                window = windows[0] if len(windows) == 1 else None
            if window is None:
                raise RuntimeError("无痕实例的窗口不存在或有多个候选，无法确定需要置顶的窗口")
            return self._window_presenter.activate(
                window["hwnd"], pid,
                validate=lambda: self._browser_ready and _tcp_listener_pids(self.browser_port) == {pid})
        except Exception as exc:  # noqa: BLE001
            return {"status": "rejected", "dispatched": False,
                    "error": f"无痕浏览器窗口置顶失败: {exc}"}

    def _present_new_foreground_window(self, previous):
        """GUI 打开或切换到新的普通应用时置顶；观察和个人浏览器不触发此路径。"""
        try:
            hwnd = foreground_window()
            if not previous or not hwnd or hwnd == previous:
                return None
            candidates = [window for window in _enumerate_windows(include_untitled=True)
                          if window["hwnd"] == hwnd]
            if len(candidates) != 1:
                return None
            window = candidates[0]
            executable = (window.get("executable") or "").casefold()
            if not window.get("pid") or not executable or executable in {
                    "chrome.exe", "chromium.exe", "msedge.exe", "firefox.exe", "brave.exe",
                    "opera.exe", "vivaldi.exe", "safari.exe", "iexplore.exe",
                    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "searchhost.exe",
                    "searchapp.exe", "dwm.exe", "sihost.exe"}:
                return None
            # 无标题的 shell 窗口通常是任务栏/桌面，而不是已选择的文件管理器。
            title = (window.get("title") or "").strip().casefold()
            if ((executable == "explorer.exe" and title in {"", "program manager", "workerw"})
                    or (executable == "applicationframehost.exe" and not title)):
                return None
            return self._window_presenter.activate(
                hwnd, window["pid"], validate=lambda: foreground_window() == hwnd)
        except Exception as exc:  # noqa: BLE001
            return {"status": "rejected", "dispatched": False,
                    "error": f"新应用窗口置顶失败: {exc}"}

    def _desktop_action(self, action):
        """截图内坐标转换为工作屏幕坐标；浏览器 viewport 不经过此路径。"""
        screen = self.work_screen
        if not screen or not screen.detected:
            return action
        args = dict(action.args)
        for x_key, y_key in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
            x, y = args.get(x_key), args.get(y_key)
            if x is None or y is None:
                continue
            x, y = int(x), int(y)
            if not (0 <= x < screen.width and 0 <= y < screen.height):
                raise ValueError("目标坐标不在当前工作屏幕内")
            args[x_key], args[y_key] = x + screen.left, y + screen.top
        return Action(action.action, args)

    def _focus_is_browser(self) -> bool:
        """仅已核验的无痕端口所属窗口在前台时进入 browser use。"""
        if self.execution_mode == "browser_first" and self._browser_subtask_active:
            return bool(self._browser_ready and self._browser_session is not None
                        and self._browser_session.page_id)
        from ..utils import foreground_browser_matches_port
        try:
            return self._browser_ready and foreground_browser_matches_port(self.browser_port)
        except Exception:  # noqa: BLE001
            return False

    def _unmanaged_browser_has_focus(self) -> bool:
        """个人浏览器不作为桌面输入兜底目标。"""
        from ..utils import detect_foreground_browser
        return detect_foreground_browser("windows") and not self._focus_is_browser()

    def _ensure_browser_ready(self):
        """明确导航时始终启动/复用并核验目标，不依赖任意浏览器焦点。"""
        from ..utils import cdp_pages_are_incognito
        try:
            session = self._get_browser_session() if self.execution_mode == "browser_first" else None
            if session is not None:
                session._check_deadline()
            if self._browser_launcher is not None:
                self._browser_launcher()
            else:
                from ..run_windows_local import CHROME_DEFAULT, ensure_chrome
                ensure_chrome(self.browser_port, CHROME_DEFAULT)
            if session is not None:
                session._check_deadline()
                playwright, browser = session._connect()  # 同一连接核验无痕，遵守共享 deadline。
                session._disconnect(playwright, browser)
                private = True
            else:
                private = cdp_pages_are_incognito("localhost", self.browser_port)
            if not private:
                raise RuntimeError(f"调试端口 {self.browser_port} 无法确认是无痕浏览器；"
                                   "请关闭该调试浏览器或更换 --browser-port")
            self._browser_ready = True
        except BaseException:
            self._revoke_browser_ready()
            raise

    def _revoke_browser_ready(self):
        self._browser_ready = False
        self._invalidate_browser_snapshot()
        self._window_presenter.restore()

    def enter_browser_subtask(self, step=None):
        """按计划进入浏览器任务；不读取桌面截图，不自动访问模型尚未选定的网址。"""
        if self.execution_mode == "desktop":
            return self._build_observation()
        self._browser_subtask_active = True
        self._desktop_subtask_active = False
        try:
            self._ensure_browser_ready()
            session = self._get_browser_session()
            session.bind_owned_page()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._invalidate_browser_snapshot()
            return Observation(info={"scene": {"browser_use": 1, "structure_available": False,
                "mode": "browser_dom", "reason": "browser_start_failed"},
                "capture_error": str(exc)[:300], "browser_start_failed": True})
        return self._build_observation()

    def enter_desktop_subtask(self, step=None):
        self._browser_subtask_active = False
        self._desktop_subtask_active = True
        self._screenshot_is_viewport = False
        self._invalidate_browser_snapshot()
        if self.work_screen is None:
            self.dpi_awareness = initialize_dpi_awareness()
            self.work_screen = detect_primary_work_screen(*self._fallback_size)
            self.width, self.height = self.work_screen.width, self.work_screen.height
        return self._build_observation()

    def _get_browser_session(self):
        """懒加载 BrowserSession；网页和前台窗口都必须聚焦才能进行 CDP 操作。"""
        if self._browser_session is None:
            from ..browser_session import BrowserSession, BrowserOptions
            self._browser_session = BrowserSession(
                options=BrowserOptions(endpoint=f"http://localhost:{self.browser_port}",
                                       require_focus=self.execution_mode != "browser_first",
                                       require_incognito=True,
                                       bind_page=self.execution_mode == "browser_first",
                                       screenshot_max_edge=self.screenshot_max_edge),
                foreground_check=self._focus_is_browser if self.execution_mode != "browser_first" else None,
                downloads_dir=self.downloads_dir,
                incognito_failure_callback=self._revoke_browser_ready,
            )
        self._browser_session.request_deadline = (getattr(self, "request_deadline", None)
            if self.execution_mode == "browser_first" else None)
        return self._browser_session

    def _invalidate_browser_snapshot(self):
        if self._browser_session is not None:
            self._browser_session.invalidate()

    def _collect_browser_snapshot(self):
        """抓当前浏览器页面的结构化 AX/DOM 快照（browser_ax）。非浏览器场景返回 None。"""
        if not self._focus_is_browser():
            self._invalidate_browser_snapshot()
            return None
        try:
            session = self._get_browser_session()
            if session is None:
                return None
            snapshot = session.collect()
            if snapshot is not None and self._focus_is_browser():
                return snapshot
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_browser_snapshot()
        return None

    def _build_observation(self) -> Observation:
        """截图 + browser_ax 快照进 context；显式标注场景（browser_use / structure_available）。"""
        if self.execution_mode == "browser_first" and not self._desktop_subtask_active:
            if not self._browser_subtask_active or not self._browser_ready:
                return Observation(info={"scene": {"browser_use": 0, "structure_available": False,
                    "mode": "planning", "reason": "awaiting_browser_subtask"}})
            session = self._get_browser_session()
            captured = session.capture_observation()
            snapshot = captured.get("snapshot")
            screenshot = captured.get("screenshot")
            self._screenshot_is_viewport = screenshot is not None
            info = {"scene": self._derive_scene(snapshot), "screenshot_origin": [0, 0],
                    **captured.get("info", {})}
            self._browser_observation_info = dict(info)
            return Observation(screenshot=screenshot, context=[snapshot] if snapshot else [], info=info)
        screenshot = self._take_screenshot()
        # 全屏截图包含地址栏、标签栏等 chrome UI，不能复用上一步的页面结构或 viewport 坐标。
        snapshot = self._collect_browser_snapshot() if self._screenshot_is_viewport else None
        if snapshot is None:
            self._invalidate_browser_snapshot()
            if self._screenshot_is_viewport:
                # 快照阶段可能已切到 chrome UI 或别的应用，再确认截图对应的坐标系。
                screenshot = self._take_screenshot()
        scene = self._derive_scene(snapshot)
        if not scene["browser_use"]:
            snapshot = None
            if self._screenshot_is_viewport:
                screenshot = self._take_desktop_screenshot()
                self._screenshot_is_viewport = False
        context = [snapshot] if snapshot and scene["structure_available"] else []
        info = {"scene": scene,
                "screenshot_coordinate_space": "css_viewport" if self._screenshot_is_viewport else "screen",
                "screenshot_origin": self._screenshot_origin(), "work_screen": self._screen_info(),
                "dpi_awareness": self.dpi_awareness}
        return Observation(screenshot=screenshot, text="", context=context, info=info)

    def _derive_scene(self, snapshot):
        """算显式场景：有快照→browser_dom；浏览器但无结构→visual；非浏览器→visual/desktop。"""
        from ..scene import Scene
        if not self._focus_is_browser():
            self._invalidate_browser_snapshot()
            return Scene(browser_use=0, structure_available=False, mode="visual",
                         reason="not_browser").to_dict()
        if snapshot:
            return Scene(browser_use=1, structure_available=True, mode="browser_dom",
                         reason="structured_page", url=snapshot.get("url", ""),
                         page_id=snapshot.get("page_id", ""),
                         snapshot_id=snapshot.get("snapshot_id", "")).to_dict()
        return Scene(browser_use=1, structure_available=False, mode="visual",
                     reason="no_structured_page").to_dict()

    def _fresh_obs_after_action(self) -> Observation:
        """动作执行后重新观察：新截图 + 新 browser 快照。"""
        return self._build_observation()

    # ---- 路由 + 执行（browser_dom）----
    # 浏览器场景里全部走 CDP 的动作（虚拟鼠标键盘，不碰真实外设）
    _BROWSER_CDP_ACTIONS = ("goto", "click", "double_click", "right_click",
                            "type", "select", "scroll", "press", "hotkey")

    def route_action(self, action, obs):
        """浏览器场景的动作 → browser_dom（CDP 虚拟输入）；结构解析失败 → None（视觉定位）。

        流程：DOM（element_id / target_ref / 语义匹配）→ 命中走 CDP；都没命中返回 None，
        交给 pipeline 用 grounding 模型定坐标，再由 step() 用 CDP 虚拟坐标点击（仍不动真实鼠标）。
        """
        if self.execution_mode == "browser_first" and self._browser_subtask_active:
            try:
                self._get_browser_session().validate_action_snapshot(action)
            except ModelRequestTimeout:
                raise
            except Exception as exc:
                return {"channel": "reobserve", "refresh_observation": True, "reason": str(exc)[:300]}
        url = self._address_bar_url(action)
        if action.action == "goto" or url:
            return {"channel": "browser_dom", "goto_url": url or action.args.get("url"),
                    "coordinate_space": "css_viewport"}
        if obs is None:
            return None
        info = obs.info if isinstance(obs.info, dict) else {}
        scene = info.get("scene") or {}
        if action.action not in self._BROWSER_CDP_ACTIONS + ("drag_and_drop",):
            return None
        # 只有当前聚焦的浏览器网页才走 CDP；chrome UI 仍需要桌面视觉定位。
        if not self._focus_is_browser():
            self._invalidate_browser_snapshot()
            if scene.get("browser_use") == 1:
                return {"channel": "reobserve", "reason": "浏览器已失去焦点，需重新观察当前应用",
                        "refresh_observation": True}
            if self._unmanaged_browser_has_focus():
                return {"channel": "reobserve", "refresh_observation": True,
                        "reason": "当前前台浏览器不是本任务的无痕实例；请使用 goto 启动或激活目标浏览器"}
            return None
        if (action.action not in self._BROWSER_CDP_ACTIONS
                or scene.get("browser_use") != 1
                or not (scene.get("structure_available")
                        or info.get("screenshot_coordinate_space") == "css_viewport")):
            return None
        session = self._get_browser_session()
        if session is None:
            return None
        if self.execution_mode == "browser_first":
            try:
                session.validate_observation_snapshot(obs)
            except BrowserActionError as exc:
                return {"channel": "reobserve", "refresh_observation": True, "reason": str(exc)}
        # 地址栏输入 URL → 转 goto（地址栏是浏览器 chrome UI，不在 DOM）
        url = self._address_bar_url(action)
        if url:
            return {"channel": "browser_dom", "goto_url": url, "coordinate_space": "css_viewport"}
        # goto / scroll / press / hotkey：无 target，直接 browser_dom
        if action.action in ("goto", "scroll", "press", "hotkey"):
            if action.action == "scroll" and action.args.get("target_ref"):
                route = session.route(action, obs)
                if route is not None:
                    return route
            return {"channel": "browser_dom", "target": action.args.get("target"),
                    **(session.snapshot_route() if self.execution_mode == "browser_first" else {}),
                    "coordinate_space": "css_viewport"}
        # click / type / select / double / right：优先 DOM 定位
        try:
            route = session.route(action, obs)  # 处理 target_ref / element_id
        except ModelRequestTimeout:
            raise
        except Exception:  # noqa: BLE001
            route = None
        if route is not None:
            return route  # browser_dom 或 reobserve
        # 语义兜底：target 描述 → ref（仍在结构层）
        target = str(action.args.get("target") or "")
        if target:
            exposed = obs.info.get("exposed_refs") if isinstance(obs.info, dict) else None
            allowed = set(exposed) if exposed is not None else (set() if self.execution_mode == "browser_first" else None)
            try:
                ref = session.resolve_target(target, allowed=allowed)
            except Exception:  # noqa: BLE001
                ref = None
            if ref:
                action.args["target_ref"] = ref
                return {"channel": "browser_dom", **session.snapshot_route(),
                        "exposed_refs": list(allowed or []), "target_ref": ref, "coordinate_space": "css_viewport"}
        # 结构解析失败：返回 None → 视觉定位（grounding 定坐标 → CDP 虚拟点击）
        return None

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

    def execute_routed(self, action, route):
        """执行已路由的 browser_dom 动作：goto / DOM(ref) / scroll/press/hotkey 全部走 CDP。"""
        if self.execution_mode == "browser_first" and self._browser_subtask_active:
            try:
                self._get_browser_session().validate_action_snapshot(action)
            except BrowserActionError as exc:
                return StepResult(observation=self._build_observation(), info={"channel": "browser_dom",
                    "status": "rejected", "dispatched": False, "error": str(exc)})
        if action.action == "goto" or route.get("goto_url"):
            return self._navigate(str(action.args.get("url") or route.get("goto_url") or ""))
        if not self._focus_is_browser():
            return self._browser_focus_rejected()
        session = self._get_browser_session()
        if session is None:
            return StepResult(observation=self._fresh_obs_after_action(),
                              info={"status": "rejected", "dispatched": False,
                                    "channel": "browser_dom", "error": "Browser session unavailable"})
        if not self._focus_is_browser():
            return self._browser_focus_rejected()
        if route.get("target_ref"):
            result = session.execute(action, route)
        elif action.action in ("scroll", "press", "hotkey"):
            result = (session.execute_target(action, None, route=route)
                      if self.execution_mode == "browser_first" else session.execute_target(action, None))
        else:
            result = {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                      "error": "no routable target"}
        obs = self._fresh_obs_after_action()
        info = dict(result)
        return StepResult(observation=obs, info=info)

    def _browser_focus_rejected(self):
        self._invalidate_browser_snapshot()
        return StepResult(observation=self._fresh_obs_after_action(),
                          info={"status": "rejected", "dispatched": False,
                                "channel": "browser_dom", "error": "Browser lost focus"})

    # ---- Environment 接口 ----
    def reset(self, task: str) -> Observation:
        if self.execution_mode == "browser_first":
            self._browser_subtask_active = False
            self._desktop_subtask_active = False
            self._screenshot_is_viewport = False
            self._browser_observation_info = {}
            if self._browser_session is not None:
                self._browser_session.close()
            return Observation(info={"scene": {"browser_use": 0, "structure_available": False,
                "mode": "planning", "reason": "awaiting_browser_subtask"}})
        self._window_presenter.restore()
        self.dpi_awareness = initialize_dpi_awareness()
        self.work_screen = detect_primary_work_screen(*self._fallback_size)
        self.width, self.height = self.work_screen.width, self.work_screen.height
        self._invalidate_browser_snapshot()
        self._screenshot_is_viewport = False
        screen = self.work_screen
        if screen.detected:
            print(f"[screen] 检测到 {screen.monitor_count} 块屏幕；默认工作屏幕：主屏 "
                  f"{screen.device or '(primary)'} {screen.width}x{screen.height} "
                  f"origin=({screen.left},{screen.top})", flush=True)
        else:
            print(f"[screen] 显示布局检测不可用，沿用默认主屏截图；后备尺寸 "
                  f"{screen.width}x{screen.height}", flush=True)
        return self._build_observation()

    def _navigate(self, url: str) -> StepResult:
        """所有导航统一核验无痕实例；CDP 失败不向个人窗口发送原生输入。"""
        url = (url or "").strip()
        if not url:
            return StepResult(observation=self._build_observation(),
                              info={"status": "rejected", "dispatched": False,
                                    "action": "goto", "error": "empty url"})
        if self.execution_mode == "browser_first":
            try:
                if not self._browser_subtask_active or not self._browser_ready:
                    self.enter_browser_subtask()
                session = self._get_browser_session()
                if not self._browser_ready or not session.page_id:
                    raise RuntimeError("本任务浏览器尚未就绪")
                result = session.goto(url)
            except ModelRequestTimeout:
                raise
            except Exception as exc:
                result = {"status": "rejected", "dispatched": False, "error": str(exc)[:300]}
            return StepResult(observation=self._build_observation(),
                              info={"channel": "browser_dom", "action": "goto", **result})
        try:
            self._ensure_browser_ready()
            session = self._get_browser_session()
            if session is None:
                raise RuntimeError("Browser session unavailable")
            presentation = self._present_browser_window()
            if presentation.get("status") != "executed":
                return StepResult(observation=self._build_observation(),
                                  info={"status": "rejected", "dispatched": False,
                                        "channel": "browser_dom", "action": "goto",
                                        "window_result": presentation,
                                        "error": presentation.get("error") or "无痕窗口未能置顶并获得焦点"})
            if not session.activate():
                return self._browser_focus_rejected()
        except Exception as exc:  # noqa: BLE001
            return StepResult(observation=self._build_observation(),
                              info={"status": "rejected", "dispatched": False,
                                    "channel": "browser_dom", "action": "goto",
                                    "error": f"无痕浏览器准备失败: {exc}"})
        if not self._focus_is_browser():
            return self._browser_focus_rejected()
        if not self._position_browser_on_work_screen():
            return StepResult(observation=self._build_observation(),
                              info={"status": "rejected", "dispatched": False, "action": "goto",
                                    "error": "无法将目标浏览器窗口放到主屏工作区，需重新观察"})
        if not self._focus_is_browser():
            return self._browser_focus_rejected()
        try:
            result = session.goto(url)
        except ModelRequestTimeout:
            raise
        except Exception as exc:  # noqa: BLE001
            # 意外异常时不确认是否已经发送导航，禁止原生输入重试。
            result = {"status": "uncertain", "dispatched": True,
                      "error": f"CDP 导航异常: {exc}"}
        return StepResult(observation=self._build_observation(),
                          info={"channel": "browser_dom", "action": "goto", **dict(result)})

    def step(self, action: Action) -> StepResult:
        name = action.action
        if self.execution_mode == "browser_first" and self._browser_subtask_active:
            try:
                self._get_browser_session().validate_action_snapshot(action)
            except BrowserActionError as exc:
                return StepResult(observation=self._build_observation(), info={"channel": "browser_dom",
                    "status": "rejected", "dispatched": False, "error": str(exc)})
        if name not in self.supported_action_names:
            return self._desktop_step_result(action, {
                "status": "rejected", "dispatched": False,
                "error": f"Windows 本地环境不支持动作: {name}",
            })
        if name == "goto":
            return self._navigate(str(action.args.get("url") or ""))
        url = self._address_bar_url(action)
        if url:
            return self._navigate(url)
        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"

        if self.execution_mode == "browser_first" and not self._desktop_subtask_active:
            return self._browser_step(action, kind)

        if (self._screenshot_is_viewport
                and name in ("click", "double_click", "right_click", "type", "press",
                             "hotkey", "scroll", "select", "drag_and_drop")):
            if not self._focus_is_browser():
                return self._browser_focus_rejected()
            if name in ("select", "drag_and_drop"):
                # 这些动作使用原生 GUI，必须先按完整桌面截图重新定位，不能把 viewport 坐标当屏幕坐标。
                self._invalidate_browser_snapshot()
                self._screenshot_is_viewport = False
                screenshot = self._take_desktop_screenshot()
                scene = self._derive_scene(None)
                scene["reason"] = "screen_coordinates_required"
                observation = Observation(screenshot=screenshot,
                                          info={"scene": scene, "screenshot_coordinate_space": "screen",
                                                "screenshot_origin": self._screenshot_origin(),
                                                "work_screen": self._screen_info(),
                                                "dpi_awareness": self.dpi_awareness})
                return StepResult(observation=observation,
                                  info={"status": "rejected", "dispatched": False, "channel": "gui",
                                        "error": "该动作需要全屏坐标，已切换到全屏观察，请重新定位后再执行"})

        # 浏览器场景 + 视觉定位坐标 → CDP 虚拟点击（不动真实鼠标）
        if (kind == "env"
                and name in ("click", "double_click", "right_click", "type")
                and action.args.get("x") is not None and action.args.get("y") is not None
                and not action.args.get("grounding_failed")
                and self._screenshot_is_viewport):
            if not self._focus_is_browser():
                return self._browser_focus_rejected()
            session = self._get_browser_session()
            if session is None:
                return StepResult(observation=self._fresh_obs_after_action(),
                                  info={"status": "rejected", "dispatched": False,
                                        "channel": "browser_dom", "error": "Browser session unavailable"})
            if not self._focus_is_browser():
                return self._browser_focus_rejected()
            # 截图是 viewport（CDP 截的），grounding 坐标即 viewport 坐标，直接点，不再屏幕→viewport 换算
            result = session.click_at_viewport(action, int(action.args["x"]), int(action.args["y"]))
            return StepResult(observation=self._build_observation(), info=dict(result))

        if kind == "browser":
            result = self.browser_action(name, action.args)
            return StepResult(observation=self._build_observation(), info={"browser_result": result})

        if kind == "shell":
            output = self.run_command(str(action.args.get("command", "")))
            return StepResult(observation=self._build_observation(), info={"terminal_result": output})

        # 桌面动作必须明确反馈拒绝、已发送或可能部分执行，不能把空命令当成成功。
        if action.args.get("grounding_failed"):
            return self._desktop_step_result(action, {
                "status": "rejected", "dispatched": False,
                "error": action.args.get("grounding_error") or "视觉定位失败，动作未执行",
            })
        if (name in ("click", "double_click", "right_click", "type", "select", "press",
                     "hotkey", "scroll", "drag_and_drop") and self._unmanaged_browser_has_focus()):
            return self._desktop_step_result(action, {
                "status": "rejected", "dispatched": False,
                "error": "当前前台浏览器不是本任务的无痕实例，未发送输入；请先使用 goto 激活目标浏览器",
            })
        dispatched = False
        try:
            if name == "switch_applications":
                app_code = str(action.args.get("app_code") or "")
                if self.work_screen and self.work_screen.detected and self.work_screen.monitor_count > 1:
                    info = activate_application(app_code, work_screen=self.work_screen,
                                                allow_browsers=False, presenter=self._window_presenter)
                else:
                    info = activate_application(app_code, allow_browsers=False,
                                                presenter=self._window_presenter)
                return self._desktop_step_result(action, info)
            self._foreground_before_input = foreground_window()
            action = self._desktop_action(action)
            if (name == "scroll" and self.work_screen and self.work_screen.detected
                    and self.work_screen.monitor_count > 1):
                import pyautogui
                x, y = pyautogui.position()
                screen = self.work_screen
                if not (screen.left <= x < screen.left + screen.width
                        and screen.top <= y < screen.top + screen.height):
                    # 滚轮发送到鼠标所在窗口，不能沿用上一任务留在副屏的光标。
                    wl, wt, wr, wb = screen.work_area
                    dispatched = True
                    pyautogui.moveTo((wl + wr) // 2, (wt + wb) // 2)
            if name in ("type", "select"):
                x, y = action.args.get("x"), action.args.get("y")
                if x is None or y is None or int(x) < 0 or int(y) < 0:
                    return self._desktop_step_result(action, {
                        "status": "rejected", "dispatched": False, "error": "输入动作缺少有效目标坐标",
                    })
                import pyautogui
                dispatched = True
                pyautogui.click(int(x), int(y))
                if self._unmanaged_browser_has_focus():
                    return self._desktop_step_result(action, {
                        "status": "uncertain", "dispatched": True,
                        "error": "点击后焦点转到非本任务浏览器，未发送后续输入；请使用 goto 激活无痕实例",
                    })
                if name == "type":
                    if action.args.get("overwrite"):
                        pyautogui.hotkey("ctrl", "a")
                        pyautogui.press("backspace")
                    write_text(str(action.args.get("text", "")), gui=pyautogui)
                else:
                    time.sleep(0.5)  # 等下拉框打开后再输入选项。
                    if self._unmanaged_browser_has_focus():
                        return self._desktop_step_result(action, {
                            "status": "uncertain", "dispatched": True,
                            "error": "输入前焦点转到非本任务浏览器，未发送选项和回车",
                        })
                    write_text(str(action.args.get("option", "")), gui=pyautogui)
                    pyautogui.press("enter")
            else:
                cmd = to_pyautogui(action)
                if not cmd:
                    return self._desktop_step_result(action, {
                        "status": "rejected", "dispatched": False, "error": "动作没有可执行命令或有效坐标",
                    })
                dispatched = True
                exec(cmd, {"__name__": "__main__"})  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            return self._desktop_step_result(action, {
                "status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                "error": f"执行异常: {exc}",
            })
        return self._desktop_step_result(action, {"status": "executed", "dispatched": True})

    def _browser_step(self, action, kind):
        """专用浏览器的结构失败仅在网页视口恢复，不向桌面发送输入。"""
        result = {"status": "rejected", "dispatched": False, "channel": "browser_dom"}
        name, args = action.action, action.args
        if not self._browser_subtask_active or not self._browser_ready:
            result["error"] = "尚未进入本任务的浏览器子任务"
        elif args.get("grounding_failed"):
            result["error"] = args.get("grounding_error") or "网页视觉定位失败，请重新观察"
        else:
            session = self._get_browser_session()
            try:
                session.validate_action_snapshot(action)
                if name in ("scroll", "press", "hotkey"):
                    result = session.execute_target(action, args.get("target"))
                elif name in ("click", "double_click", "right_click", "type"):
                    if args.get("x") is None or args.get("y") is None:
                        result["error"] = "网页动作缺少有效节点或视觉坐标"
                    else:
                        point = session.image_to_viewport(args["x"], args["y"])
                        result = session.click_at_viewport(action, *point)
                elif name == "select":
                    result["error"] = "请选择当前 AX 节点；自定义下拉框先点击控件再点击选项"
                elif name == "drag_and_drop":
                    if any(args.get(key) is None for key in ("x1", "y1", "x2", "y2")):
                        result["error"] = "网页拖拽缺少有效视觉坐标"
                    else:
                        result = session.drag_at_viewport(session.image_to_viewport(args["x1"], args["y1"]),
                                                          session.image_to_viewport(args["x2"], args["y2"]))
                elif name == "wait":
                    seconds = min(60.0, max(0.0, float(args.get("seconds", 1))))
                    remaining = session._timeout_seconds(60)
                    if seconds > remaining:
                        raise ModelRequestTimeout("等待时间超过本步剩余浏览器预算")
                    time.sleep(seconds)
                    session._check_deadline()
                    result.update(status="executed", dispatched=False)
                elif kind == "browser":
                    result.update(status="executed", browser_result=self.browser_action(name, args))
                else:
                    result["error"] = f"浏览器子任务不支持桌面动作 {name}，需要明确的桌面子任务"
            except BrowserActionError as exc:
                result.update(status="rejected", dispatched=False, error=str(exc)[:300])
            except ModelRequestTimeout:
                raise
            except Exception as exc:
                result.update(status="uncertain", dispatched=True, error=str(exc)[:300])
        return StepResult(observation=self._build_observation(), info=result)

    def _desktop_step_result(self, action: Action, info: dict) -> StepResult:
        result_info = {"channel": "gui", "action": action.action, **info}
        if result_info.get("dispatched") and action.action != "wait" and self.action_settle_seconds:
            time.sleep(self.action_settle_seconds)
        previous = self._foreground_before_input
        self._foreground_before_input = None
        if result_info.get("status") == "executed" and action.action not in ("wait", "switch_applications"):
            presentation = self._present_new_foreground_window(previous)
            if presentation is not None:
                result_info["window_result"] = presentation
                if presentation.get("status") != "executed":
                    result_info.update(status="uncertain",
                                       error=presentation.get("error") or "新应用窗口未能置顶")
        return StepResult(observation=self._build_observation(), info=result_info)

    # ---- 浏览器操作 ----
    def browser_action(self, action_name: str, args: Dict[str, Any]) -> str:
        """执行 browser kind 动作（当前只有 get_page_source）。"""
        if self.execution_mode == "browser_first":
            try:
                self._get_browser_session().validate_action_snapshot(Action(action_name, args))
            except BrowserActionError as exc:
                return f"浏览器动作已拒绝: {exc}"
        if action_name == "get_page_source":
            return self.get_page_source()
        return f"未知浏览器动作 {action_name}"

    def get_page_source(self) -> str:
        if not self._focus_is_browser():
            return "当前焦点不在浏览器（可能正在操作其他应用），无需抓取页面源码"
        session = self._get_browser_session()
        html = session.get_page_source() if session is not None else ""
        return html or "获取页面源码失败（Chrome 可能未开 remote debugging）"

    # ---- 终端 / Python ----
    def _command_timeout(self):
        deadline = getattr(self, "request_deadline", None)
        remaining = 120.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise ModelRequestTimeout("本步预算已耗尽，未启动本机命令")
        return min(120.0, remaining)

    def run_command(self, command: str) -> str:
        try:
            r = subprocess.run(["cmd", "/c", command], capture_output=True, text=True,
                               timeout=self._command_timeout(), encoding="utf-8", errors="replace")
        except (subprocess.TimeoutExpired, ModelRequestTimeout) as exc:
            raise ModelRequestTimeout("本机终端命令超出当前步骤预算") from exc
        except Exception as exc:  # noqa: BLE001
            return f"命令执行失败: {exc}"
        return (r.stdout or "").strip() or (r.stderr or "").strip() or "(无输出)"

    def run_python(self, code: str) -> str:
        try:
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               timeout=self._command_timeout(), encoding="utf-8", errors="replace")
        except (subprocess.TimeoutExpired, ModelRequestTimeout) as exc:
            raise ModelRequestTimeout("本机 Python 命令超出当前步骤预算") from exc
        except Exception as exc:  # noqa: BLE001
            return f"python 执行失败: {exc}"
        return (r.stdout or "").strip() or (r.stderr or "").strip() or "(无输出)"

    def evaluate(self) -> float:
        # 本地手动任务没有独立评测器：返回 0，由人工/模型 verify 判断是否完成
        return 0.0

    def close(self) -> None:
        self._browser_ready = False
        try:
            if self._browser_session is not None:
                try:
                    self._browser_session.close()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    self._browser_session = None
        finally:
            if not self._window_presenter.restore():
                print("[window] 未能恢复本任务窗口的置顶状态，请检查该窗口")
