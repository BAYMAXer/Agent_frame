"""任务完成验证器（程序侧确定性判断，可逐步扩展）。

设计：每个验证器是「触发关键词 → 判断函数」对。判断任务是否完成时，
根据任务描述匹配关键词，用对应验证器检查执行结果（execution.info）。

当前验证器：
- 下载验证（download）：download 成功 + verified（文件核实落盘）。
- 导航验证（navigation）：goto 执行成功（已导航到目标页面）。

后续可扩展：表单填写验证、搜索执行验证、文件编辑验证、页面出现某元素验证……
新增只需往 _VERIFIERS 里加一条 (关键词列表, 判断函数, 名称) 即可。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple


def _check_download(info: Dict[str, Any], task: str) -> bool:
    """下载验证器：download 成功 + verified（程序核实落盘）。"""
    return info.get("action") == "download" and bool(info.get("verified"))


def _check_navigation(info: Dict[str, Any], task: str) -> bool:
    """导航验证器：goto 执行成功即已导航到目标页面。"""
    return info.get("action") == "goto" and info.get("status") == "executed"


# 验证器注册表：[(触发关键词列表, 判断函数, 验证器名)]
_VERIFIERS = [
    (["下载", "download"], _check_download, "download"),
    (["打开", "访问", "导航", "进入", "goto", "open", "navigate"], _check_navigation, "navigation"),
]


def check_completion(task: str, info: Dict[str, Any]) -> str:
    """根据任务描述 + 执行结果，判断任务是否完成。

    返回命中的验证器名（str），未命中返回空字符串。
    """
    task_lower = (task or "").lower()
    for keywords, check, name in _VERIFIERS:
        if any(k.lower() in task_lower for k in keywords):
            if check(info, task):
                return name
    return ""
