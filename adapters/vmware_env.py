"""VMware 环境适配器：把抽象 Environment 接到 OSWorld 的 DesktopEnv + VMware 虚拟机。

关键点：
- 包装 OSWorld 的 DesktopEnv（provider=vmware, action_space=pyautogui）；
- 把新框架的 Action（已坐标化）翻译成 pyautogui 命令字符串（to_pyautogui）；
- 一个 VM 环境实例对应一个 task（reset 会做任务 setup + 快照回退）。
"""
from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

from ..actions import Action
from ..env import Environment, Observation, StepResult, run_local_command, run_local_python
from .uno_commands import SET_CELL_VALUES_CMD, SAVE_DOC_CMD


def to_pyautogui(action: Action) -> str:
    """把新框架的 Action（env 类，已坐标化）翻译成 OSWorld 的 pyautogui 命令字符串。"""
    name = action.action
    a = action.args
    head = "import pyautogui; import time; "

    # grounding 失败：拒绝执行（返回空命令 → step() 空转一步），绝不做坐标钳制。
    if a.get("grounding_failed"):
        return ""

    if name == "click":
        x, y = a.get('x'), a.get('y')
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            # 对 Calc 单元格：双击确保焦点真正切换（单次 click 可能只是激活表格区，不切选中格）。
            # 判断：target 是单元格引用（如 E3 / Sheet1 的 A5）或描述里含"单元格"。
            t = str(a.get("target", "")).lower()
            import re
            if re.fullmatch(r"[a-z]\d+", t.strip()) or "单元格" in a.get("target", ""):
                return f"{head} pyautogui.click({x}, {y}); time.sleep(0.2); pyautogui.click({x}, {y})"
            return f"{head} pyautogui.click({x}, {y})"
        return ""  # grounding 失败：不执行，空转
    if name == "double_click":
        x, y = a.get('x'), a.get('y')
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            return f"{head} pyautogui.doubleClick({x}, {y})"
        return ""
    if name == "right_click":
        x, y = a.get('x'), a.get('y')
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            return f"{head} pyautogui.click({x}, {y}, button='right')"
        return ""
    if name == "type":
        # 原子「聚焦+输入」：先点击目标元素聚焦，再输入文字（对齐 Agent S3 的 type 语义）。
        x, y = a.get('x'), a.get('y')
        cmd = head
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            cmd += f"pyautogui.click({x}, {y}); "
        if a.get('overwrite'):
            # 覆盖写：全选 + 退格删除旧内容，再输入新文字（对齐 Agent S3 的 type(overwrite=True)）。
            cmd += "pyautogui.hotkey('ctrl', 'a'); pyautogui.press('backspace'); "
        cmd += f"pyautogui.write({str(a.get('text', ''))!r})"
        return cmd
    if name == "scroll":
        amount = int(a.get("amount", 1))
        if a.get("direction", "down") == "up":
            pass
        else:
            amount = -amount
        return f"{head} pyautogui.scroll({amount})"
    if name == "press":
        return f"{head} pyautogui.press({str(a.get('key', ''))!r})"
    if name == "hotkey":
        keys = list(a.get("keys", []))
        return f"{head} pyautogui.hotkey(*{keys!r})"
    if name == "goto":
        url = str(a.get("url", ""))
        return (f"{head} pyautogui.hotkey('ctrl', 'l'); time.sleep(0.5); "
                f"pyautogui.write({url!r}); pyautogui.press('enter')")
    if name == "drag_and_drop":
        return (f"{head} pyautogui.moveTo({a.get('x1')}, {a.get('y1')}); "
                f"pyautogui.dragTo({a.get('x2')}, {a.get('y2')}, duration=1.0)")
    if name == "select":
        # 简化：点开下拉框 → 输入选项 → 回车
        return (f"{head} pyautogui.click({a.get('x')}, {a.get('y')}); time.sleep(0.5); "
                f"pyautogui.write({str(a.get('option', ''))!r}); pyautogui.press('enter')")
    if name == "wait":
        return f"{head} time.sleep({float(a.get('seconds', 1))})"
    if name == "save":
        # 程序化保存（UNO store），避开 Ctrl+S 对 xlsx/docx/pptx 弹的"确认格式"对话框
        app_name = a.get("app_name")
        return SAVE_DOC_CMD.format(app_name=repr(app_name) if app_name else "None")
    if name == "switch_applications":
        # 简化：alt+tab 切应用（要按 app_code 精确定位需扩展）
        return f"{head} pyautogui.hotkey('alt', 'tab')"
    if name == "set_cell_values":
        # LibreOffice UNO 直写单元格（不经 GUI），写完后 store() 落盘
        cell_values = a.get("new_cell_values") or {}
        app_name = str(a.get("app_name") or "")
        sheet_name = str(a.get("sheet_name") or "Sheet1")
        return SET_CELL_VALUES_CMD.format(
            cell_values=cell_values, app_name=app_name, sheet_name=sheet_name)
    return ""


def verify_cell_values(env, cell_values, app_name, sheet_name):
    """写完后校验：读目标单元格，确认真的写进去了。返回 (bool, 说明)。"""
    from .uno_commands import VERIFY_CELL_CMD
    ctrl = getattr(env, "controller", None)
    if ctrl is None:
        return False, "无 controller，无法校验"
    try:
        result = ctrl.run_python_script(VERIFY_CELL_CMD.format(
            cell_ref=list(cell_values.keys())[0] if cell_values else "",
            app_name=app_name or "",
            sheet_name=sheet_name or "Sheet1",
        ))
        out = ""
        if isinstance(result, dict):
            out = (result.get("output") or "").strip()
        elif isinstance(result, str):
            out = result.strip()
        if not out:
            return False, "校验脚本无输出"
        # 校验：返回里应有 CELL_VALUE=...（非 None/空）
        if "CELL_VALUE=" in out and ("None" not in out.split("CELL_VALUE=")[1][:20] or "'" in out.split("CELL_VALUE=")[1][:20]):
            return True, out
        return False, out
    except Exception as exc:  # noqa: BLE001
        return False, f"校验失败: {exc}"


class VMwareEnvironment(Environment):
    """包装 OSWorld DesktopEnv 的 VMware 环境。"""

    def __init__(self, task_config: Dict[str, Any], vm_path: Optional[str] = None,
                 headless: bool = False, screen_width: int = 1920,
                 screen_height: int = 1080, desktop_env_path: Optional[str] = None,
                 snapshot_name: str = "init_state_ca3", provider: str = "vmware",
                 cache_dir: Optional[str] = None, decision_model=None):
        self.task_config = task_config
        self.os_name = "linux"           # Ubuntu 虚拟机
        self.screen_width = screen_width
        self.screen_height = screen_height
        # 决策模型（用于 type 落点校验，可选；None 则跳过校验）
        self.decision_model = decision_model
        # 最近一次 type 输入的文字（供 type 落点校验）
        self._last_typed_text: str = ""
        # 浏览器会话（懒加载，跨步复用 CDP 连接）
        self._browser_session = None

        if desktop_env_path:
            sys.path.insert(0, desktop_env_path)

        from desktop_env.desktop_env import DesktopEnv  # noqa: E402
        env_kwargs: Dict[str, Any] = dict(
            provider_name=provider,
            path_to_vm=vm_path,
            snapshot_name=snapshot_name,
            action_space="pyautogui",
            screen_size=(screen_width, screen_height),
            headless=headless,
            os_type="Ubuntu",
            require_a11y_tree=False,
            enable_proxy=True,
        )
        if cache_dir:
            env_kwargs["cache_dir"] = cache_dir
        self._env = DesktopEnv(**env_kwargs)

    @property
    def controller(self):
        return getattr(self._env, "controller", None)

    def _get_browser_session(self):
        """懒加载 BrowserSession（保持 CDP 连接，跨步复用）。连不上返回 None。"""
        host = getattr(self._env, "vm_ip", None)
        port = getattr(self._env, "chromium_port", None) or 9222
        if not host:
            return None
        if self._browser_session is None:
            from ..browser_session import BrowserSession, BrowserOptions
            self._browser_session = BrowserSession(
                options=BrowserOptions(endpoint=f"http://{host}:{port}"),
                foreground_check=self._focus_is_browser,
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

    def route_action(self, action, obs):
        """路由动作：带 target_ref 且引用有效 → browser_dom；否则 None（GUI grounding）。

        语义兜底：base 模型常漏给/给错 target_ref，此时用 action 的英文 target 描述
        在 AX 快照里确定性匹配一个 ref 回填，再走 DOM 定位——不逼模型逐字抄 hex ref。
        """
        session = self._get_browser_session()
        if session is None or session.snapshot is None or obs is None:
            return None
        try:
            route = session.route(action, obs)
        except Exception:  # noqa: BLE001
            route = None
        # 模型自己给的 target_ref 有效 → 直接用
        if route is not None and route.get("channel") == "browser_dom":
            return route
        # 语义回退：target_ref 缺失/无效时，用 target 描述在快照里找 ref
        target = str(action.args.get("target") or "")
        if target and action.action in ("click", "double_click", "right_click", "type", "select"):
            exposed = obs.info.get("exposed_refs") if isinstance(obs.info, dict) else None
            # [] = 没有任何 ref 暴露给模型，无法 DOM 路由；None = info 未设置，不限制
            if exposed is not None and not exposed:
                return route
            allowed = set(exposed) if exposed else None
            try:
                ref = session.resolve_target(target, allowed=allowed)
            except Exception:  # noqa: BLE001
                ref = None
            if ref:
                action.args["target_ref"] = ref
                return session.route(action, obs)
        # 有 ref 但无效 → reobserve；无 ref → None（走 GUI grounding）
        return route

    def execute_routed(self, action, route):
        """执行已路由的 browser_dom 动作（DOM 定位 + actionability check）。"""
        session = self._get_browser_session()
        if session is None:
            return self.step(action)
        result = session.execute(action, route)
        obs = self._fresh_obs_after_action()
        info = dict(result)
        return StepResult(observation=obs, info=info)

    def _build_observation(self, raw_obs) -> Observation:
        """从 desktop_env 的 obs 构建 Observation：browser 快照进 context，桌面 AX fallback 进 text。"""
        screenshot = raw_obs.get("screenshot") if isinstance(raw_obs, dict) else None
        snapshot = self._collect_browser_snapshot()
        context = [snapshot] if snapshot else []
        text = ""
        if snapshot is None and isinstance(raw_obs, dict):
            text = self._a11y_text(raw_obs)
        return Observation(screenshot=screenshot, text=text, context=context)

    def _fresh_obs_after_action(self) -> Observation:
        """动作执行后重新观察：新截图 + 新 browser 快照 + 桌面 AX fallback。"""
        raw = None
        try:
            raw = self._env._get_obs() if hasattr(self._env, "_get_obs") else None
        except Exception:  # noqa: BLE001
            raw = None
        return self._build_observation(raw)

    def _focus_is_browser(self) -> bool:
        """检测 VM 里当前焦点窗口是否是浏览器（毫秒级）。

        优先 xdotool 读活动窗口名（严格焦点检测）；VM 里没有 xdotool 时降级为
        CDP 探测（浏览器开着且有 http 网页就认为在浏览器场景——OSWorld 单任务场景够用，
        真实多应用系统请确保 VM 里装了 xdotool 以启用严格焦点检测）。
        """
        # 1) 试 xdotool 焦点检测
        try:
            out = self.run_command(
                "xdotool getactivewindow getwindowname 2>/dev/null "
                "|| xdotool getwindowfocus getwindowname 2>/dev/null"
            )
            out = (out or "").strip()
            # 只有拿到「有效的窗口名」才据其判断；"(无输出)/失败/not found" 都视为 xdotool 不可用，进 fallback
            if out and out != "(无输出)" and "失败" not in out and "not found" not in out and "No such file" not in out:
                return any(k in out.lower()
                           for k in ("chrome", "chromium", "firefox", "msedge", "edge", "browser"))
        except Exception:  # noqa: BLE001
            pass
        # 2) fallback：CDP 探测（xdotool 不可用）
        host = getattr(self._env, "vm_ip", None)
        port = getattr(self._env, "chromium_port", None) or 9222
        if not host:
            return False
        try:
            from ..utils import list_cdp_pages
            pages = list_cdp_pages(host, port)
            # 有页面（http 网页 或 chrome:// 设置页，排除 devtools/扩展）就认为在浏览器场景
            return any(
                p.get("url") and not p.get("url", "").startswith(("devtools://", "chrome-extension://"))
                for p in pages
            )
        except Exception:  # noqa: BLE001
            return False

    def _page_a11y(self) -> str:
        """抓当前浏览器页面的「可交互元素 + selector」，browser use 决策用。

        只有「焦点在浏览器」时才抓——浏览器开着但焦点不在，说明任务不在浏览器做，
        抓页面信息是错的（白抓且可能误导决策模型）。

        返回的是「role "name" [selector]」列表（如 button "百度一下" [#chat-submit-button]），
        决策模型直接抄 selector 输出 click_element/fill，跳过视觉定位模型。
        """
        if not self._focus_is_browser():
            return ""
        host = getattr(self._env, "vm_ip", None)
        port = getattr(self._env, "chromium_port", None) or 9222
        if not host:
            return ""
        try:
            from .. import utils
            # 判断当前页面是不是 WebUI（chrome://settings 等 shadow DOM）。
            # WebUI 里 fetch_interactive_elements（querySelectorAll）只能抓到极少的 light DOM
            # 元素（如一个搜索框），列表不完整会误导模型；必须直接用 AX-Tree（穿透 shadow DOM）。
            pages = utils.list_cdp_pages(host, port)
            current_url = ""
            for p in pages:
                if p.get("url") and not p["url"].startswith(("devtools://", "chrome-extension://")):
                    current_url = p["url"]
            is_webui = current_url.startswith(("chrome://", "edge://", "about:", "view-source:"))
            if not is_webui:
                items = utils.fetch_interactive_elements(host, port)
                if items:
                    text = utils.interactive_elements_to_text(items)
                    return text + "\n\n（上面每个元素带 [selector]，定位【优先】用 click_element / fill，selector 从 [ ] 里原样抄）"
            # WebUI 或抓不到：用 AX-Tree。AX-Tree 没有 selector，必须明确告诉模型「不要用 click_element」，
            # 否则模型会把 role+name 误当成 selector 输出，导致 click_element 失败。
            a11y = utils.fetch_accessibility_tree(host, port)
            return (a11y + "\n\n（这是可访问性树，元素只有 role+name、没有 selector；"
                    "定位【不要用】click_element，用 click + target 描述走视觉定位模型）")
        except Exception:  # noqa: BLE001
            return ""

    def _a11y_text(self, obs) -> str:
        """优先 CDP 页面 AX-Tree（浏览器场景），fallback AT-SPI 全桌面树（桌面应用场景）。"""
        page = self._page_a11y()
        if page:
            return page
        if not isinstance(obs, dict):
            return ""
        at = obs.get("accessibility_tree")
        if not at:
            return ""
        s = at if isinstance(at, str) else str(at)
        if len(s) > 8000:
            return s[:8000] + "\n...(AX-Tree 过长已截断)"
        return s

    # ---- Environment 接口 ----
    def reset(self, task: str) -> Observation:
        obs = self._env.reset(self.task_config)
        # launch 是异步的（服务器端 subprocess.Popen 立即返回），reset 返回的截图
        # 可能早于目标应用窗口出现，导致 agent 第一步面对"空桌面"而走错路。
        # 对含 launch 步骤的任务，等屏幕稳定（窗口已弹出且动画结束）后再返回截图。
        if self._has_launch_step():
            screenshot = obs.get("screenshot") if isinstance(obs, dict) else None
            screenshot = self._wait_until_screen_stable(screenshot)
            return self._build_observation({"screenshot": screenshot})
        # 对齐 Agent S3 的 reset 后 time.sleep(10)：等环境（应用启动/文件打开）
        # 完全 ready 再截图，否则初始截图是"半成品"状态会误导后续每一步决策。
        time.sleep(10)
        try:
            fresh = self._env._get_obs()
            return self._build_observation(fresh)
        except Exception:  # noqa: BLE001  截图取不到就退回 reset 返回的观察
            pass
        return self._build_observation(obs)

    def _has_launch_step(self) -> bool:
        """任务 setup 里是否含 launch 步骤（含则说明有应用是异步启动的）。"""
        for cfg in self.task_config.get("config", []) or []:
            if cfg.get("type") == "launch":
                return True
        return False

    def _wait_until_screen_stable(self, initial, max_wait: float = 15.0,
                                  stable_frames: int = 3, interval: float = 0.6):
        """轮询截图，直到连续 stable_frames 帧几乎无变化（应用窗口已稳定），或超时。

        用 64x64 灰度 mean-abs-diff 判定"无变化"；截图取不到或超时就退回当前已有截图，
        保证不会比直接返回更差。
        """
        ctrl = self.controller
        if ctrl is None:
            return initial
        get_shot = getattr(ctrl, "get_screenshot", None)
        if get_shot is None:
            return initial
        try:
            from PIL import ImageChops
        except ImportError:
            return initial

        def diff(a, b):
            try:
                ia = a.convert("L").resize((64, 64))
                ib = b.convert("L").resize((64, 64))
                hist = ImageChops.difference(ia, ib).histogram()
                return sum(i * c for i, c in enumerate(hist)) / (64 * 64)
            except Exception:
                return 255.0

        prev = initial
        if prev is None:
            try:
                prev = get_shot()
            except Exception:
                return initial

        stable = 0
        elapsed = 0.0
        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval
            try:
                cur = get_shot()
            except Exception:
                continue
            if cur is None:
                continue
            if diff(prev, cur) < 2.0:
                stable += 1
            else:
                stable = 0
            prev = cur
            if stable >= stable_frames:
                return cur
        return prev if prev is not None else initial

    def step(self, action: Action) -> StepResult:
        # set_cell_values / save 是 LibreOffice UNO 多行脚本（含 def/for/缩进），
        # 必须走 controller.run_python_script 保留多行结构。走 env.step 会被
        # execute_python_command 拼成 "python -c" 单行，破坏缩进结构导致静默失败
        # （表现为"写入校验：失败"、单元格写不进去）。
        if action.action in ("set_cell_values", "save"):
            cmd = to_pyautogui(action)
            ctrl = self.controller
            if ctrl is not None and hasattr(ctrl, "run_python_script"):
                try:
                    result = ctrl.run_python_script(cmd)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)}
                obs = self._env._get_obs() if hasattr(self._env, "_get_obs") else {"screenshot": None}
                return StepResult(
                    observation=self._build_observation(obs),
                    info={"uno_result": result},
                )
            # 无 controller 时回退到 env.step
        cmd = to_pyautogui(action)
        if not cmd:
            # grounding 失败（坐标 -1）或未实现动作：空转一步，不执行 pyautogui
            import time as _t
            _t.sleep(1.0)
            obs = self._env._get_obs() if hasattr(self._env, "_get_obs") else {"screenshot": None}
            return StepResult(observation=self._build_observation(obs))
        obs, reward, done, info = self._env.step(cmd, pause=3)
        screenshot = obs.get("screenshot") if isinstance(obs, dict) else None
        info = info or {}

        # type 落点校验：输入后让决策模型看一眼截图，确认文字打进了预期的输入框。
        # 典型错误：Find/Replace 场景下 grounding 把 Replace 框定位回 Find 框，
        # 导致 "text"+"test" 都打进 Find 框变成 "texttest"。校验失败就把标记写进 info，
        # pipeline 会把它暴露给模型，并提示先撤销（Ctrl+Z）再正确聚焦重输。
        if action.action == "type":
            typed = str(action.args.get("text", ""))
            if typed:
                warn = self._check_type_target(screenshot, typed)
                if warn:
                    info["type_wrong_target"] = True
                    info["type_check"] = warn
            self._last_typed_text = typed

        return StepResult(observation=self._build_observation(obs),
                          reward=float(reward or 0.0), done=bool(done), info=info)

    def _check_type_target(self, screenshot, typed: str) -> Optional[str]:
        """type 后的落点校验：判断输入的文字是否出现在合理的位置。

        返回 None 表示「看着没问题」（放行）；返回一段中文说明表示「疑似打错框」。
        依赖决策模型的视觉能力；模型不可用或截图缺失时一律放行（不误伤、不阻塞主流程）。
        """
        if self.decision_model is None or screenshot is None:
            return None
        prompt = (
            "用户刚在一个输入框里输入了文字「%s」。请仔细看这张截图：\n"
            "1. 找到这段文字「%s」现在出现在屏幕上的哪个输入框/控件里；\n"
            "2. 判断它是否出现在【合理的目标位置】。最典型的错误是：做查找替换时，"
            "本该输入到「Replace/替换为」框的文字，被输入到了「Find/查找」框里"
            "（症状：Find 框里的内容变成了「查找词+刚输入的文字」拼在一起，如 texttest）。\n"
            "只输出一个词：OK（位置合理）或 WRONG（位置错误/打错框）。"
            % (typed, typed)
        )
        try:
            from ..model.decision_model import encode_image
            url = encode_image(screenshot)
            if not url:
                return None
            messages = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": prompt},
            ]}]
            raw = self.decision_model.chat(messages, retries=1)
            if "WRONG" in (raw or "").upper():
                return ("输入后检测到文字可能打进了错误的输入框（常见：查找词被拼进了查找框）。"
                        "请先用 hotkey [\"ctrl\",\"z\"] 撤销这次错误输入，"
                        "再确认焦点在正确的输入框（如 Replace 框）后重新输入。")
        except Exception:  # noqa: BLE001  校验失败放行，不影响主流程
            return None
        return None

    def run_command(self, command: str) -> str:
        ctrl = self.controller
        if ctrl is not None:
            runner = getattr(ctrl, "run_bash_script", None)
            if runner is not None:
                try:
                    result = runner(command)
                    if isinstance(result, str):
                        return result.strip() or "(无输出)"
                    if isinstance(result, dict):
                        out = (result.get("output") or "").strip()
                        err = (result.get("error") or "").strip()
                        return (out + ("\n" + err if err else "")).strip() or "(无输出)"
                except Exception:  # noqa: BLE001
                    pass
        return run_local_command(command, "linux")

    def run_python(self, code: str) -> str:
        ctrl = self.controller
        if ctrl is not None:
            runner = getattr(ctrl, "run_python_script", None)
            if runner is not None:
                try:
                    result = runner(code)
                    if isinstance(result, str):
                        return result.strip() or "(无输出)"
                    if isinstance(result, dict):
                        out = (result.get("output") or "").strip()
                        err = (result.get("error") or "").strip()
                        return (out + ("\n" + err if err else "")).strip() or "(无输出)"
                except Exception:  # noqa: BLE001
                    pass
        return run_local_python(code)

    def get_page_source(self) -> str:
        """通过 Chrome DevTools Protocol 获取当前页面 URL + HTML 源码。

        browser use 场景：让决策模型直接读 DOM/源码，而不是只靠截图猜坐标。
        Chrome 在 Linux/Windows/Mac 都支持 remote debugging（9222 端口），因此跨平台。
        连接失败 / 非浏览器任务时返回说明性文字，不抛异常。
        """
        env = self._env
        if env is None:
            return "环境未初始化，无法获取页面源码"
        if not self._focus_is_browser():
            return "当前焦点不在浏览器（可能正在操作其他应用），无需抓取页面源码"
        host = getattr(env, "vm_ip", None)
        port = getattr(env, "chromium_port", None) or 9222
        if not host:
            return "无法确定目标机 IP，无法连接 Chrome"
        remote_url = f"http://{host}:{port}"
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            return f"playwright 不可用，无法连接 Chrome CDP：{exc}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(remote_url)
                try:
                    contexts = browser.contexts
                    if not contexts:
                        return "Chrome 没有打开的页面上下文"
                    pages = contexts[0].pages
                    if not pages:
                        return "Chrome 没有打开的标签页"
                    # 选「当前页面」：优先最后一个非 chrome:// 的普通网页
                    page = None
                    for pg in reversed(pages):
                        u = pg.url or ""
                        if u and not u.startswith(("chrome://", "about:", "devtools://", "edge://")):
                            page = pg
                            break
                    if page is None:
                        page = pages[-1]
                    url = page.url
                    html = page.content()
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            return f"获取页面源码失败（Chrome 可能未打开或未开 remote debugging）：{exc}"
        return f"URL: {url}\n\nHTML:\n{html}"

    def browser_action(self, action_name: str, args: dict) -> str:
        """执行 browser kind 动作。当前只有 get_page_source（读精简 DOM）。

        浏览器操作（click/type/select/scroll）已改为走 route_action/execute_routed
        （target_ref DOM 定位），不再走这里。
        """
        if action_name == "get_page_source":
            return self.get_page_source()
        return f"未知浏览器动作 {action_name}"

    def evaluate(self) -> float:
        return float(self._env.evaluate())

    def close(self) -> None:
        # 先关闭浏览器会话（断开 playwright/CDP 连接），避免跨任务的 asyncio loop 冲突
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser_session = None
        if self._env is not None:
            try:
                self._env.close()
            except Exception:  # noqa: BLE001
                pass
