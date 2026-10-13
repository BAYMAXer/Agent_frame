"""Windows 本机环境与远程 UI-TARS / UI-Venus-2 定位模型适配器。"""
from __future__ import annotations

from .windows_input import to_pyautogui
from .windows_env import WindowsEnvironment
from .uitars_grounding import RealUItarsGrounding
from .uivenus2_grounding import UIVenus2Grounding

__all__ = [
    "WindowsEnvironment", "to_pyautogui", "RealUItarsGrounding", "UIVenus2Grounding",
]
