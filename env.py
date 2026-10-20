"""环境层：统一环境接口 + OSWorld 适配器。

设计原则：智能体只依赖下面的 Environment 接口，不直接碰 desktop_env，
这样换评测平台（OSWorld / WebArena / 浏览器）不影响智能体逻辑。
"""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .actions import Action


@dataclass
class Observation:
    """一步观察结果。"""
    screenshot: Any = None          # 截图（PIL.Image 或文件路径，具体由后端决定）
    text: str = ""                  # 可选的文本化信息（如元素坐标列表、accessibility）
    info: Dict[str, Any] = field(default_factory=dict)
    context: list = field(default_factory=list)  # 结构化观察块（browser_ax snapshot 等）


@dataclass
class StepResult:
    """执行一个动作后的返回。"""
    observation: Observation
    reward: float = 0.0
    done: bool = False
    info: Dict[str, Any] = field(default_factory=dict)


class Environment(ABC):
    """环境统一接口。"""

    # Whether evaluate() is an independent task evaluator, rather than a stub.
    evaluation_available = False

    @abstractmethod
    def reset(self, task: str) -> Observation:
        """启动任务环境，返回初始观察。"""
        raise NotImplementedError

    def observe(self) -> Observation:
        """刷新当前状态，不执行动作，也不重置任务或虚拟机快照。

        后端必须显式实现；不能通过 reset() 模拟观察。
        """
        raise NotImplementedError("This environment does not implement observe()")

    @abstractmethod
    def step(self, action: Action) -> StepResult:
        """执行一个动作，返回新观察 + 是否完成。"""
        raise NotImplementedError

    @abstractmethod
    def run_command(self, command: str) -> str:
        """在目标机终端执行一条命令，返回输出（OS 感知）。"""
        raise NotImplementedError

    @abstractmethod
    def run_python(self, code: str) -> str:
        """在目标机执行一段 Python 代码，返回输出。"""
        raise NotImplementedError

    @abstractmethod
    def get_page_source(self) -> str:
        """获取当前浏览器页面的 URL + HTML 源码（经 Chrome DevTools Protocol）。

        browser use 场景专用：让决策模型直接读页面 DOM/源码，而不是只靠截图猜坐标。
        跨平台（Linux/Windows/Mac 的 Chrome 都支持 remote debugging）。非浏览器任务
        或连接失败时返回说明性文字，不抛异常。
        """
        raise NotImplementedError

    def route_action(self, action: Action, obs: "Observation"):
        """路由动作到执行通道（browser_dom / reobserve / None=GUI grounding）。

        默认返回 None（走 GUI grounding）。支持结构化 DOM 定位的环境（如 VM）覆写此方法：
        - 动作带 target_ref 且引用有效 → {"channel": "browser_dom", ...}
        - target_ref 过期/无效 → {"channel": "reobserve", "reason": ...}
        """
        return None

    def execute_routed(self, action: Action, route: dict) -> StepResult:
        """执行已路由的动作（browser_dom 通道）。默认回退 step()。"""
        return self.step(action)

    def browser_event_metadata(self) -> dict:
        """Poll an existing session without screenshots or opening a browser."""
        session = getattr(self, "_browser_session", None)
        return session.event_metadata() if session is not None else {}

    def cancel_download(self, download_id: str):
        session = getattr(self, "_browser_session", None)
        if session is not None:
            return session.cancel_download(download_id)
        return {"status": "rejected", "dispatched": False, "error": "No active browser session"}

    @abstractmethod
    def evaluate(self) -> float:
        """任务结束后的得分（0 或 1）。"""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """释放环境资源（关虚拟机/容器）。"""
        raise NotImplementedError


# 目标机操作系统 -> 本地回退时用的 shell 前缀
_SHELL_PREFIX = {
    "linux": ["bash", "-c"],
    "mac": ["bash", "-c"],
    "windows": ["cmd", "/c"],
}


def run_local_command(command: str, os_name: str) -> str:
    """在 agent 本机执行命令（离线回退），按操作系统选 shell。

    真实跑任务时走 OSWorldEnvironment.run_command（优先用目标机 controller）。
    支持 linux / mac / windows。
    """
    os_name = (os_name or "linux").lower()
    shell = _SHELL_PREFIX.get(os_name)
    if shell is None:
        return f"未知操作系统「{os_name}」，支持 linux / mac / windows"
    try:
        r = subprocess.run(shell + [command], capture_output=True, text=True,
                           timeout=120)
    except subprocess.TimeoutExpired:
        return f"命令执行超时（>120s）: {command}"
    except Exception as exc:  # noqa: BLE001
        return f"命令执行失败: {exc}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out and err:
        return out + "\n" + err
    return out or err or f"(返回码 {r.returncode}，无输出)"


def run_local_python(code: str) -> str:
    """在 agent 本机执行一段 Python 代码（离线回退）。"""
    import subprocess
    for py in ("python3", "python"):
        try:
            r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            return f"python 执行失败: {exc}"
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return (out + ("\n" + err if err else "")).strip() or f"(返回码 {r.returncode}，无输出)"
    return "本机找不到 python 解释器"


class OSWorldEnvironment(Environment):
    """OSWorld 适配器（占位）。

    TODO(下一步接入)：对接 desktop_env。映射关系如下（待实现）：
      - reset(task)     -> desktop_env.reset(task_id) 得到初始截图
      - step(action)    -> 把本框架的 Action 翻译成 OSWorld 的 pyautogui 命令
                           （click(x,y) / type(text) / key_press / hotkey / scroll / select）
      - run_command(cmd)-> 在 VM 终端执行 shell 命令（OS 感知）
      - evaluate()      -> desktop_env.evaluator 打分
      - close()         -> desktop_env.close() 关 VM
    """

    def __init__(self, vm_path: str, headless: bool = False,
                 width: int = 1920, height: int = 1080,
                 sleep_after_execution: float = 1.5, os_name: str = "linux"):
        self.vm_path = vm_path
        self.headless = headless
        self.width = width
        self.height = height
        self.sleep_after_execution = sleep_after_execution
        self.os_name = os_name            # 目标机操作系统：linux / mac / windows
        self._env = None  # 真实的 desktop_env 实例，接入时创建

    @property
    def controller(self):
        """目标机控制器（环境接入后才有）。供 skill / 终端命令查询目标机用。"""
        return getattr(self._env, "controller", None)

    def reset(self, task: str) -> Observation:
        raise NotImplementedError("OSWorld 后端尚未接入，见类注释 TODO")

    def step(self, action: Action) -> StepResult:
        raise NotImplementedError("OSWorld 后端尚未接入，见类注释 TODO")

    def run_command(self, command: str) -> str:
        """在目标机终端执行命令（OS 感知）。控制器可用走目标机，否则本地回退。"""
        ctrl = self.controller
        if ctrl is not None:
            out = self._run_via_controller(ctrl, command)
            if out is not None:
                return out
        return run_local_command(command, self.os_name)

    def run_python(self, code: str) -> str:
        """在目标机执行一段 Python 代码。控制器可用走目标机，否则本地回退。"""
        ctrl = self.controller
        if ctrl is not None:
            runner = getattr(ctrl, "run_python_script", None)
            if runner is not None:
                try:
                    result = runner(code)
                except Exception:  # noqa: BLE001
                    result = None
                if isinstance(result, str):
                    return result.strip() or "(无输出)"
                if isinstance(result, dict):
                    out = (result.get("output") or "").strip()
                    err = (result.get("error") or "").strip()
                    return (out + ("\n" + err if err else "")).strip() or "(无输出)"
        return run_local_python(code)

    def get_page_source(self) -> str:
        """OSWorld 后端尚未接入，无法获取页面源码（接入后走 Chrome CDP）。"""
        return "OSWorld 后端尚未接入，无法获取页面源码"

    def _run_via_controller(self, ctrl, command: str) -> Optional[str]:
        """通过控制器在目标机执行命令；返回 None 表示不可用。"""
        if self.os_name.lower() == "windows":
            runner = getattr(ctrl, "run_cmd_script", None) or getattr(ctrl, "run_powershell_script", None)
        else:
            runner = getattr(ctrl, "run_bash_script", None)
        if runner is None:
            return None
        try:
            result = runner(command)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(result, str):
            return result.strip() or None
        if isinstance(result, dict):
            out = (result.get("output") or "").strip()
            err = (result.get("error") or "").strip()
            return (out + ("\n" + err if err else "")).strip() or None
        return None

    def evaluate(self) -> float:
        raise NotImplementedError("OSWorld 后端尚未接入，见类注释 TODO")

    def close(self) -> None:
        if self._env is not None:
            # 接入后调用 self._env.close()
            self._env = None


def build_environment(cfg) -> Environment:
    """根据配置构造环境。目前只支持 osworld。"""
    from .config import EnvConfig
    env_cfg: EnvConfig = cfg.env
    if env_cfg.provider != "osworld":
        raise ValueError(f"不支持的环境 provider: {env_cfg.provider}")
    return OSWorldEnvironment(
        vm_path=env_cfg.vm_path,
        headless=env_cfg.headless,
        width=env_cfg.screen_width,
        height=env_cfg.screen_height,
        sleep_after_execution=env_cfg.sleep_after_execution,
        os_name=env_cfg.os,
    )
