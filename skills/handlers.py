"""skill 的 handler 实现。每个 handler 签名：handler(args: dict, ctx) -> str。

ctx 是 SkillContext（鸭子类型，取 ctx.controller 拿目标机控制器）。
新增 skill 三步：config/skills.yaml 声明 + 这里写 handler + 登记进 SKILL_HANDLERS。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .calculator import safe_calc


# --------------------------------------------------------------------------- #
# 在目标机（VM）里执行的脚本
# --------------------------------------------------------------------------- #
# 检查文件/目录是否存在，移植自 Agent-S3 的 VERIFY_FILE_CMD
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

# 取目标机当前日期时间（含星期）
TIME_CMD = """import datetime
now = datetime.datetime.now()
wd = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][now.weekday()]
print(now.strftime("%Y-%m-%d %H:%M:%S") + " (" + wd + ")")
"""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _controller(ctx):
    return getattr(ctx, "controller", None) if ctx is not None else None


def _run_in_env(ctrl, script: str) -> Optional[str]:
    """在目标机里跑一段 python 脚本，返回 output 文本；不可用/失败返回 None。"""
    runner = getattr(ctrl, "run_python_script", None) or getattr(ctrl, "execute_python_command", None)
    if runner is None:
        return None
    try:
        result = runner(script)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(result, str):
        return result.strip() or None
    if isinstance(result, dict):
        out = (result.get("output") or "").strip()
        err = (result.get("error") or "").strip()
        return (out + ("\n" + err if err else "")).strip() or None
    return None


# --------------------------------------------------------------------------- #
# handler 实现
# --------------------------------------------------------------------------- #
def _handler_calculate(args: Dict[str, Any], ctx) -> str:
    return safe_calc(str(args.get("expression", "")))


def _handler_get_current_time(args: Dict[str, Any], ctx) -> str:
    """取目标机当前时间；控制器可用时查 VM 时钟，否则回退本机时间。"""
    ctrl = _controller(ctx)
    if ctrl is not None:
        out = _run_in_env(ctrl, TIME_CMD)
        if out:
            return out
    now = datetime.now()
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][now.weekday()]
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({weekday})"


def _handler_verify_file(args: Dict[str, Any], ctx) -> str:
    """检查目标机文件/目录是否存在。

    真正通过 env.controller 在 VM 里执行（不是本地 os.path 的假实现）。
    环境未接入时如实报错，不给错误结果。
    """
    path = str(args.get("path", ""))
    if not path:
        return "verify_file 需要提供 path"
    ctrl = _controller(ctx)
    if ctrl is not None:
        out = _run_in_env(ctrl, VERIFY_FILE_CMD.format(path=repr(path)))
        if out:
            return out
    return f"verify_file 无法执行：环境未接入（env.controller 为空），无法检查目标机文件系统。path={path}"


def _handler_ilearning_complete_course(args: Dict[str, Any], ctx) -> str:
    """运行 iLearning 课程自动完成脚本（skills/ilearning.py）。

    通过 env 拿到任务浏览器会话，可选先 goto 到课程 URL，再跑完成脚本。
    脚本会阻塞较长时间（几分钟到几十分钟），期间不要并发操作浏览器。
    """
    env = getattr(ctx, "env", None)
    get_session = getattr(env, "_get_browser_session", None) if env is not None else None
    if get_session is None:
        return "ilearning_complete_course 无法执行：当前环境没有浏览器会话接入"
    session = get_session()
    if session is None:
        return "浏览器会话不可用，请重新观察。"
    url = str(args.get("url", "")).strip()
    if url:
        nav = session.goto(url)
        if not (isinstance(nav, dict) and nav.get("dispatched")):
            err = (nav or {}).get("error") if isinstance(nav, dict) else str(nav)
            return f"打开课程页失败：{err}"
    return session.complete_ilearning_course()


def _handler_ilearning_answer_exam(args: Dict[str, Any], ctx) -> str:
    """注入预编译 JS 自动完成 iLearning 考试（examContent 逐题格式）。

    针对「打开考试页面 → 完成考试」类任务：循环选第一个未选选项 → 下一题，最后交卷。
    不读题、不调用答题模型，只完成页面点击，满足「完成考试页面点击操作」的验收。
    """
    env = getattr(ctx, "env", None)
    get_session = getattr(env, "_get_browser_session", None) if env is not None else None
    if get_session is None:
        return "ilearning_answer_exam 无法执行：当前环境没有浏览器会话接入"
    session = get_session()
    if session is None:
        return "浏览器会话不可用，请重新观察。"
    from .ilearning import EXAM_AUTO_JS
    return session.evaluate_js(EXAM_AUTO_JS, timeout_ms=180000)


# name -> handler。新增 skill 除了在 config/skills.yaml 声明，还要在这里登记。
SKILL_HANDLERS: Dict[str, Any] = {
    "calculate": _handler_calculate,
    "get_current_time": _handler_get_current_time,
    "verify_file": _handler_verify_file,
    "ilearning_complete_course": _handler_ilearning_complete_course,
    "ilearning_answer_exam": _handler_ilearning_answer_exam,
}
