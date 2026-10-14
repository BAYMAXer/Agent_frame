"""Normalize task plans and advance them using action-associated evidence.

The planner proposes subgoals; it cannot assert that they are already complete.
Desktop completion comes from the next observation of the action bound to that
subgoal. Navigation/download completion may also use deterministic execution
results. Neither path completes other subgoals merely because their text matches.
"""
from __future__ import annotations

from copy import deepcopy
import json
import ntpath
import posixpath
import re
import unicodedata
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


def _directory_error(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return "下载目录必须是非空字符串"
    if len(value) > 4096:
        return "下载目录不能超过 4096 个字符"
    if any(unicodedata.category(char) == "Cc" for char in value):
        return "下载目录不能包含控制字符"
    directory = value.strip().replace("/", "\\")
    drive, _ = ntpath.splitdrive(directory)
    if not drive or not ntpath.isabs(directory):
        return "下载目录必须是完整的 Windows 绝对路径"
    if drive.startswith("\\\\") and len([part for part in drive[2:].split("\\") if part]) < 2:
        return "下载目录必须包含 UNC 主机与共享名称"
    return None


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
        if "directory" in value and _directory_error(value["directory"]) is None:
            check["directory"] = _path_identity(value["directory"].strip())
    return check


def normalize_plan(raw: Any, task: str = "", *, deduplicate: bool = True,
                   download_dir: Optional[str] = None) -> Dict[str, Any]:
    """Create consecutive, stable IDs and a safe, bounded initial plan.

    Planner indices are ignored; goals retain their list order and get fresh
    idx/id. Only identical goal/criteria/check entries are deduplicated. If any
    step is unrecoverable, fall back to the full task instead of a partial plan.
    A plan produced here can be normalized again without losing its progress.
    When no valid subgoal survives, retain the task itself as one observable goal.
    """
    if download_dir is not None:
        directory_error = _directory_error(download_dir)
        if directory_error:
            raise ValueError(directory_error)
    destination = _path_identity(download_dir.strip()) if download_dir is not None else ""
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
        if check["kind"] == "download" and destination:
            check["directory"] = destination
        step_key = (" ".join(goal.split()).casefold(), " ".join(criteria.split()).casefold(),
                    tuple(sorted(check.items())), source.get("mode"),
                    _text(source.get("instructions")), _text(source.get("target_url")),
                    tuple(_text(item) for item in source.get("keywords", []) if isinstance(item, str))
                    if isinstance(source.get("keywords", []), list) else ())
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
        if isinstance(source.get("keywords"), list):
            keywords = list(dict.fromkeys(_text(item) for item in source["keywords"]
                                          if isinstance(item, str) and _text(item)))
            if keywords:
                step["keywords"] = keywords[:16]
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
            file_evidence = source.get("file_evidence")
            if (isinstance(file_evidence, dict) and source.get("status") == "done"
                    and file_evidence.get("verification_source") == "filesystem_rename"):
                step["file_evidence"] = {key: file_evidence[key] for key in
                    ("source_path", "path", "bytes", "sha256", "verification_source") if key in file_evidence}
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


_DOWNLOAD_LINK_NOUN = re.compile(
    r"下载\s*(?:链接|按钮|入口|地址|源|列表|详情页|页面|页|中心|站点|历史|记录|次数|目录|设置|统计|速度|状态|进度|结果)|"
    r"\bdownload\s+(?:link|button|url|source|page|list|history|record|count|directory|folder|settings|statistics|speed|status|progress|results?)\b", re.I)
_DOWNLOADED_ARTIFACT = re.compile(r"已(?:经)?下载|下载(?:的|后)", re.I)
_DOWNLOAD_ACTION = re.compile(r"下载|\bdownload(?:ing)?\b", re.I)
_NEGATED_DOWNLOAD = re.compile(
    r"(?:无需|不用|不要|不必|禁止|不需要|不)\s*(?:进行|实际|立即)?下载|"
    r"\b(?:without|do\s+not|don't|no\s+need\s+to|never)\s+download(?:ing)?\b", re.I)
_LINK_DISCOVERY = re.compile(
    r"(?:查找|找到|搜索|识别|确认|定位|获取|提取).*(?:链接|入口|地址)|"
    r"\b(?:find|locate|identify|confirm|inspect|collect|extract|obtain)\b.*"
    r"\b(?:link|url)\b", re.I)
_OBSERVATION_GOAL = re.compile(
    r"^(?:请\s*)?(?:确认|检查|核对|验证|查看|查找|找到|搜索|识别|定位|获取|提取)|"
    r"^(?:please\s+)?(?:find|locate|identify|confirm|inspect|check|verify|search)\b", re.I)
_FOLLOWING_TRANSFER = re.compile(
    r"(?:并|然后|之后|后|再)[^。；;\n]{0,24}(?:下载|保存|另存为)|"
    r"\b(?:and|then)\b[^。；;\n]{0,24}\b(?:download|save|store)\b", re.I)
_SAVED_FILE = re.compile(
    r"(?:PDF|文件).{0,25}(?:落盘|已保存|保存成功|保存到|存在于)|"
    r"论文.{0,25}(?:落盘|已保存|保存成功|保存到)|"
    r"(?:落盘|保存到)(?:本地|磁盘|目录)|"
    r"已保存到.{0,8}(?:目录|文件夹)|"
    r"(?:PDF|文件|论文)(?:(?!\b(?:and|but)\b|并且|而且|且)[^。；;，,\n]){0,96}"
    r"(?:已(?:经)?(?:成功)?下载(?:完成|完毕|到|至)|已(?:经)?(?:成功)?(?:保存|存储)|已(?:在|于)[^。；;，,\n]{0,32}(?:保存|存储)|"
    r"(?:下载|保存)(?:完成|完毕))|"
    r"\b(?:PDF|file|paper)\b.{0,25}\b(?:saved|stored)\b|"
    r"\b(?:PDF|file)\b.{0,25}\bexists\b|"
    r"\b(?:PDF|file|paper)\b(?:(?!\b(?:and|but)\b)[^。；;，,\n]){0,96}\bdownloaded\b|"
    r"\b(?:saved|stored)\b.{0,25}\b(?:locally|disk|directory|folder)\b", re.I)
_SAVE_ACTION = re.compile(r"保存|另存为|\b(?:save|store)\b", re.I)
_DOWNLOAD_INTERMEDIATE = re.compile(
    r"(?:已发起|发起|启动|开始).{0,8}下载|下载.{0,8}(?:已发起|开始|启动)|"
    r"保存对话框|PDF(?!\s*(?:下载\s*)?(?:链接|入口|地址)).{0,12}(?:查看|预览)|"
    r"PDF(?!\s*(?:下载\s*)?(?:链接|入口|地址)).{0,12}(?:已打开|已显示)|"
    r"\bdownload\b.{0,15}\b(?:started|initiated|requested)\b|"
    r"\b(?:start|initiate|request)\b.{0,15}\bdownload\b|"
    r"\b(?:PDF\s+(?:viewer|preview)|save\s+dialog)\b|"
    r"\bPDF\b(?!\s+(?:download\s+)?(?:link|url)\b).{0,15}\b(?:open|opened|visible|loaded)\b|"
    r"\b(?:navigate[sd]?|navigation)\b.{0,25}\bPDF\b", re.I)
_CONFIRMED_LINK = re.compile(
    r"(?:链接|入口|地址).{0,30}(?:可见|存在|已找到|已确认|已定位|已显示|匹配)|"
    r"(?:已找到|已确认|已定位|已显示).{0,30}(?:链接|入口|地址)|"
    r"\b(?:link|url)\b.{0,30}\b(?:visible|available|exists|identified|confirmed|found)\b|"
    r"\b(?:identified|confirmed|found)\b.{0,30}\b(?:link|url)\b", re.I)
_PDF_PREVIEW_ACTION = re.compile(
    r"(?:打开|查看|阅读|浏览|预览).{0,24}PDF(?!\s*(?:下载\s*)?(?:链接|入口|地址|详情页|页面|页|中心|站点|历史|目录|设置))|"
    r"\b(?:open|view|read|preview|inspect)\b.{0,30}\bPDF\b(?!\s+(?:download\s+)?(?:link|url|page|history|settings)\b)", re.I)
_RESULT_NEGATION = re.compile(
    r"不能|不可|不应|不需(?:要)?|无需|不必|不用|不要|未(?:曾|能)?|没有|并非|不是|禁止|不足以|"
    r"不(?:仅)?以|不(?=下载|打开|查看|预览|保存|发起|启动|作为|视为|算作|代表|构成|足以)|"
    r"\b(?:not|never|without|no|don't|doesn't|cannot|can't|needn't)\b", re.I)
_POSITIVE_FILE_PROPERTY = re.compile(
    r"\bnot\s+(?:empty|zero)\b|\b(?:no|without)\s+errors?\b|没有错误|并非空文件|不是空文件", re.I)
_REJECTED_RESULT_SUFFIX = re.compile(
    r"^\s*(?:页(?:面)?|器)?\s*(?:已出现|已打开|已显示|出现|的出现)?\s*(?:不(?:能|应|足以)?|并非|不是).{0,12}(?:完成|判据|依据|落盘)|"
    r"^\s*(?:(?:is|are|does)\s+)?(?:not|never|cannot|can't|doesn't)\b.{0,24}\b(?:complete|completion|sufficient|saved)\b", re.I)
_EXPLICIT_INTERMEDIATE = re.compile(
    r"(?:已发起|发起|启动|开始).{0,8}下载|下载.{0,8}(?:已发起|开始|启动)|保存对话框|"
    r"PDF\s*(?:查看(?:器|页(?:面)?)?|预览(?:器|页(?:面)?)?|阅读器)|"
    r"\b(?:download\b.{0,15}\b(?:started|initiated|requested)|"
    r"(?:start|initiate|request)\b.{0,15}\bdownload|"
    r"PDF\s+(?:viewer|preview)|save\s+dialog)\b", re.I)


def _semantic_clauses(text: str):
    # A PDF mention in one sentence does not turn "inspect the title" in the
    # next into a viewer result. Contrast clauses also reset negation scope.
    text = re.sub(r"https?://[^\s<>\"'`，。；;,]+", "", text, flags=re.I)
    return re.split(r"[。！？!?；;，,\n]|(?<!\d)\.(?!\d)|但是|然而|而是|但|\b(?:but|however)\b", text, flags=re.I)


def _positive_matches(pattern, text: str):
    for clause in _semantic_clauses(text):
        for match in pattern.finditer(clause):
            context = clause[:match.end()]
            if pattern is _SAVED_FILE:
                context = _POSITIVE_FILE_PROPERTY.sub("", context)
                prefix = clause[:match.start()]
                negatives = list(_RESULT_NEGATION.finditer(prefix))
                if negatives and re.search(r"\band\b|并且|而且|且", prefix[negatives[-1].end():], re.I):
                    # "X is not ..., and the PDF has been saved" supplies a
                    # separate affirmative file predicate with its own subject.
                    context = _POSITIVE_FILE_PROPERTY.sub("", match.group())
            if (_RESULT_NEGATION.search(context)
                    or _REJECTED_RESULT_SUFFIX.search(clause[match.end():match.end() + 40])):
                continue
            yield clause, match


def _has_saved_file_result(text: str) -> bool:
    return next(_positive_matches(_SAVED_FILE, text), None) is not None


def _has_intermediate_result(text: str) -> bool:
    for clause, match in _positive_matches(_DOWNLOAD_INTERMEDIATE, text):
        # "PDF 对应的链接可见" describes a link even when PDF and link are not
        # adjacent. A real viewer/download/save-dialog assertion still counts.
        if (next(_positive_matches(_CONFIRMED_LINK, clause), None) is not None
                and not _EXPLICIT_INTERMEDIATE.search(match.group())):
            continue
        return True
    return False


def _has_preview_intent(text: str) -> bool:
    return next(_positive_matches(_PDF_PREVIEW_ACTION, text), None) is not None


def _has_transfer_intent(text: str) -> bool:
    cleaned = _transfer_text(text)
    return (next(_positive_matches(_DOWNLOAD_ACTION, cleaned), None) is not None
            or next(_positive_matches(_SAVE_ACTION, cleaned), None) is not None)


def _transfer_text(text: str) -> str:
    text = re.sub(r"https?://[^\s<>\"'`，。；;,]+", "", text, flags=re.I)
    return _DOWNLOAD_LINK_NOUN.sub("", _DOWNLOADED_ARTIFACT.sub("", _NEGATED_DOWNLOAD.sub("", text)))


def _download_action_text(text: str) -> bool:
    # "find a download link" describes evidence discovery, not a file transfer.
    cleaned = _transfer_text(text)
    return bool(_DOWNLOAD_ACTION.search(cleaned)
                and (not _OBSERVATION_GOAL.search(cleaned) or _FOLLOWING_TRANSFER.search(cleaned)))


def _requires_download_check(step: Dict[str, Any]) -> bool:
    goal = _text(step.get("goal"))
    criteria = _text(step.get("completion_criteria"))
    # An explicit file result plus a download check resolves compound goals such
    # as "find its link and save the paper" without relying on one connector.
    if (_completion_check(step.get("completion_check"))["kind"] == "download"
            and _has_saved_file_result(criteria)):
        return True
    cleaned_goal = _transfer_text(goal)
    observing = bool(_OBSERVATION_GOAL.search(cleaned_goal) and not _FOLLOWING_TRANSFER.search(cleaned_goal))
    return bool(_download_action_text(goal)
            or (not observing and step.get("mode") == "browser"
                and next(_positive_matches(_SAVE_ACTION, cleaned_goal), None) is not None
                and re.search(r"PDF|文件|论文|\b(?:file|paper)\b", goal, re.I))
            or bool(not observing and step.get("mode") == "browser" and _has_saved_file_result(criteria)
                    and not _LINK_DISCOVERY.search(goal)))


def prepare_executable_plan(raw: Any, task: str = ""):
    """Correct only unmistakable PDF link checks in a new planner proposal.

    Keep identity, source conditions and all prose verbatim. File saving and
    ambiguous checks remain subject to strict validation and planner retries.
    """
    proposal = deepcopy(raw)
    corrections = []
    if not isinstance(proposal, dict) or not isinstance(proposal.get("steps"), list):
        return proposal, corrections
    for index, step in enumerate(proposal["steps"], 1):
        if not isinstance(step, dict):
            continue
        check = step.get("completion_check")
        if not isinstance(check, dict) or _text(check.get("kind")).lower() not in {"navigation", "download"}:
            continue
        goal, criteria = _text(step.get("goal")), _text(step.get("completion_criteria"))
        pdf_link_goal = bool(_LINK_DISCOVERY.search(goal) and (
            re.search(r"PDF", goal, re.I)
            or (re.search(r"论文|\bpaper\b", goal, re.I) and re.search(r"下载|\bdownload\b", goal, re.I))))
        prose = "\n".join((goal, _text(step.get("instructions")), criteria))
        if (not pdf_link_goal or not _CONFIRMED_LINK.search(criteria)
                or _has_saved_file_result(criteria) or _has_intermediate_result(prose)
                or _has_preview_intent(prose) or _has_transfer_intent(prose)):
            continue
        url, filename = _text(check.get("url")), _text(check.get("filename"))
        # A user-supplied identity must still reach the active subtask verifier.
        # If it exists only in the old check, ask the planner to preserve it in
        # its observation goal rather than silently deleting that condition.
        if task and ((url and _known_download_url(url, task) and url.casefold() not in prose.casefold())
                     or (filename and filename.casefold() in task.casefold()
                         and filename.casefold() not in prose.casefold())):
            continue
        previous_kind = _text(check.get("kind")).lower()
        step["completion_check"] = {"kind": "observation"}
        corrections.append(f"子任务 {index}：纯 PDF 链接确认的 completion_check.kind 从 {previous_kind} 改为 observation；保留目标与来源条件，移除仅用于导航/文件落盘的检查字段")
    return proposal, corrections


def _known_download_url(url: str, task: str) -> bool:
    """Initial constraints may use task URLs or an explicitly named source host.

    A known host alone does not prove a PDF path, identifier or query string.
    Later observed identities belong in execution evidence, not guessed plans.
    """
    candidates = re.findall(r"https?://(?:(?!\]\()[^\s<>\"'`，。；）])+", task, re.I)
    known_urls = []
    for candidate in candidates:
        candidate = candidate.rstrip(",;.")
        # DOI/publisher paths can contain paired parentheses. Strip only extra
        # closing delimiters from prose or a Markdown link around the URL.
        while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        while candidate.endswith("]") and candidate.count("]") > candidate.count("["):
            candidate = candidate[:-1]
        known_urls.append(candidate)
    if any(_url_matches(url, candidate) for candidate in known_urls):
        return True
    parsed = urlsplit(url)
    if parsed.path not in {"", "/"} or parsed.query:
        return False
    host = parsed.hostname or ""
    return bool(host and re.search(r"(?<![\w.-])" + re.escape(host) + r"(?![\w.-])", task, re.I))


def _task_declares_directory(task: str, directory: str) -> bool:
    # This is a literal declaration check, not an interpretation of plan prose.
    # The caller supplies parsed/configured destinations through download_dir.
    wanted = _path_identity(directory)
    normalized_task = task.replace("/", "\\").casefold()
    suffix = r"\\?" if not wanted.endswith("\\") else ""
    return bool(re.search(re.escape(wanted) + suffix + r"(?![A-Za-z0-9_.\\-])", normalized_task))


def validate_executable_plan(raw: Any, task: str = "", download_dir: Optional[str] = None) -> Optional[str]:
    """Strict browser-first plans must cover the task before any action starts."""
    if download_dir is not None:
        directory_error = _directory_error(download_dir)
        if directory_error:
            return directory_error
    destination = _path_identity(download_dir.strip()) if download_dir is not None else ""
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
        raw_check = step.get("completion_check")
        if isinstance(raw_check, dict) and "directory" in raw_check:
            directory_error = _directory_error(raw_check["directory"])
            if directory_error:
                return f"子任务 {index}：{directory_error}"
            if _text(raw_check.get("kind")).lower() != "download":
                return f"子任务 {index} 的 directory 仅用于 kind=download 的文件落盘检查"
            directory = _path_identity(raw_check["directory"].strip())
            if destination and directory != destination:
                return f"子任务 {index} 的下载目录与可信目标目录不一致；使用已配置的真实下载目录，不能自行指定其他位置"
            if not destination and not _task_declares_directory(task, directory):
                return f"子任务 {index} 的下载目录未由用户任务或配置提供；不能猜测 completion_check.directory"
        check = _completion_check(raw_check)
        goal = _text(step.get("goal"))
        criteria = _text(step.get("completion_criteria"))
        saving = _requires_download_check(step)
        preview = _has_preview_intent("\n".join((goal, _text(step.get("instructions")))))
        discovery = bool(_LINK_DISCOVERY.search(goal)
                         and re.search(r"PDF|下载|\bdownload\b", goal, re.I) and not saving and not preview)
        if saving and check["kind"] != "download":
            return f"子任务 {index} 要下载或保存文件，completion_check 必须为 kind=download，以已验证的目标文件落盘为完成判据"
        if discovery and check["kind"] != "observation":
            return f"子任务 {index} 只确认或获取链接，应使用 kind=observation；打开 PDF 页面与下载文件必须分别定义完成结果"
        if ((saving or check["kind"] == "download" or discovery)
                and _has_intermediate_result(criteria) and not _has_saved_file_result(criteria)):
            return f"子任务 {index} 不能以下载已发起、保存对话框或 PDF 查看页作为文件下载/链接确认的完成判据"
        if check["kind"] == "download" and not _has_saved_file_result(criteria):
            return f"子任务 {index} 的下载判据缺少正向文件落盘结果；明确目标文件已保存并由程序验证，否定预览或发起下载不能代替该结果"
        # Validation runs before any browser observations exist. Do not bind the
        # eventual artifact to a familiar but unobserved provider or PDF ID.
        if task and check["kind"] in {"download", "navigation"}:
            url = check.get("url", "")
            artifact_check = check["kind"] == "download" or bool(
                re.search(r"PDF", goal, re.I) and (preview or not _DOWNLOAD_LINK_NOUN.search(goal)))
            if url and artifact_check and not _known_download_url(url, task):
                return f"子任务 {index} 的 PDF/下载地址未由用户任务提供；删除猜测的 completion_check.url，保留已知论文身份及来源限制，执行时使用页面实际链接"
            filename = check.get("filename", "")
            if filename and filename.casefold() not in task.casefold():
                return f"子任务 {index} 的下载文件名未由用户任务提供；删除猜测的 filename，执行后使用已验证文件的真实名称"
    if task and _download_action_text(task) and not any(
            _completion_check(step.get("completion_check"))["kind"] == "download" for step in raw["steps"]):
        return "用户要求下载文件，计划必须包含 kind=download 的文件落盘子任务，不能只搜索链接或打开 PDF 页面"
    return None


def build_plan_repair_feedback(raw: Any, error: str, task: str = "", download_dir: Optional[str] = None) -> str:
    """Give the planner a concrete result template without inventing identities.

    This produces feedback data only. It neither modifies nor accepts a plan;
    the regenerated full proposal must still pass normal validation.
    """
    rules = [
        "重新输出完整的 1～8 步计划 JSON，不执行操作，不只输出修补片段。保留其它正确子任务。",
        "保留用户明确的论文身份、来源、数量、合法 URL、文件名和目录要求；不要猜作者、PDF ID、下载源或文件名。",
        "纯链接确认使用 observation，以目标条目对应的链接可见且身份/来源匹配为结果；下载/保存文件使用 download，以程序验证目标文件真实落盘为结果。",
        "真正打开/阅读 PDF 可以使用 navigation，但不能代表文件已下载；用户要求下载时必须另外保留文件落盘步骤。",
        "正向写出当前子任务的完成结果；下载已发起、保存对话框出现和 PDF 预览不是落盘结果，否定这些状态也不能代替正向结果。",
        "未知 completion_check.url/filename 应删除猜测，执行时根据页面实际链接与文件证据；用户已给定的合法值必须保留在适合的检查或 goal/instructions 中。",
    ]
    payload = {"validation_error": _text(error)[:1000], "repair_rules": rules}
    match = re.search(r"子任务\s+(\d+)", _text(error))
    source_steps = raw.get("steps") if isinstance(raw, dict) else None
    index = int(match.group(1)) if match else None
    step = (source_steps[index - 1] if index and isinstance(source_steps, list)
            and 0 < index <= len(source_steps) and isinstance(source_steps[index - 1], dict) else None)
    if step is not None:
        goal, instructions = _text(step.get("goal")), _text(step.get("instructions"))
        check = step.get("completion_check") if isinstance(step.get("completion_check"), dict) else {}
        payload["current_step_data"] = {"index": index, "goal": goal[:2000], "instructions": instructions[:2000],
                                        "completion_criteria": _text(step.get("completion_criteria"))[:2000],
                                        "completion_check": {key: (value[:4096] if isinstance(value, str)
                                                                     else {"invalid_type": type(value).__name__})
                                                             for key, value in check.items()
                                                             if key in {"kind", "url", "filename", "directory"}}}
        prose = "\n".join((goal, instructions))
        preview = _has_preview_intent(prose)
        saving = _has_transfer_intent(prose) or _requires_download_check(step)
        link = bool(_LINK_DISCOVERY.search(goal) and not saving and not preview)
        if saving or (_text(check.get("kind")).lower() == "download" and not link and not preview):
            template = {"completion_criteria": "本子任务所对应的目标文件已在指定目录落盘，并由程序核实真实路径、非零大小和文件格式", "completion_check": {"kind": "download"}}
        elif preview:
            preview_url = _text(check.get("url"))
            known_preview_url = bool(preview_url and _http_url(preview_url) and _known_download_url(preview_url, task))
            template = {"completion_criteria": "本子任务指定的 PDF 正文已在浏览器查看页面打开，且目标身份与来源符合用户要求", "completion_check": {"kind": "navigation" if known_preview_url else "observation"}}
            if not known_preview_url:
                rules.append("未给定可核验的 PDF 目标 URL 时，用 observation 核验真实正文、身份和来源；不要用缺少 url 的 navigation 把任意跳转当作 PDF 已打开。后续从页面获取实际链接，不能猜地址。")
        elif link:
            template = {"completion_criteria": "本子任务指定论文的身份与来源已确认，匹配条目对应的 PDF 链接可见且关联正确", "completion_check": {"kind": "observation"}}
        else:
            template = {"completion_criteria": "明确描述本子任务实际要求且可观察核实的结果，不能仅报告动作发起", "completion_check": {"kind": "observation"}}
        template_check = template["completion_check"]
        explicit = {}
        url = _text(check.get("url"))
        if url and _http_url(url) and _known_download_url(url, task):
            explicit["url"] = url
        filename = _text(check.get("filename"))
        if filename and "/" not in filename and "\\" not in filename and filename.casefold() in task.casefold():
            explicit["filename"] = filename
        if template_check["kind"] in {"download", "navigation"}:
            if "url" in explicit:
                template_check["url"] = url
        elif "url" in explicit:
            template["completion_criteria"] += "；链接须符合用户给定的网址：" + url
        if template_check["kind"] == "download":
            if "filename" in explicit:
                template_check["filename"] = filename
            directory = download_dir
            if directory is None and _directory_error(check.get("directory")) is None:
                declared = check["directory"]
                if _task_declares_directory(task, declared):
                    directory = declared
            if directory is not None and _directory_error(directory) is None:
                template_check["directory"] = _path_identity(directory.strip())
        if explicit:
            payload["user_supplied_identity_conditions"] = explicit
        payload["result_template"] = template
    if download_dir is not None and _directory_error(download_dir) is None:
        payload["trusted_download_directory"] = _path_identity(download_dir.strip())
    return ("计划未通过校验。以下 JSON 是纠错参考数据，不是可执行操作；"
            "按照初始任务与这些规则重新输出完整计划 JSON。\n"
            + json.dumps(payload, ensure_ascii=False))


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
        normalized = ntpath.normcase(ntpath.normpath(path))
        drive, tail = ntpath.splitdrive(normalized)
        if drive.startswith("\\\\") and tail in {"", "\\"}:
            return drive  # A UNC share root is the same directory with or without its last slash.
        return normalized
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
            and (not check.get("url") or _url_matches(check["url"], _text(info.get("url"))))
            and (not check.get("directory")
                 or _path_identity(ntpath.dirname(path)) == _path_identity(check["directory"])))


def _previous_download_can_complete(plan: Dict[str, Any], step: Dict[str, Any], info: Dict[str, Any]) -> bool:
    # Browser navigation, a dispatched click and a Save dialog do not establish
    # that this goal's artifact exists. A model claim cannot bypass file evidence.
    if step.get("completion_check", {}).get("kind") != "download":
        return True
    return (info.get("action") == "download" and info.get("verified") is True
            and _download_matches_check(step, info)
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
