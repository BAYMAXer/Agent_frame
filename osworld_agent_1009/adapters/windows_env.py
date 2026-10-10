"""Windows 本机环境：pyautogui 控制桌面 + CDP 控制浏览器。

用于在 Windows 本机（非 VM）上跑 GUI agent 任务，重点支持 browser use：
- 观察：pyautogui 截图 + browser_ax 快照（焦点在浏览器时经 BrowserSession 抓取）；
- 执行：桌面 GUI 动作（click/type/hotkey 等）走 pyautogui（坐标定位）；
        浏览器 DOM 动作（带 target_ref / 语义兜底命中）走 CDP（DOM 定位，跳过视觉定位）；
- 焦点检测走 Windows GetForegroundWindow。

与 VMwareEnvironment 的对齐点：browser_ax 快照进 context、target_ref 路由 + 语义兜底、
execute_routed 走 CDP。差异点：截图用 pyautogui、CDP host 是 localhost、桌面无 AT-SPI。
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Any, Dict, Optional

from ..env import Environment, Observation, StepResult
from ..actions import Action, get_action_spec
from .vmware_env import to_pyautogui


class WindowsEnvironment(Environment):
    """Windows 本机环境（GUI + browser use）。"""

    def __init__(self, browser_port: int = 9222, width: int = 1920, height: int = 1080,
                 downloads_dir: Optional[str] = None):
        self.browser_port = browser_port
        self.width = width
        self.height = height
        self.os_name = "windows"
        self.browser_only = True  # 浏览器专用：用精简 system prompt + 强化 DOM 定位
        self.downloads_dir = downloads_dir
        self._browser_session = None

    # ---- 观察 ----
    def _take_screenshot(self):
        import pyautogui
        return pyautogui.screenshot()

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
                                       require_focus=False),
                foreground_check=None,
                downloads_dir=self.downloads_dir,
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
        """截图 + browser_ax 快照进 context；显式标注场景（browser_use / structure_available）。"""
        screenshot = self._take_screenshot()
        snapshot = self._collect_browser_snapshot()
        context = [snapshot] if snapshot else []
        info = {"scene": self._derive_scene(snapshot)}
        return Observation(screenshot=screenshot, text="", context=context, info=info)

    def _derive_scene(self, snapshot):
        """算显式场景：有快照→browser_dom；浏览器但无结构→visual；非浏览器→visual/desktop。"""
        from ..scene import Scene
        if snapshot:
            return Scene(browser_use=1, structure_available=True, mode="browser_dom",
                         reason="structured_page", url=snapshot.get("url", ""),
                         snapshot_id=snapshot.get("snapshot_id", "")).to_dict()
        if self._focus_is_browser():
            return Scene(browser_use=1, structure_available=False, mode="visual",
                         reason="no_structured_page").to_dict()
        return Scene(browser_use=0, structure_available=False, mode="visual",
                     reason="not_browser").to_dict()

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
        if obs is None or action.action not in self._BROWSER_CDP_ACTIONS:
            return None
        # 只有「浏览器可用」才走 CDP；否则回 GUI（桌面应用场景）
        if not self._focus_is_browser():
            return None
        session = self._get_browser_session()
        if session is None:
            return None
        # 地址栏输入 URL → 转 goto（地址栏是浏览器 chrome UI，不在 DOM）
        url = self._address_bar_url(action)
        if url:
            return {"channel": "browser_dom", "goto_url": url, "coordinate_space": "css_viewport"}
        # goto / scroll / press / hotkey：无 target，直接 browser_dom
        if action.action in ("goto", "scroll", "press", "hotkey"):
            return {"channel": "browser_dom", "target": action.args.get("target"),
                    "coordinate_space": "css_viewport"}
        # click / type / select / double / right：优先 DOM 定位
        try:
            route = session.route(action, obs)  # 处理 target_ref / element_id
        except Exception:  # noqa: BLE001
            route = None
        if route is not None:
            return route  # browser_dom 或 reobserve
        # 语义兜底：target 描述 → ref（仍在结构层）
        target = str(action.args.get("target") or "")
        if target:
            exposed = obs.info.get("exposed_refs") if isinstance(obs.info, dict) else None
            allowed = set(exposed) if exposed else None
            try:
                ref = session.resolve_target(target, allowed=allowed)
            except Exception:  # noqa: BLE001
                ref = None
            if ref:
                action.args["target_ref"] = ref
                return {"channel": "browser_dom", "snapshot_id": session.snapshot["snapshot_id"],
                        "target_ref": ref, "coordinate_space": "css_viewport"}
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
        session = self._get_browser_session()
        if session is None:
            return self.step(action)
        if action.action == "goto" or route.get("goto_url"):
            result = session.goto(str(action.args.get("url") or route.get("goto_url")))
        elif action.action in ("scroll", "press", "hotkey"):
            result = session.execute_target(action, None)
        elif route.get("target_ref"):
            result = session.execute(action, route)
        else:
            result = {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                      "error": "no routable target"}
        obs = self._fresh_obs_after_action()
        info = dict(result)
        return StepResult(observation=obs, info=info)

    # ---- Environment 接口 ----
    def reset(self, task: str) -> Observation:
        return self._build_observation()

    def step(self, action: Action) -> StepResult:
        name = action.action
        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"

        # 浏览器场景 + 视觉定位坐标 → CDP 虚拟点击（不动真实鼠标）
        if (kind in ("env", "uno")
                and name in ("click", "double_click", "right_click", "type")
                and action.args.get("x") is not None and action.args.get("y") is not None
                and not action.args.get("grounding_failed")
                and self._focus_is_browser()):
            session = self._get_browser_session()
            if session is not None:
                result = session.click_at_screen(action, int(action.args["x"]), int(action.args["y"]))
                return StepResult(observation=self._build_observation(), info=dict(result))

        if kind == "browser":
            result = self.browser_action(name, action.args)
            return StepResult(observation=self._build_observation(), info={"browser_result": result})

        if kind == "shell":
            output = self.run_command(str(action.args.get("command", "")))
            return StepResult(observation=self._build_observation(), info={"terminal_result": output})

        # env / uno 动作：坐标化后执行 pyautogui 命令
        cmd = to_pyautogui(action)
        if cmd:
            try:
                exec(cmd, {"__name__": "__main__"})  # noqa: S102  执行 pyautogui 命令字符串
            except Exception as exc:  # noqa: BLE001
                return StepResult(observation=self._build_observation(),
                                  info={"error": f"执行异常: {exc}"})
        return StepResult(observation=self._build_observation())

    # ---- 浏览器操作 ----
    def browser_action(self, action_name: str, args: Dict[str, Any]) -> str:
        """执行 browser kind 动作（当前只有 get_page_source）。"""
        if action_name == "get_page_source":
            return self.get_page_source()
        return f"未知浏览器动作 {action_name}"

    def get_page_source(self) -> str:
        if not self._focus_is_browser():
            return "当前焦点不在浏览器（可能正在操作其他应用），无需抓取页面源码"
        from .. import utils
        html = utils.fetch_page_source("localhost", self.browser_port)
        return html or "获取页面源码失败（Chrome 可能未开 remote debugging）"

    # ---- 终端 / Python ----
    def run_command(self, command: str) -> str:
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
        # 本地手动任务没有独立评测器：返回 0，由人工/模型 verify 判断是否完成
        return 0.0

    def close(self) -> None:
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser_session = None
