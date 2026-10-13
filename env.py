"""Windows 本机环境接口。

智能体依赖统一 Environment 接口，由 Windows 本机适配器执行。
浏览器页面和 Windows 桌面共享 Observation 与 StepResult。
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

    @abstractmethod
    def reset(self, task: str) -> Observation:
        """启动任务环境，返回初始观察。"""
        raise NotImplementedError

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
        Windows Chrome 通过 remote debugging 接入。非浏览器任务
        或连接失败时返回说明性文字，不抛异常。
        """
        raise NotImplementedError

    def route_action(self, action: Action, obs: "Observation"):
        """路由动作到执行通道（browser_dom / reobserve / None=GUI grounding）。

        默认返回 None（走 GUI grounding）。Windows 浏览器适配器覆写此方法：
        - 动作带 target_ref 且引用有效 → {"channel": "browser_dom", ...}
        - target_ref 过期/无效 → {"channel": "reobserve", "reason": ...}
        """
        return None

    def execute_routed(self, action: Action, route: dict) -> StepResult:
        """执行已路由的动作（browser_dom 通道）。默认回退 step()。"""
        return self.step(action)

    @abstractmethod
    def evaluate(self) -> float:
        """任务结束后的得分（0 或 1）。"""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """释放本次运行的浏览器和窗口资源。"""
        raise NotImplementedError


# Windows 本机命令与环境工厂。
def run_local_command(command: str, os_name: str = "windows") -> str:
    if (os_name or "windows").lower() != "windows":
        return "仅支持 Windows 本机命令"
    try:
        result = subprocess.run(["cmd", "/c", command], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "本机命令执行超时（120 秒）"
    except Exception as exc:
        return f"本机命令执行失败: {exc}"
    return (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip() or f"(返回码 {result.returncode}，无输出)"


def run_local_python(code: str) -> str:
    import sys
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "本机 Python 执行超时（120 秒）"
    except Exception as exc:
        return f"本机 Python 执行失败: {exc}"
    return (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip() or f"(返回码 {result.returncode}，无输出)"


def build_environment(env_cfg, context_cfg=None):
    """构造唯一支持的 Windows 本机环境。"""
    from .adapters.windows_env import WindowsEnvironment
    from .run_windows_local import CHROME_DEFAULT, ensure_chrome
    chrome_path = getattr(env_cfg, "chrome_path", "") or CHROME_DEFAULT
    return WindowsEnvironment(browser_port=getattr(env_cfg, "browser_port", 9222),
        width=env_cfg.screen_width, height=env_cfg.screen_height,
        execution_mode=env_cfg.execution_mode,
        screenshot_max_edge=getattr(context_cfg, "screenshot_max_edge", 1280),
        browser_launcher=lambda: ensure_chrome(getattr(env_cfg, "browser_port", 9222), chrome_path),
        action_settle_seconds=getattr(env_cfg, "action_settle_seconds", 0.25),
        downloads_dir=getattr(env_cfg, "downloads_dir", None))
