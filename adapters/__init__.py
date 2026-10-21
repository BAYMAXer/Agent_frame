"""适配层：把抽象框架接到真实的 OSWorld VMware 虚拟机 + 真实 UI-TARS / UI-Venus-2 grounding。

不改动框架任何现有文件，只在这里提供：
- VMwareEnvironment：实现 Environment 接口，包装 OSWorld 的 DesktopEnv；
- RealUItarsGrounding：实现 Grounding 接口，真实请求 UI-TARS 端点；
- UIVenus2Grounding：实现 Grounding 接口，真实请求 UI-Venus-2 端点（[0,1000] 归一化坐标）；
- task_loader：从 evaluation_examples 加载 task_config；
- run_vm：入口，组装环境 + grounding + agent 跑真实任务。
"""
from __future__ import annotations

from importlib import import_module

# Importing the remote environment on a headless A must not load local GUI or
# VM adapters. Preserve the package's public imports through lazy attributes.
_EXPORTS = {
    'VMwareEnvironment': ('vmware_env', 'VMwareEnvironment'),
    'to_pyautogui': ('vmware_env', 'to_pyautogui'),
    'WindowsEnvironment': ('windows_env', 'WindowsEnvironment'),
    'RemoteBrowserEnvironment': ('remote_browser_env', 'RemoteBrowserEnvironment'),
    'RealUItarsGrounding': ('uitars_grounding', 'RealUItarsGrounding'),
    'UIVenus2Grounding': ('uivenus2_grounding', 'UIVenus2Grounding'),
    'load_task_config': ('task_loader', 'load_task_config'),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module('.' + module, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    "VMwareEnvironment", "WindowsEnvironment", "to_pyautogui",
    "RealUItarsGrounding", "UIVenus2Grounding", "load_task_config", "RemoteBrowserEnvironment",
]
