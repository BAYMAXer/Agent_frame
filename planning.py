"""Normalize task plans and advance them using action-associated evidence.

The planner proposes subgoals; it cannot assert that they are already complete.
Desktop completion comes from the next observation of the action bound to that
subgoal. Navigation/download completion may also use deterministic execution
results. Neither path completes other subgoals merely because their text matches.
"""
from __future__ import annotations

from copy import deepcopy
import ntpath
import posixpath
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

MAX_PLAN_STEPS = 12
_STATUSES = {"pending", "in_progress", "done", "blocked"}
_NEGATIVE_EXECUTION = {"rejected", "uncertain", "failed", "failure", "error", "timeout"}
_DISPATCH_ONLY = {
    "success", "executed", "已执行", "动作已发送", "命令已发送", "执行成功",
    "动作已执行", "命令已执行", "命令已执行，输出将在下一步返回",
    "command executed", "action dispatched", "action executed",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _completion_check(value: Any) -> Dict[str, str]:
    """Keep declarative checks only; never retain executable planner content."""
    if not isinstance(value, dict):
        return {"kind": "observation"}
    kind = _text(value.get("kind")).lower()
    if kind not in {"observation", "navigation", "download"}:
        return {"kind": "observation"}
    check = {"kind": kind}
    if kind in {"navigation", "download"}:
        url = _text(value.get("url"))
        if url and _http_url(url):
            check["url"] = url
    if kind == "download":
        filename = _text(value.get("filename"))
        if filename and "/" not in filename and "\\" not in filename:
            check["filename"] = filename
    return check


def normalize_plan(raw: Any, task: str = "", *, deduplicate: bool = True) -> Dict[str, Any]:
    """Create consecutive, stable IDs and a safe, bounded initial plan.

    Planner indices are ignored; goals retain their list order and get fresh
    idx/id. Only identical goal/criteria/check entries are deduplicated. If any
    step is unrecoverable, fall back to the full task instead of a partial plan.
    A plan produced here can be normalized again without losing its progress.
    When no valid subgoal survives, retain the task itself as one observable goal.
    """
    obj = raw if isinstance(raw, dict) else {}
    source_steps = obj.get("steps") if isinstance(obj.get("steps"), list) else []
    canonical = obj.get("schema_version") == 1 and all(
        isinstance(step, dict) and step.get("id") == f"p{pos}"
        and step.get("idx") == pos
        for pos, step in enumerate(source_steps, 1)
    )
    steps = []
    seen_steps = set()
    incomplete = False
    for source in source_steps:
        if not isinstance(source, dict):
            incomplete = True
            continue
        goal = _text(source.get("goal"))
        if not goal:
            incomplete = True
            continue
        criteria = _text(source.get("completion_criteria")) or goal
        check = _completion_check(source.get("completion_check"))
        step_key = (" ".join(goal.split()).casefold(), " ".join(criteria.split()).casefold(),
                    tuple(sorted(check.items())), source.get("mode"),
                    _text(source.get("instructions")), _text(source.get("target_url")))
        if deduplicate and step_key in seen_steps:
            continue
        seen_steps.add(step_key)
        number = len(steps) + 1
        status = source.get("status", "pending") if canonical else "pending"
        step = {
            "id": f"p{number}", "idx": number, "goal": goal,
            "status": status if status in _STATUSES else "pending",
            "completion_criteria": criteria,
            "completion_check": check,
            "mode": source.get("mode") if source.get("mode") in {"browser", "desktop"}
                    else ("browser" if check["kind"] in {"navigation", "download"} else "desktop"),
            "instructions": _text(source.get("instructions")) or goal,
        }
        target_url = _text(source.get("target_url"))
        if target_url and _http_url(target_url):
            step["target_url"] = target_url
        if canonical and isinstance(source.get("result"), str):
            step["result"] = source["result"]
        if canonical:
            for key in ("evidence", "blocked_reason", "update_source"):
                if _text(source.get(key)):
                    step[key] = _text(source[key])
            if type(source.get("updated_at_step")) is int:
                step["updated_at_step"] = source["updated_at_step"]
            download = _download_evidence(source.get("download_evidence"))
            if download:
                step["download_evidence"] = download
        steps.append(step)
        if len(steps) > MAX_PLAN_STEPS:
            # Reject an oversized decomposition rather than silently dropping
            # its final subgoals (which often contain the actual deliverable).
            steps = []
            break
    if incomplete and _text(task):
        steps = []
    if not steps and _text(task):
        steps = [{"id": "p1", "idx": 1, "goal": _text(task), "status": "pending",
                  "completion_criteria": _text(task), "completion_check": {"kind": "observation"}}]
    keywords = obj.get("keywords") if isinstance(obj.get("keywords"), list) else []
    plan = {
        "schema_version": 1, "task": _text(task) or _text(obj.get("task")),
        "steps": steps, "keywords": list(dict.fromkeys(_text(k) for k in keywords if _text(k))),
        "final_check": _text(obj.get("final_check")) or _text(task),
        "current_step_id": None,
        "constraints": [_text(c) for c in obj.get("constraints", []) if _text(c)]
                       if isinstance(obj.get("constraints"), list) else [],
    }
    _select_current(plan)
    return plan


def validate_executable_plan(raw: Any) -> Optional[str]:
    """Strict browser-first plans must cover the task before any action starts."""
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        return "计划必须包含 steps 数组"
    if not 1 <= len(raw["steps"]) <= 8:
        return "计划需要 1～8 个完整子任务"
    for index, step in enumerate(raw["steps"], 1):
        if not isinstance(step, dict):
            return f"子任务 {index} 必须是对象"
        if step.get("mode") not in {"browser", "desktop"}:
            return f"子任务 {index} 的 mode 必须是 browser 或 desktop"
        if any(not _text(step.get(key)) for key in ("goal", "instructions", "completion_criteria")):
            return f"子任务 {index} 缺少 goal、instructions 或 completion_criteria"
        if step.get("target_url") and not _http_url(_text(step["target_url"])):
            return f"子任务 {index} 的 target_url 必须是完整 HTTP(S) 网址"
    return None


def record_subtask_completion(plan, step_id, evidence, result, event_step=0):
    """Commit only the active subtask after the caller has verified observation evidence."""
    if step_id != get_current_step_id(plan) or not _observed_evidence(evidence):
        return False
    step = next((s for s in plan["steps"] if s["id"] == step_id), None)
    if step is None or step.get("completion_check", {}).get("kind") != "observation":
        return False
    _record(step, "done", evidence, "subtask_verification", event_step)
    step["result"] = _text(result)
    _select_current(plan)
    return True


def get_current_step_id(plan: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the first unfinished subgoal, including a currently blocked one."""
    if not isinstance(plan, dict):
        return None
    return next((step.get("id") for step in plan.get("steps", [])
                 if isinstance(step, dict) and step.get("status") != "done"), None)


def _select_current(plan: Dict[str, Any]) -> None:
    current = get_current_step_id(plan)
    plan["current_step_id"] = current
    for step in plan.get("steps", []):
        if step.get("id") == current and step.get("status") == "pending":
            step["status"] = "in_progress"
        elif step.get("id") != current and step.get("status") == "in_progress":
            step["status"] = "pending"


def _http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _url_matches(expected: str, actual: str) -> bool:
    if not _http_url(actual):
        return False
    if not expected:
        return True
    try:
        wanted, got = urlsplit(expected), urlsplit(actual)
        if (wanted.hostname or "").lower() != (got.hostname or "").lower():
            return False
        if wanted.port != got.port:
            return False
        wanted_path = wanted.path.rstrip("/")
        if wanted_path and wanted_path != got.path.rstrip("/"):
            return False
        query = parse_qs(got.query)
        return all(query.get(key) == values for key, values in parse_qs(wanted.query).items())
    except ValueError:
        return False


def _execution_allows_completion(info: Dict[str, Any]) -> bool:
    return not (
        _text(info.get("status")).lower() in _NEGATIVE_EXECUTION
        or bool(info.get("error")) or info.get("dispatched") is False
        or info.get("verified") is False
    )


def _observed_evidence(value: Any) -> str:
    evidence = _text(value)
    if evidence.rstrip("。.!！ ").lower() in _DISPATCH_ONLY:
        return ""
    return evidence


def _download_evidence(value: Any) -> Dict[str, Any]:
    """A declarative file identity; never retain arbitrary execution fields."""
    if not isinstance(value, dict) or not _text(value.get("path")):
        return {}
    evidence = {"path": _text(value["path"]), "url": _text(value.get("url")),
                "source": _text(value.get("source")) or "download"}
    size = value.get("bytes")
    if type(size) is int and size > 0:
        evidence["bytes"] = size
    return evidence


def _url_identity(url: str) -> str:
    if not _http_url(url):
        return ""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if port and port != {"http": 80, "https": 443}.get(parsed.scheme.lower()):
            host += f":{port}"
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/",
                           urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), ""))
    except ValueError:
        return ""


def _path_identity(path: str) -> str:
    if not path:
        return ""
    if "\\" in path or ntpath.splitdrive(path)[0]:
        return ntpath.normcase(ntpath.normpath(path))
    return posixpath.normpath(path)


def _download_not_reused(plan: Dict[str, Any], current: str, info: Dict[str, Any]) -> bool:
    path, url = _path_identity(_text(info.get("path"))), _url_identity(_text(info.get("url")))
    for other in plan.get("steps", []):
        if other.get("id") == current or other.get("status") != "done":
            continue
        used = other.get("download_evidence")
        if not isinstance(used, dict):
            continue
        used_path, used_url = _path_identity(_text(used.get("path"))), _url_identity(_text(used.get("url")))
        if (path and path == used_path) or (url and url == used_url):
            return False
    return True


def _record_download(step: Dict[str, Any], info: Dict[str, Any]) -> None:
    evidence = _download_evidence({"path": info.get("path"), "url": info.get("url"),
                                   "bytes": info.get("bytes"),
                                   "source": "cached_file" if info.get("status") == "already_downloaded" else "download"})
    if evidence:
        step["download_evidence"] = evidence


def _verified_cached_download(info: Dict[str, Any]) -> bool:
    size = info.get("bytes")
    return (info.get("status") == "already_downloaded" and info.get("action") == "download"
            and info.get("dispatched") is False and info.get("verified") is True
            and info.get("verification_source") == "cached_file"
            and type(size) is int and size > 0 and bool(_text(info.get("path")))
            and _http_url(_text(info.get("url"))) and not info.get("error"))


def _download_matches_check(step: Dict[str, Any], info: Dict[str, Any]) -> bool:
    check = step.get("completion_check", {})
    path = _text(info.get("path"))
    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
    return (bool(path) and (not check.get("filename") or check["filename"] == filename)
            and (not check.get("url") or _url_matches(check["url"], _text(info.get("url")))))


def _previous_download_can_complete(plan: Dict[str, Any], step: Dict[str, Any], info: Dict[str, Any]) -> bool:
    # Visual downloads (e.g. a native Save dialog) may still use observed evidence.
    # When the execution does contain file evidence, a model claim cannot bypass
    # its declared identity or reuse a file already assigned to another subgoal.
    if step.get("completion_check", {}).get("kind") != "download" or info.get("action") != "download":
        return True
    return (info.get("verified") is True and _download_matches_check(step, info)
            and _download_not_reused(plan, step["id"], info))


def _record(step: Dict[str, Any], status: str, evidence: str, source: str, event_step: int) -> None:
    step.update(status=status, evidence=evidence, update_source=source, updated_at_step=event_step)
    if status != "blocked":
        step.pop("blocked_reason", None)


def update_plan_progress(
    plan: Optional[Dict[str, Any]], *, decision_update: Optional[Dict[str, Any]] = None,
    previous_action_step_id: Optional[str] = None, previous_action_status: str = "",
    previous_observed_effect: str = "", previous_execution_info: Optional[Dict[str, Any]] = None,
    execution_action: str = "", execution_info: Optional[Dict[str, Any]] = None,
    action_plan_step_id: Optional[str] = None, event_step: int = 0,
) -> bool:
    """Apply one decision/execute phase to a plan in place and report changes.

    Call with decision fields before binding the current action to a subgoal;
    call again with execution fields afterwards. Completion can advance at most
    one bound subgoal per phase. Unknown IDs and attempts to skip ahead are ignored.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return False
    before = deepcopy(plan)
    _select_current(plan)
    steps = {step.get("id"): step for step in plan["steps"] if isinstance(step, dict)}
    patch = decision_update if isinstance(decision_update, dict) else {}
    update = patch.get("plan_update") if isinstance(patch.get("plan_update"), dict) else {}
    previous_info = previous_execution_info if isinstance(previous_execution_info, dict) else {}
    current = get_current_step_id(plan)

    # Recovery is explicit; a reported block remains visible until retried.
    if update.get("current_step_id") == current and current in steps:
        step = steps[current]
        if step.get("status") == "blocked":
            _record(step, "in_progress", "重新尝试当前子目标", "decision_recovery", event_step)

    if (previous_action_step_id == current and current in steps
            and _text(previous_action_status).lower() == "success"
            and _execution_allows_completion(previous_info)
            and _previous_download_can_complete(plan, steps[current], previous_info)):
        completed = update.get("completed_steps")
        entries = completed if isinstance(completed, list) else []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("step_id") != current:
                continue
            evidence = _observed_evidence(entry.get("evidence")) or _observed_evidence(previous_observed_effect)
            if evidence:
                _record(steps[current], "done", evidence, "observation", event_step)
                if (steps[current].get("completion_check", {}).get("kind") == "download"
                        and previous_info.get("action") == "download"):
                    _record_download(steps[current], previous_info)
                break
        # Legacy completed_add must exactly name the bound subgoal.
        legacy = patch.get("completed_add")
        if (steps[current].get("status") != "done" and isinstance(legacy, list)
                and steps[current].get("goal") in [_text(item) for item in legacy]):
            evidence = _observed_evidence(previous_observed_effect)
            if evidence:
                _record(steps[current], "done", evidence, "observation_legacy", event_step)
                if (steps[current].get("completion_check", {}).get("kind") == "download"
                        and previous_info.get("action") == "download"):
                    _record_download(steps[current], previous_info)

    _select_current(plan)
    current = get_current_step_id(plan)
    blocked = update.get("blocked_steps")
    for entry in blocked if isinstance(blocked, list) else []:
        if not isinstance(entry, dict) or entry.get("step_id") != current or current not in steps:
            continue
        reason = _text(entry.get("reason"))
        if reason:
            _record(steps[current], "blocked", reason, "decision_blocked", event_step)
            steps[current]["blocked_reason"] = reason
            break

    info = execution_info if isinstance(execution_info, dict) else {}
    cached_download = _verified_cached_download(info)
    if (action_plan_step_id == current and current in steps
            and ((info.get("status") == "executed" and _execution_allows_completion(info)) or cached_download)):
        check = steps[current].get("completion_check", {})
        action = _text(info.get("action")) or _text(execution_action)
        url = _text(info.get("url"))
        if (action == "goto" and check.get("kind") == "navigation"
                and _url_matches(_text(check.get("url")), url)):
            _record(steps[current], "done", f"已导航至 {url}", "navigation", event_step)
        elif (action == "download" and check.get("kind") == "download"
              and info.get("verified") is True and _download_not_reused(plan, current, info)):
            path = _text(info.get("path"))
            if _download_matches_check(steps[current], info):
                _record(steps[current], "done", f"已核实文件落盘：{path}", "download", event_step)
                _record_download(steps[current], info)
    _select_current(plan)
    return plan != before
