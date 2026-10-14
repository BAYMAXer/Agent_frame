"""Read-only, action-independent evidence for a downloaded file's rename.

The original user request supplies the destination name. A completed download
in the trusted plan supplies the source. Neither terminal output nor a model's
claim that a rename was sent is proof that the requested file now exists.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import re
import stat
import unicodedata
from typing import Any, Callable, Optional


_RENAME = re.compile(
    r"(?:重命名|改名)\s*(?:为|成|至)\s*|"
    r"\brename\b[^,;。；\n]{0,160}?\b(?:to|as)\s*", re.I)
_RENAME_GOAL = re.compile(r"重命名|改名|\brename\b", re.I)
_NEGATED = re.compile(r"(?:不要|不用|无需|不必|禁止|不需要|不)\s*$|"
                      r"\b(?:do\s+not|don't|never|without)\s*$", re.I)
_QUOTES = {'"': '"', "'": "'", '`': '`', '“': '”', '‘': '’', '「': '」', '『': '』'}
_DEVICE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_CLAUSE_END = re.compile(
    r"[，,。；;\n！？!?]|\s+(?:and|then)\b|"
    r"(?:然后|并且|并|且)(?=\s*(?:检查|核对|验证|确认|打开|查看|保存|下载|移动|复制|重命名))", re.I)
_OTHER_OUTCOME = re.compile(
    r"打开|阅读|分析|上传|发送|复制|移动|搬移|搬到|归档|删除|转换|编辑|打印|压缩|解压|"
    r"修改(?:文件)?内容|提取|总结|摘要|翻译|执行|运行|"
    r"\b(?:open|read|analy[sz]e|upload|send|copy|move|archive|delete|convert|edit|print|"
    r"compress|extract|summari[sz]e|translate|execute|run)\b", re.I)
_FILE_TOKEN = re.compile(r"[^\s/\\<>:\"'`“”‘’，,。；;!?！？]+\.[A-Za-z0-9]{1,12}(?![\w.])")
_ABSOLUTE_TOKEN = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`“”‘’，,。；;!?！？]+")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _kind(value: dict) -> str:
    check = value.get("completion_check")
    return _text(check.get("kind")) if isinstance(check, dict) else ""


def _valid_basename(name: str) -> bool:
    return bool(name and len(name) <= 255 and name not in {".", ".."}
                and name == name.strip() and not name.endswith(".")
                and not any(unicodedata.category(c) == "Cc" for c in name)
                and not any(c in name for c in '<>:"/\\|?*')
                and not _DEVICE.match(name))


def _path_identity(path: str) -> str:
    if ntpath.splitdrive(path)[0] or "\\" in path:
        return ntpath.normcase(ntpath.normpath(path))
    return os.path.normcase(os.path.normpath(path))


def _valid_source_path(path: str) -> bool:
    if (not path or len(path) > 4096
            or any(unicodedata.category(c) == "Cc" for c in path)
            or path.startswith(("\\\\?\\", "\\\\.\\"))):
        return False
    drive, tail = ntpath.splitdrive(path)
    if drive:
        if not ntpath.isabs(path) or not tail.startswith(("\\", "/")):
            return False
        if drive.startswith("\\\\") and not re.fullmatch(r"\\\\[^\\/]+[\\/][^\\/]+", drive):
            return False
        if drive.startswith("\\\\"):
            if any(not _valid_basename(part) for part in drive[2:].replace("/", "\\").split("\\")):
                return False
        elif not re.fullmatch(r"[A-Za-z]:", drive):
            return False
        parts = tail.replace("/", "\\").split("\\")
        if any(part and not _valid_basename(part) for part in parts):
            return False
    elif not os.path.isabs(path):
        return False
    else:
        # Native absolute paths also permit platform-independent tests. Names
        # still follow Windows rules because these tasks run on Windows.
        if any(part and not _valid_basename(part) for part in path.split("/")):
            return False
    return _valid_basename(ntpath.basename(path))


def _read_target(text: str, start: int) -> Optional[str]:
    tail = text[start:].lstrip()
    if not tail:
        return None
    opener = tail[0]
    if opener in _QUOTES:
        end = tail.find(_QUOTES[opener], 1)
        if end < 0:
            return None
        value = tail[1:end]
    else:
        value = _CLAUSE_END.split(tail, maxsplit=1)[0].strip()
        value = value.rstrip(".)]）】").strip()
    return value if _valid_basename(value) else None


def _rename_targets(text: str) -> Optional[list[str]]:
    targets = []
    for match in _RENAME.finditer(text):
        if _NEGATED.search(text[max(0, match.start() - 40):match.start()]):
            return None
        target = _read_target(text, match.end())
        if target is None:
            return None
        targets.append(target)
    return targets


def _with_extension(name: str, source: str) -> str:
    return name if ntpath.splitext(name)[1] else name + ntpath.splitext(source)[1]


def _explicit_other_source(task: str, source: str) -> bool:
    match = next(_RENAME.finditer(task), None)
    if match is None:
        return True
    if re.match(r"rename\b", match.group(), re.I):
        portion = re.sub(r"^rename\b|\b(?:to|as)\s*$", "", match.group(), flags=re.I)
    else:
        # Only the current rename clause identifies its source. A filename in
        # an earlier download clause must not hide a different rename source.
        portion = re.split(r"[，,。；;\n]", task[:match.start()])[-1]
    portion = re.sub(r"https?://[^\s<>\"'`]+", "", portion, flags=re.I)
    for opener, closer in _QUOTES.items():
        quoted = re.findall(re.escape(opener) + r"(.*?)" + re.escape(closer), portion)
        for path in quoted:
            if (_valid_source_path(path)
                    and _path_identity(path) != _path_identity(source)):
                return True
    for path in _ABSOLUTE_TOKEN.findall(portion):
        if (ntpath.splitext(path)[1] and _valid_source_path(path)
                and _path_identity(path) != _path_identity(source)):
            return True
    parent = ntpath.dirname(source) if ntpath.splitdrive(source)[0] or "\\" in source else os.path.dirname(source)
    for location in (r"桌面|\bdesktop\b", r"下载文件夹|\bdownloads(?:\s+folder)?\b"):
        if re.search(location, portion, re.I) and not re.search(location, parent, re.I):
            return True
    for known in (source, ntpath.basename(source)):
        # Preserve all other filename mentions. A single matching download
        # filename plus a different explicitly selected source is ambiguous.
        portion = re.sub(r"(?<![\w.-])" + re.escape(known) + r"(?![\w.-])",
                         " ", portion, flags=re.I)
    return _FILE_TOKEN.search(portion) is not None


def _only_rename_outcome(subtask: dict, source: str, target: str) -> bool:
    parent = ntpath.dirname(source) if ntpath.splitdrive(source)[0] or "\\" in source else os.path.dirname(source)
    allowed_paths = {_path_identity(source), _path_identity(target), _path_identity(parent)}
    for field in ("goal", "completion_criteria"):
        prose = _text(subtask.get(field))
        semantic_prose = prose
        for known in sorted({source, target, parent, ntpath.basename(source),
                             ntpath.basename(target)}, key=len, reverse=True):
            semantic_prose = semantic_prose.replace(known, " ")
        if _OTHER_OUTCOME.search(semantic_prose):
            return False
        for token in _ABSOLUTE_TOKEN.findall(prose):
            token = token.rstrip(").]）】")
            if _path_identity(token) not in allowed_paths:
                return False
    return True


def _stat_identity(value: os.stat_result) -> tuple:
    # Python 3.12 on Windows reports creation time for path lstat's ctime,
    # while an opened handle can report the NTFS change time after a rename.
    # Device/inode, size and modification time agree across both interfaces.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


class FileRenameVerifier:
    """Capture the source before actions; verify the destination after actions.

    ``descriptor`` is intent data only. ``baseline`` is populated solely from
    the real source file. An absent baseline can never be recovered by treating
    an existing destination as evidence of this task's rename.
    """

    def __init__(self, task: str, subtask: dict, plan: dict,
                 check_deadline: Optional[Callable[[], None]] = None):
        self.descriptor: dict = {}
        self.baseline: dict = {}
        self._check_deadline = check_deadline
        self._build_descriptor(task, subtask, plan)
        if self.descriptor:
            snapshot = self._snapshot(self.descriptor["source_path"])
            recorded_size = self.descriptor.pop("recorded_bytes", None)
            if snapshot and (recorded_size is None or recorded_size == snapshot["bytes"]):
                self.baseline = snapshot

    def _deadline(self) -> None:
        if self._check_deadline is not None:
            self._check_deadline()

    def _build_descriptor(self, task: str, subtask: dict, plan: dict) -> None:
        if (not isinstance(task, str) or not isinstance(subtask, dict) or not isinstance(plan, dict)
                or subtask.get("mode") != "desktop"
                or _kind(subtask) != "observation"
                or not _text(subtask.get("id")) or subtask.get("status") == "done"
                or not _RENAME_GOAL.search(_text(subtask.get("goal")))):
            return
        targets = _rename_targets(task)
        if not targets or len(targets) != 1:
            return
        source_steps = plan.get("steps")
        if not isinstance(source_steps, list):
            return
        downloads = {}
        found_current = False
        for step in source_steps:
            if not isinstance(step, dict):
                continue
            if step.get("id") == subtask.get("id"):
                found_current = True
                break
            if step.get("status") != "done":
                return
            evidence = step.get("download_evidence")
            if (step.get("status") != "done"
                    or _kind(step) != "download"
                    or not isinstance(evidence, dict)):
                continue
            path = _text(evidence.get("path"))
            if _valid_source_path(path):
                size = evidence.get("bytes")
                downloads[_path_identity(path)] = (path, size if type(size) is int and size > 0 else None)
        if not found_current or not downloads:
            return
        candidates = list(downloads.values())
        if len(candidates) > 1:
            # Only the original request can choose among multiple downloads.
            # A planner's guess of the source is not a trusted identity.
            candidates = [(path, size) for path, size in candidates
                          if path.casefold() in task.casefold()
                          or re.search(r"(?<![\w.-])" + re.escape(ntpath.basename(path))
                                       + r"(?![\w.-])", task, re.I)]
        if len(candidates) != 1:
            return
        source, recorded_size = candidates[0]
        if _explicit_other_source(task, source):
            return
        filename = _with_extension(targets[0], source)
        if not _valid_basename(filename):
            return
        planned = []
        for field in ("goal", "instructions"):
            declared = _rename_targets(_text(subtask.get(field)))
            if declared is None:
                return
            planned.extend(declared)
        if (not planned or any(_with_extension(name, source) != filename for name in planned)):
            return
        # Paths come from trusted completed download evidence; filenames come
        # from the original request. Never follow a model-proposed directory.
        parent = ntpath.dirname(source) if ntpath.splitdrive(source)[0] or "\\" in source else os.path.dirname(source)
        target = ntpath.join(parent, filename) if ntpath.splitdrive(source)[0] or "\\" in source else os.path.join(parent, filename)
        if not _only_rename_outcome(subtask, source, target):
            return
        self.descriptor = {"step_id": subtask.get("id"), "source_path": source,
                           "path": target, "filename": filename,
                           "recorded_bytes": recorded_size}

    def _snapshot(self, path: str) -> dict:
        self._deadline()
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                return {}
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _stat_identity(before) != _stat_identity(opened):
                    return {}
                while True:
                    self._deadline()
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            self._deadline()
            if (_stat_identity(before) != _stat_identity(after)
                    or _stat_identity(after) != _stat_identity(os.lstat(path))):
                return {}
            return {"bytes": after.st_size, "sha256": digest.hexdigest(),
                    "stat": _stat_identity(after)}
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return {}

    def _exact_target_name(self) -> bool:
        with os.scandir(os.path.dirname(self.descriptor["path"])) as entries:
            for entry in entries:
                self._deadline()
                if entry.name == self.descriptor["filename"]:
                    return True
        return False

    def _source_missing(self) -> bool:
        source, target = self.descriptor["source_path"], self.descriptor["path"]
        if _path_identity(source) == _path_identity(target):
            # Windows resolves a path without regard to case. Check only the
            # known parent directory to distinguish an actual case-only rename.
            if ntpath.basename(source) == self.descriptor["filename"]:
                return False
            parent = os.path.dirname(target)
            names = set()
            with os.scandir(parent) as entries:
                for entry in entries:
                    self._deadline()
                    if entry.name in {ntpath.basename(source), self.descriptor["filename"]}:
                        names.add(entry.name)
            return (self.descriptor["filename"] in names and ntpath.basename(source) not in names)
        try:
            os.lstat(source)
        except FileNotFoundError:
            return True
        return False

    def verify(self) -> dict:
        """Return deterministic evidence only after the full rename is observed."""
        if not self.descriptor or not self.baseline:
            return {}
        self._deadline()
        try:
            already_named = (ntpath.basename(self.descriptor["source_path"])
                             == self.descriptor["filename"]
                             and _path_identity(self.descriptor["source_path"])
                             == _path_identity(self.descriptor["path"]))
            if ((not already_named and not self._source_missing()) or not self._exact_target_name()):
                return {}
            target = self._snapshot(self.descriptor["path"])
            if (not target or target["bytes"] != self.baseline["bytes"]
                    or target["sha256"] != self.baseline["sha256"]
                    or (not already_named and not self._source_missing())):
                return {}
        except TimeoutError:
            raise
        except OSError:
            return {}
        path = self.descriptor["path"]
        message = (f"已核实下载文件已具有要求的文件名：{path}（{target['bytes']} 字节），文件内容一致"
                   if already_named else
                   f"已核实文件重命名：{path}（{target['bytes']} 字节），原文件名已消失且文件内容一致")
        return {"action": "file_rename", "status": "executed", "dispatched": False,
                "verified": True, "verification_source": "filesystem_rename",
                "step_id": self.descriptor["step_id"],
                "source_path": self.descriptor["source_path"], "path": path,
                "bytes": target["bytes"], "sha256": target["sha256"],
                "message": message}
