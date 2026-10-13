"""skill 的 handler 实现。每个 handler 签名：handler(args: dict, ctx) -> str。

ctx 是 SkillContext，文件查询只通过明确标记的 Windows 本机环境执行。
新增 skill 三步：config/skills.yaml 声明 + 这里写 handler + 登记进 SKILL_HANDLERS。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from .calculator import safe_calc


# --------------------------------------------------------------------------- #
# 在 Windows 本机环境执行的文件查询脚本
# --------------------------------------------------------------------------- #
# path 使用 repr 编码，通过本机 Python 执行，不经过 shell 拼接。
VERIFY_FILE_CMD = """import os
def check(p):
    if os.path.isfile(p):
        return "FILE exists: " + str(p) + " (" + str(os.path.getsize(p)) + " bytes)"
    if os.path.isdir(p):
        try:
            entries = sorted(os.listdir(p))
        except Exception as e:
            return "DIR exists: " + str(p) + " (listdir failed: " + str(e) + ")"
        if not entries:
            return "DIR exists: " + str(p) + " (empty)"
        return "DIR exists: " + str(p) + " | contents: " + ", ".join(entries)
    return "NOT FOUND: " + str(p)
print(check({path}))
"""

# --------------------------------------------------------------------------- #
# handler 实现
# --------------------------------------------------------------------------- #
def _handler_calculate(args: Dict[str, Any], ctx) -> str:
    return safe_calc(str(args.get("expression", "")))


def _handler_get_current_time(args: Dict[str, Any], ctx) -> str:
    """读取 Windows 本机时钟，不调用远端控制器。"""
    now = datetime.now()
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][now.weekday()]
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({weekday})"


def _handler_verify_file(args: Dict[str, Any], ctx) -> str:
    """通过已声明的 Windows 本机 Python 通道检查文件或目录。"""
    path = str(args.get("path", ""))
    if not path:
        return "verify_file 需要提供 path"
    env = getattr(ctx, "env", None) if ctx is not None else None
    if (getattr(env, "local_environment", False) is True
            and getattr(env, "os_name", "windows") == "windows"):
        runner = getattr(env, "run_python", None)
        if callable(runner):
            out = runner(VERIFY_FILE_CMD.format(path=repr(path)))
            if isinstance(out, str) and out.strip():
                return out.strip()
    return f"verify_file 无法执行：需要 Windows 本机环境的 run_python 通道。path={path}"


# name -> handler。新增 skill 除了在 config/skills.yaml 声明，还要在这里登记。
SKILL_HANDLERS: Dict[str, Any] = {
    "calculate": _handler_calculate,
    "get_current_time": _handler_get_current_time,
    "verify_file": _handler_verify_file,
}
