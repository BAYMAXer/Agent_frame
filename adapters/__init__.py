"""适配层：把抽象框架接到真实的 OSWorld VMware 虚拟机 + 真实 UI-TARS / UI-Venus-2 grounding。

不改动框架任何现有文件，只在这里提供：
- VMwareEnvironment：实现 Environment 接口，包装 OSWorld 的 DesktopEnv；
- RealUItarsGrounding：实现 Grounding 接口，真实请求 UI-TARS 端点；
- UIVenus2Grounding：实现 Grounding 接口，真实请求 UI-Venus-2 端点（[0,1000] 归一化坐标）；
- task_loader：从 evaluation_examples 加载 task_config；
- run_vm：入口，组装环境 + grounding + agent 跑真实任务。
"""
from __future__ import annotations

from .vmware_env import VMwareEnvironment, to_pyautogui
from .windows_env import WindowsEnvironment
from .uitars_grounding import RealUItarsGrounding
from .uivenus2_grounding import UIVenus2Grounding
from .task_loader import load_task_config

__all__ = [
    "VMwareEnvironment", "WindowsEnvironment", "to_pyautogui",
    "RealUItarsGrounding", "UIVenus2Grounding", "load_task_config",
]
