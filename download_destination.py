"""Resolve download destinations from the original task, before planning."""
from __future__ import annotations

import ntpath
import re


_PATH_START = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:|\\\\)")
_CN_DESTINATION = re.compile(
    r"(?:下载(?!\s*(?:链接|历史|设置|目录|地址|入口))|保存|另存为|存放|存储)"
    r"(?:(?![，,。；;\n]|移动|复制|搬移|归档).){0,240}?"
    r"(?:到|至|于|在|路径为)\s*(?:本地(?:目录|文件夹)?|目录|文件夹|磁盘)?\s*[\"'`“‘]?\s*$", re.I)
_EN_DESTINATION = re.compile(
    r"\b(?:download(?:ing)?(?!\s+(?:link|page|button|history|settings)\b)|save|store)\b"
    r"(?:(?![,;\n]|\b(?:move|copy|archive)\b).){0,240}?"
    r"\b(?:to|into|in|under)\s+(?:(?:the\s+)?(?:folder|directory)\s+)?[\"'`“‘]?\s*$", re.I)
_NEGATED = re.compile(r"(?:已|已经|不|不要|不用|无需|不必|禁止|不需要)\s*(?:实际|进行)?$|"
                      r"\b(?:don't|do not|never|without|already)\s*$", re.I)
_OBSERVATION = re.compile(r"^(?:请\s*)?(?:核对|检查|查看|验证|确认|查找|找到|统计)|"
                          r"^(?:please\s+)?(?:check|verify|inspect|find|locate|confirm)\b", re.I)
_FOLLOWING_ACTION = re.compile(r"(?:后|然后|随后|并|再)|\b(?:and|then)\b", re.I)
_QUOTES = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’'}


def _read_directory(task, start):
    before = task[:start].rstrip()
    opener = before[-1:] if before[-1:] in _QUOTES else None
    tail = task[start:]
    if opener:
        end = tail.find(_QUOTES[opener])
        if end < 0:
            raise ValueError("下载目标目录的引号未闭合")
        value = tail[:end]
    else:
        # Spaces are part of Windows paths. Stop at an explicit clause boundary,
        # not the first space (which would turn 'D:\\paper files' into D:\\paper).
        value = re.split(r"[，。；;,\n]|\s+(?:and|then)\b|(?:然后|并且|且|并)(?=下载|移动|复制|打开|核对|检查|保存)",
                         tail, maxsplit=1, flags=re.I)[0].strip()
        value = re.sub(r"\s*(?:目录|文件夹)(?:下|中|里)?(?:\s.*)?$", "", value).strip()
        value = value.rstrip("。.)]）】").strip()
    if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("下载目标目录不是有效的绝对路径")
    if any(c in value for c in '<>"|?*') or ':' in value[2:]:
        raise ValueError("下载目标目录包含无效路径字符")
    drive, rest = ntpath.splitdrive(value)
    unc_root = bool(re.fullmatch(r"\\\\[^\\/]+[\\/][^\\/]+", drive)) and not rest
    if not drive or not (rest.startswith(("\\", "/")) or unc_root):
        raise ValueError("下载目标目录必须是绝对 Windows 路径")
    # A file path must not silently become a directory named paper.pdf.
    if ntpath.splitext(value)[1].lower() in {".pdf", ".zip", ".docx", ".xlsx", ".csv"}:
        raise ValueError("下载保存目录应填写文件夹路径；文件名请在任务中单独指定")
    return ntpath.normpath(value)


def task_download_directory(task):
    """Return the single explicit transfer destination; ignore sources and moves.

    Only the caller's original user task is accepted here. Web observations and
    planner prose cannot change the executor's destination.
    """
    if not isinstance(task, str):
        return None
    directories = {}
    for match in _PATH_START.finditer(task):
        prefix = task[max(0, match.start() - 300):match.start()]
        intent = _CN_DESTINATION.search(prefix) or _EN_DESTINATION.search(prefix)
        if intent is None or _NEGATED.search(prefix[:intent.start()]):
            continue
        lead = prefix[:intent.start()].strip()
        if _OBSERVATION.search(lead) and not _FOLLOWING_ACTION.search(lead):
            continue
        directory = _read_directory(task, match.start())
        directories[ntpath.normcase(directory).rstrip("\\")] = directory
    if len(directories) > 1:
        raise ValueError("任务指定了多个不同的下载目录，请将各目录的下载分别执行")
    return next(iter(directories.values()), None)


def resolve_download_destination(task, configured_dir=None):
    directory = task_download_directory(task)
    configured = configured_dir.strip() if isinstance(configured_dir, str) else None
    return {"configured_dir": configured or None,
            "directory": directory or configured or None,
            "source": "task" if directory else ("configuration" if configured else "default")}
