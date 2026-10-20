"""Deterministic completion contracts backed by fresh page or local-file evidence.

Explicit contracts must come from trusted task configuration, never model output.
Only complete single navigation/download instructions with an explicit HTTP(S)
URL, or an unambiguous single-title paper download, are inferred automatically.
Other tasks remain unknown without a contract. Private checks never enter the
public specification or public failure report.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import os
import re
import stat
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .task_specs import infer_paper_spec
from .paper_resolver import arxiv_id_from_url, is_verified_binding, query_matches_title


def _url(value):
    if not isinstance(value, str) or not value or re.search(r"\s|[\\\x00-\x1f\x7f]", value):
        raise ValueError("url must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if parsed.scheme.lower() not in ("http", "https") or not host or parsed.username or parsed.password:
            raise ValueError
        host = host.lower()
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        else:
            host = host.encode("idna").decode("ascii")
            label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            if len(host) > 253 or not all(re.fullmatch(label, part) for part in host.rstrip(".").split(".")):
                raise ValueError
        if port is not None and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, parsed.fragment))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("url must be an absolute HTTP(S) URL without credentials") from exc


def _string(value, field, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{field} must be a {'possibly empty ' if empty else 'nonempty '}string")
    return value


def _normalize_spec(spec):
    if not isinstance(spec, dict) or set(spec) != {"checks"} or not isinstance(spec["checks"], list):
        raise ValueError("completion spec must contain only a checks list")
    allowed = {
        "url_equals": {"url"},
        "element_state": {"url", "role", "name", "element_id", "value", "states"},
        "text_present": {"url", "text"},
        "unresolved_requirement": {"text"},
        "download": {"url", "filename", "sha256", "min_bytes", "bind_to"},
        "paper_download": {"title", "query", "filename", "count", "format", "author", "year", "doi", "arxiv_id", "url", "min_bytes", "sha256", "bind_to", "workspace"},
    }
    checks, ids = [], set()
    for index, raw in enumerate(spec["checks"]):
        if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw["kind"] not in allowed:
            raise ValueError("unknown or missing completion check kind")
        kind = raw["kind"]
        if set(raw) - ({"id", "kind", "scope"} | allowed[kind]):
            raise ValueError(f"unsupported fields for {kind}")
        check = copy.deepcopy(raw)
        check["id"] = _string(check.get("id", f"check_{index + 1}"), "id")
        if check["id"] in ids:
            raise ValueError("completion check ids must be unique")
        ids.add(check["id"])
        check["scope"] = check.get("scope", "final")
        if check["scope"] not in ("final", "milestone"):
            raise ValueError("scope must be final or milestone")
        if "url" in check:
            check["url"] = _url(check["url"])
        elif kind == "url_equals" or (kind == "download" and "bind_to" not in check):
            raise ValueError(f"{kind} requires url")
        if kind == "element_state":
            for field in ("role", "name"):
                _string(check.get(field), field)
            if "element_id" in check:
                _string(check["element_id"], "element_id")
            if "value" in check:
                _string(check["value"], "value", empty=True)
            if "states" in check:
                states = check["states"]
                supported = {"checked", "selected", "expanded", "disabled", "focused", "required", "readonly"}
                if (not isinstance(states, dict) or not states or set(states) - supported
                        or any(type(v) is not bool and not (k == "checked" and v == "mixed")
                               for k, v in states.items())):
                    raise ValueError("states must contain supported boolean AX states (checked may be mixed)")
            if "value" not in check and "states" not in check:
                raise ValueError("element_state requires value or states")
        elif kind in ("text_present", "unresolved_requirement"):
            _string(check.get("text"), "text")
        elif kind in ("download", "paper_download"):
            if "bind_to" in check:
                _string(check["bind_to"], "bind_to")
            check["min_bytes"] = check.get("min_bytes", 1)
            if type(check["min_bytes"]) is not int or check["min_bytes"] < 1:
                raise ValueError("min_bytes must be a positive integer")
            if "filename" in check:
                name = _string(check["filename"], "filename")
                if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
                    raise ValueError("filename must be a basename")
            if "sha256" in check:
                if not isinstance(check["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", check["sha256"]):
                    raise ValueError("sha256 must contain 64 hexadecimal characters")
                check["sha256"] = check["sha256"].lower()
            if kind == "paper_download":
                if "workspace" in check and check["workspace"] != "default":
                    raise ValueError("paper workspace must be the selected default folder")
                if "title" not in check and "query" not in check:
                    raise ValueError("paper_download requires title or query")
                for field in ("title", "query"):
                    if field in check:
                        check[field] = _string(check[field], field).strip()
                        if len(check[field]) > 500 or not any(c.isalnum() for c in check[field]):
                            raise ValueError("paper title/query must contain letters or numbers and be at most 500 characters")
                check["count"] = check.get("count", 1)
                if type(check["count"]) is not int or not 1 <= check["count"] <= 20:
                    raise ValueError("paper count must be an integer between 1 and 20")
                check["format"] = check.get("format", "pdf")
                if check["format"] != "pdf":
                    raise ValueError("paper_download currently supports only pdf")
                for field in ("author", "doi", "arxiv_id"):
                    if field in check:
                        check[field] = _string(check[field], field).strip()
                if "year" in check and (type(check["year"]) is not int or not 1500 <= check["year"] <= 2999):
                    raise ValueError("paper year must be an integer between 1500 and 2999")
                if "doi" in check:
                    check["doi"] = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", check["doi"], flags=re.I).lower()
                    if not re.fullmatch(r"10\.\d{4,9}/\S+", check["doi"]):
                        raise ValueError("doi must be a valid DOI identifier")
                if "arxiv_id" in check:
                    check["arxiv_id"] = re.sub(r"^arxiv:\s*", "", check["arxiv_id"], flags=re.I)
                    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", check["arxiv_id"], re.I):
                        raise ValueError("arxiv_id must be an arXiv identifier")
        checks.append(check)
    return {"checks": checks}


def _infer_spec(task):
    paper = infer_paper_spec(task)
    if paper:
        return _normalize_spec(paper)
    # Full-string matching deliberately rejects additional goals and implicit URLs.
    target = r"(?P<url>https?://[A-Za-z0-9:/?#\[\]@!$&'()*+,;=._%~\-]+)"
    patterns = (
        ("url_equals", rf"(?:请\s*)?(?:打开|访问|进入|导航到)\s*{target}[。！]?"),
        ("url_equals", rf"(?:please\s+)?(?:open|visit|navigate\s+to|go\s+to|goto)\s+{target}"),
        ("download", rf"(?:请\s*)?下载\s*{target}[。！]?"),
        ("download", rf"(?:please\s+)?download\s+{target}"),
    )
    for kind, pattern in patterns:
        match = re.fullmatch(pattern, task.strip(), re.IGNORECASE)
        if match:
            try:
                return _normalize_spec({"checks": [{"kind": kind, "url": match.group("url")}]})
            except ValueError:
                pass
    return {"checks": []}


def _read_artifact(path):
    """Read real bytes on every evaluation; do not trust executor verified/bytes."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return {"status": "failed", "reason": "download path is not a regular file"}
        with open(path, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return {"status": "failed", "reason": "download path is not a regular file"}
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        current = os.stat(path)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if signature(before) != signature(after) or signature(after) != signature(current):
            return {"status": "unknown", "reason": "download changed while being verified"}
        return {"status": "passed", "reason": "local file read successfully", "bytes": after.st_size,
                "sha256": digest.hexdigest(), "identity": (after.st_dev, after.st_ino)}
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError):
        return {"status": "failed", "reason": "downloaded file does not exist as a regular file"}
    except OSError:
        return {"status": "unknown", "reason": "downloaded file cannot be read"}


def _title_key(text):
    # Preserve punctuation and word boundaries: "C++" is not "C", and
    # "A BC" is not "AB C". Only Unicode, casing and whitespace are normalized.
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _title_key_compact(text):
    # 去掉所有空白，容忍 PDF 文本提取时词间空格丢失（如 UNIFYINGSTATIC3DANDDYNAMIC）。
    # 只作为精确匹配失败后的兜底，不替代上面的词边界安全版。
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _parse_pdf(path, evidence):
    """Parse downloaded bytes, never filenames or executor/model identity claims."""
    if evidence.get("bytes", 0) > 100 * 1024 * 1024:
        return {"status": "unknown", "reason": "PDF exceeds the bounded parser size limit"}
    try:
        with open(path, "rb") as handle:
            if not handle.read(8).startswith(b"%PDF-"):
                return {"status": "failed", "reason": "downloaded bytes are not a PDF document"}
        try:
            from pypdf import PdfReader
        except ImportError:
            return {"status": "unknown", "reason": "PDF verification requires the pypdf dependency"}
        with open(path, "rb") as handle:
            reader = PdfReader(handle, strict=True)
            if reader.is_encrypted:
                return {"status": "unknown", "reason": "encrypted PDF cannot be verified without access"}
            page_count = len(reader.pages)
            if page_count < 1:
                return {"status": "failed", "reason": "PDF contains no pages"}
            page = reader.pages[0]
            margin_arxiv_ids = []
            left, bottom, right, top = map(float, page.mediabox)
            def visit(text, cm, tm, _font, _size):
                # arXiv stamps are often rotated in the outer page margin,
                # extracted after the abstract rather than before the title.
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                vertical = abs(tm[0] * cm[1] + tm[1] * cm[3]) > abs(tm[0] * cm[0] + tm[1] * cm[2])
                at_margin = x <= left + (right - left) * .12 or x >= right - (right - left) * .12
                stamp = re.fullmatch(r'arXiv:\s*((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)'
                    r'\s+\[[A-Za-z.\-]+\]\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}', ' '.join(text.split()), re.I)
                if vertical and at_margin and bottom <= y <= top and stamp:
                    margin_arxiv_ids.append(stamp.group(1))
            first_text = page.extract_text(visitor_text=visit) or ""
        # Parsing can take time: confirm it inspected the same stable artifact.
        after = _read_artifact(path)
        if (after.get("status") != "passed" or after.get("sha256") != evidence["sha256"]
                or after.get("identity") != evidence["identity"]):
            return {"status": "unknown", "reason": "PDF changed while being parsed"}
        return {"status": "passed", "pages": page_count, "first_text": first_text[:50000],
                "margin_arxiv_ids": sorted(set(margin_arxiv_ids))}
    except Exception:
        return {"status": "failed", "reason": "downloaded PDF is malformed or cannot be parsed"}


def _paper_identity(check, parsed):
    if parsed["status"] != "passed":
        return {"status": parsed["status"], "reason": parsed["reason"]}
    lines = [line.strip() for line in parsed["first_text"].splitlines() if line.strip()]
    if not lines:
        return {"status": "unknown", "reason": "PDF has no extractable first-page text for paper identity"}
    expected = _title_key(check["title"])
    expected_compact = _title_key_compact(check["title"])
    heading = None
    # Exact, complete heading lines near the start of the actual PDF. A title
    # mentioned in its abstract/references, metadata, or filename is insufficient.
    for start in range(min(8, len(lines))):
        if any(re.match(r"^(?:abstract|摘要|references|参考文献)\b", line, re.I) for line in lines[:start + 1]):
            break
        joined = ""
        for end in range(start, min(start + 6, len(lines))):
            joined = (joined + " " + lines[end]).strip()
            key = _title_key(joined)
            key_compact = _title_key_compact(joined)
            if key == expected or key_compact == expected_compact:
                heading = {"title": " ".join(lines[start:end + 1]), "line": start + 1}
                break
            if len(key_compact) > len(expected_compact):
                break
        if heading:
            break
    if not heading:
        return {"status": "unknown", "reason": "requested title is not an exact PDF first-page heading"}
    # Optional identity qualifiers must be in the paper's first-page heading
    # area, not in a bibliography or a discussion of somebody else's paper.
    header = []
    for line in lines[:20]:
        if re.match(r"^(?:abstract|摘要|references|参考文献)\b", line, re.I):
            break
        header.append(line)
    text = unicodedata.normalize("NFKC", "\n".join(header))
    if "author" in check:
        # Author names need word boundaries; 'Ann' must not match 'Joanne'.
        author = r"\s+".join(re.escape(p) for p in unicodedata.normalize("NFKC", check["author"]).split())
        if not re.search(r"(?<!\w)" + author + r"(?!\w)", text, re.I):
            return {"status": "unknown", "reason": "requested author is not established by PDF first-page text"}
    if "year" in check and not re.search(r"(?<!\d)" + str(check["year"]) + r"(?!\d)", text):
        return {"status": "unknown", "reason": "requested publication year is not established by PDF text"}
    for field in ("doi", "arxiv_id"):
        if field not in check:
            continue
        identifier = check[field]
        suffix = r"(?:v\d+)?" if field == "arxiv_id" and not re.search(r"v\d+$", identifier) else ""
        if field == 'arxiv_id':
            stamp_ids = parsed.get('margin_arxiv_ids', [])
            same = lambda value: bool(re.fullmatch(re.escape(identifier) + suffix, value, re.I))
            if stamp_ids and any(not same(value) for value in stamp_ids):
                return {'status': 'failed', 'reason': 'PDF arXiv stamp contradicts the required paper identity'}
            if any(same(value) for value in stamp_ids):
                continue
            if check.get('_verified_arxiv_id') == identifier:
                # Official metadata and the actual successful PDF transport
                # jointly establish the ID; the actual PDF title is still
                # required above. This flag is never accepted in task specs.
                continue
        if not re.search(r"(?<![\w./])" + re.escape(identifier) + suffix + r"(?![\w/])", text, re.I):
            return {"status": "unknown", "reason": f"requested {field} is not established by PDF text"}
    return {"status": "passed", "reason": "completed PDF has the requested first-page paper identity",
            "matched_title": heading["title"], "title_line": heading["line"], "pages": parsed["pages"]}


class CompletionVerifier:
    """Evaluate a trusted conjunctive contract against current observations.

    Page checks use browser_ax blocks. ``text_present`` matches a complete AX
    name/text after whitespace normalization. Element role/name must identify
    exactly one node; optional element_id disambiguates duplicate names.
    Page milestones latch; final page checks refresh on every observe(), and
    downloads always recheck the file even when their scope is milestone.
    Paper downloads require a completed artifact and an exact first-page PDF
    heading, plus any explicitly requested identity qualifiers. They never trust
    a model's title claim, PDF metadata alone, or a filename.
    """

    def __init__(self, task, spec=None, private_spec=None):
        if not isinstance(task, str):
            raise ValueError("task must be a string")
        self._public_spec = _normalize_spec(spec) if spec is not None else _infer_spec(task)
        if any("bind_to" in c for c in self._public_spec["checks"]):
            raise ValueError("bind_to is available only for private artifact checks")
        self._spec = copy.deepcopy(self._public_spec)
        self._public_ids = {c["id"] for c in self._public_spec["checks"]}
        public_by_id = {c["id"]: c for c in self._public_spec["checks"]}
        private = _normalize_spec(private_spec) if private_spec is not None else {"checks": []}
        ids = set(self._public_ids)
        for index, check in enumerate(private["checks"]):
            if "bind_to" in check:
                target = public_by_id.get(check["bind_to"])
                if target is None or target["kind"] not in ("download", "paper_download"):
                    raise ValueError("bind_to must identify an existing public artifact check")
                expected_count = target.get("count", 1)
                raw = private_spec["checks"][index]
                if "count" in raw and raw["count"] != expected_count:
                    raise ValueError("bound paper count must equal the public goal count")
                if check["kind"] == "paper_download":
                    check["count"] = expected_count
            # Private IDs are internal too: even a descriptive ID may leak an answer.
            cid = f"private_check_{index + 1}"
            while cid in ids:
                cid = "_" + cid
            check["id"] = cid
            ids.add(cid)
            self._spec["checks"].append(check)
        self.source = "explicit" if spec is not None or private_spec is not None else ("task" if self._spec["checks"] else "none")
        self._pages = []
        self._milestones = {}
        self._artifacts = {}
        self._artifact_ids = {}
        self._retired_artifact_ids = set()
        self._bindings = {}
        self._inferred = spec is None
        self._legacy_download_sequence = 0
        self._workspace_dir = None

    @property
    def bindings(self):
        return {cid: binding.to_dict() for cid, binding in self._bindings.items()}

    def paper_goals(self):
        goals = []
        for check in self._public_spec['checks']:
            if check['kind'] == 'paper_download':
                goals.append({**copy.deepcopy(check), 'check_id': check['id'],
                    'query': check.get('query') or check.get('title'),
                    'status': 'resolved' if check['id'] in self._bindings else 'pending',
                    'binding': self._bindings[check['id']].to_dict() if check['id'] in self._bindings else None})
        return goals

    def download_progress(self):
        """Public download milestones: same artifact integrity/identity, no final name.

        Never relax the stored contract or private evaluator. Model observations
        and image reviewers cannot manufacture this report.
        """
        checks = [{k: copy.deepcopy(v) for k, v in c.items() if k != 'filename'}
                  for c in self._public_spec['checks'] if c['kind'] in {'download', 'paper_download'}]
        return list(self._download_checks(checks).values())

    def download_facts(self):
        """Recheck durable browser-download receipts, independently of task goals.

        Closing a popup, observing another page or replanning cannot erase a
        completed transfer. Conversely, a receipt is never a latched success:
        every call rereads its file and compares it with the original browser
        receipt. These facts establish transport/integrity only, not that the
        downloaded content satisfies the user's whole task.
        """
        facts = []
        for path, record in self._artifacts.items():
            if not record.get('browser_receipt'):
                continue
            current, initial = _read_artifact(path), record['initial']
            status, reason = current['status'], current['reason']
            if record.get('migration_error'):
                status, reason = 'failed', record['migration_error']
            elif status == 'passed':
                if initial['status'] != 'passed':
                    status, reason = 'failed', 'file was not verified at the successful download event'
                elif (current['identity'], current['bytes'], current['sha256']) != (
                        initial['identity'], initial['bytes'], initial['sha256']):
                    status, reason = 'failed', 'file has changed since the successful download event'
                elif current['bytes'] <= 0:
                    status, reason = 'failed', 'downloaded file is empty'
                else:
                    reason = 'browser download completed; current local file matches its original receipt'
            fact = {'artifact_id': record['artifact_id'], 'path': path,
                'version': record.get('version', 1), 'transport': 'browser',
                'completed': True, 'urls': sorted(record['urls']),
                'source_urls': sorted(record['source_urls']),
                'bytes': current.get('bytes'), 'sha256': current.get('sha256'),
                'file_identity': list(current['identity']) if current.get('identity') else None,
                'status': status, 'reason': reason}
            if status == 'passed':
                from .artifact_download import inspect_download_format
                fact.update(inspect_download_format(path))
            facts.append(fact)
        return facts

    @staticmethod
    def _download_format(path):
        """Identify common saved-byte containers without trusting a filename."""
        from .artifact_download import inspect_download_format
        return inspect_download_format(path).get('format')

    def download_transfers(self, consumed=()):
        """Transport facts only; content, identity and final names are separate.

        A wrong file can complete a transfer, never the following validation.
        Only episode-registered, unchanged actual files count; no model claims.
        """
        valid = []
        for path, record in self._artifacts.items():
            ident = record.get('artifact_id')
            if not ident or ident in consumed or record.get('migration_error'):
                continue
            now, initial = _read_artifact(path), record['initial']
            if (now['status'] != 'passed' or initial['status'] != 'passed'
                    or (now['identity'], now['sha256']) != (initial['identity'], initial['sha256'])):
                continue
            valid.append((record, {'path': path, 'artifact_id': ident, 'bytes': now['bytes'], 'sha256': now['sha256']}))
        reports = []
        for check in self._public_spec['checks']:
            if check['kind'] not in {'download', 'paper_download'}:
                continue
            binding = self._bindings.get(check['id'])
            ordered = sorted(valid, key=lambda pair: not (self._artifact_matches_binding(pair[0], binding)
                if binding else check.get('url') in pair[0]['urls']))
            unique = {}
            for record, artifact in ordered:
                # A duplicated paper cannot satisfy a multiple-paper count.
                key = artifact['sha256'] if check['kind'] == 'paper_download' else artifact['artifact_id']
                unique.setdefault(key, artifact)
            selected = list(unique.values())[:check.get('count', 1)]
            complete = len(selected) == check.get('count', 1)
            reports.append(self._result(check, 'passed' if complete else 'unknown',
                '文件传输已完成；接下来单独核对内容、身份和文件名' if complete else '等待新的完整下载产物',
                'artifact_transport', artifacts=selected if complete else []))
        return reports

    def pending_paper_goals(self):
        return [goal for goal in self.paper_goals() if goal['status'] == 'pending']

    def bind_paper(self, check_id, binding):
        """Monotonically refine one public query using adapter-issued evidence."""
        if not is_verified_binding(binding):
            raise ValueError('Paper binding must originate from the official metadata adapter')
        check = next((c for c in self._public_spec['checks'] if c['id'] == check_id), None)
        if not check or check['kind'] != 'paper_download':
            raise ValueError('Unknown public paper goal')
        query = check.get('query') or check.get('title')
        if _title_key(binding.query) != _title_key(query):
            raise ValueError('Paper selection refers to a different original user query')
        if (binding.selection.get('method') != 'model_semantic'
                or binding.selection.get('status') != 'matched'
                or binding.selection.get('selected_id') != binding.canonical_id
                or binding.selection.get('metadata_hash') != binding.evidence_hash):
            raise ValueError('Paper identity requires model selection tied to official metadata')
        # An explicit machine-supplied exact-title contract is still a real
        # constraint. Natural-language queries are selected semantically.
        if 'title' in check and not self._inferred and _title_key(check['title']) != _title_key(binding.full_title):
            raise ValueError('Binding cannot relax an explicit full-title contract')
        if 'author' in check:
            author = r'\s+'.join(re.escape(part) for part in _title_key(check['author']).split())
            if not any(re.search(r'(?<!\w)' + author + r'(?!\w)', _title_key(name)) for name in binding.authors):
                raise ValueError('Official authors do not satisfy the required author')
        if 'year' in check and binding.publication_years and check['year'] not in binding.publication_years:
            raise ValueError('Official publication metadata differs from the required year')
        if 'doi' in check and binding.dois and check['doi'] not in binding.dois:
            raise ValueError('Official DOI metadata differs from the required DOI')
        if 'arxiv_id' in check:
            expected = check['arxiv_id']
            if (binding.canonical_id != expected if re.search(r'v\d+$', expected) else
                    re.sub(r'v\d+$', '', binding.canonical_id) != expected):
                raise ValueError('Binding differs from the required arXiv identifier')
        old = self._bindings.get(check_id)
        if old:
            if (old.canonical_id, old.full_title, old.evidence_hash) != (
                    binding.canonical_id, binding.full_title, binding.evidence_hash):
                raise ValueError('A bound paper goal cannot be changed during an episode')
            return False
        self._bindings[check_id] = binding
        return True

    @property
    def spec(self):
        return copy.deepcopy(self._spec)

    @property
    def public_spec(self):
        return copy.deepcopy(self._public_spec)

    def public_report(self, report=None):
        """Expose progress without leaking private targets through failure reasons."""
        report = self.evaluate() if report is None else report
        checks = [copy.deepcopy(c) for c in report.get("checks", []) if c.get("id") in self._public_ids]
        private = [c for c in report.get("checks", []) if c.get("id") not in self._public_ids]
        if private:
            status = ("failed" if any(c.get("status") == "failed" for c in private)
                      else "passed" if all(c.get("status") == "passed" for c in private) else "unknown")
            checks.append({"id": "private_validation", "kind": "private_validation", "status": status,
                           "reason": "independent private validation passed" if status == "passed" else "independent private validation is not satisfied",
                           "source": "private_evaluator"})
        status = report.get("status", "unknown")
        reason = ("all completion checks passed" if status == "passed" else
                  "; ".join(c["reason"] for c in checks if c.get("status") != "passed")
                  or "no deterministic completion contract for this task")
        return {"status": status, "checks": checks, "reason": reason, "source": report.get("source", self.source)}

    @staticmethod
    def _rename_chain(event, initial_digest):
        version = event.get('version', 1)
        history = event.get('rename_history', [])
        if type(version) is not int or version < 1 or not isinstance(history, list) or len(history) != version - 1:
            return None
        previous = None
        for index, change in enumerate(history, 1):
            if (not isinstance(change, dict) or change.get('from_version') != index
                    or change.get('to_version') != index + 1 or change.get('sha256') != initial_digest):
                return None
            try:
                before = str(Path(_string(change.get('from_path'), 'from_path')).resolve())
                after = str(Path(_string(change.get('to_path'), 'to_path')).resolve())
            except (ValueError, OSError):
                return None
            if Path(before).parent != Path(after).parent or (previous and previous != before):
                return None
            previous = after
        if previous and previous != str(Path(event['path']).resolve()):
            return None
        return history

    def _observe_artifact(self, event, path, resources, sources):
        identifier = event.get('artifact_id') or event.get('download_id') or 'legacy_path:' + path
        if not isinstance(identifier, str) or not identifier:
            return
        if identifier in self._retired_artifact_ids:
            return
        if (event.get('artifact_id') and event.get('download_id')
                and event['artifact_id'] != event['download_id']):
            return
        previous_path = self._artifact_ids.get(identifier)
        old = self._artifacts.get(previous_path) if previous_path else None
        if old:
            if old['artifact_id'] != identifier:
                return
            version = event.get('version', 1)
            if type(version) is int and 1 <= version < old.get('version', 1):
                historic_path = old['rename_history'][version - 1]['from_path']
                if str(Path(historic_path).resolve()) == path:
                    return  # A cumulative old completion arrived after a newer rename.
            if path == old['path'] and event.get('version', 1) == old.get('version', 1):
                return  # Cumulative events must never establish a new hash baseline.
            digest = old['initial'].get('sha256')
            history = self._rename_chain(event, digest)
            current = _read_artifact(path)
            old_version = old.get('version', 1)
            valid = (history is not None and event.get('version', 1) > old_version
                and history[:old_version - 1] == old.get('rename_history', [])
                and str(Path(history[old_version - 1]['from_path']).resolve()) == old['path']
                and event.get('initial_sha256') == digest and current.get('sha256') == digest
                and current.get('identity') == old['initial'].get('identity')
                and resources == old['urls'] and sources == old['source_urls'])
            if not valid:
                old['migration_error'] = 'artifact path/version changed without a valid content-preserving rename chain'
                return
            self._artifacts.pop(previous_path, None)
            old.update(path=path, version=event['version'], rename_history=copy.deepcopy(history))
            old.pop('migration_error', None)
            self._artifacts[path] = old
            self._artifact_ids[identifier] = path
            return
        evidence = _read_artifact(path)
        digest = evidence.get('sha256')
        history = self._rename_chain(event, digest)
        if history is None or (event.get('initial_sha256') is not None and event['initial_sha256'] != digest):
            return
        if event.get('version', 1) > 1 and event.get('initial_sha256') != digest:
            return
        previous_owner = self._artifacts.get(path)
        if previous_owner:
            self._retired_artifact_ids.add(previous_owner['artifact_id'])
        self._artifacts[path] = {'urls': resources, 'source_urls': sources, 'path': path,
            'initial': evidence, 'download_id': identifier, 'artifact_id': identifier,
            'version': event.get('version', 1), 'rename_history': copy.deepcopy(history)}
        # Legacy artifact checks retain their existing compatibility behavior.
        # The public durable-facts channel is stricter: only an actual completed
        # browser receipt with a matching original file baseline enters it.
        identity = event.get('file_identity')
        if (event.get('transport') == 'browser' and event.get('completed') is True
                and (event.get('artifact_id') or event.get('download_id'))
                and evidence.get('status') == 'passed' and evidence.get('bytes', 0) > 0
                and type(event.get('bytes')) is int and event['bytes'] == evidence['bytes']
                and event.get('sha256') == evidence.get('sha256')
                and isinstance(identity, (list, tuple)) and len(identity) == 2
                and all(type(value) is int for value in identity)
                and tuple(identity) == evidence.get('identity')):
            self._artifacts[path]['browser_receipt'] = True
        self._artifact_ids[identifier] = path

    def observe(self, observation, execution_info=None):
        self._pages = []
        context = (observation.get("context", []) if isinstance(observation, dict)
                   else getattr(observation, "context", []))
        if isinstance(observation, dict) and observation.get("kind") == "browser_ax":
            context = [observation]
        if isinstance(context, (list, tuple)):
            self._pages = copy.deepcopy([b for b in context if isinstance(b, dict) and b.get("kind") == "browser_ax"])
        info = execution_info if isinstance(execution_info, dict) else {}
        observed_info = (observation.get("info", {}) if isinstance(observation, dict)
                         else getattr(observation, "info", {}))
        observed_info = observed_info if isinstance(observed_info, dict) else {}
        initial_state = observed_info.get('initial_state') or {}
        if isinstance(initial_state, dict) and isinstance(initial_state.get('workspace_dir'), str):
            directory = Path(initial_state['workspace_dir']).expanduser()
            if directory.is_absolute():
                self._workspace_dir = str(directory.resolve())
        # Browser downloads often finish after click() returns. Their completion
        # can arrive only in a later observation, including request_finish's fresh
        # observation; always consume both channels.
        events = []
        for channel in (observed_info, info):
            if isinstance(channel.get("artifacts"), list):
                events.extend(channel["artifacts"])
        if info.get("action") == "download" and info.get("status") in ("executed", "already_downloaded"):
            if not any(isinstance(e, dict) and e.get("path") == info.get("path") for e in events):
                legacy = info
                if not info.get('download_id') and not info.get('artifact_id'):
                    # Old executors report each completed explicit transfer
                    # without a stable ID; give that new transfer its own ID.
                    self._legacy_download_sequence += 1
                    legacy = {**info, 'download_id': f'legacy-transfer-{self._legacy_download_sequence}'}
                events.append(legacy)
        for event in events:
            if (not isinstance(event, dict) or event.get("status") not in ("completed", "executed", "already_downloaded")
                    or ("completed" in event and event["completed"] is not True)):
                continue
            try:
                resources, sources = set(), set()
                for field in ("url", "final_url", "source_url", "page_url"):
                    try:
                        value = _url(event.get(field))
                    except ValueError:
                        continue
                    (resources if field in ("url", "final_url") else sources).add(value)
                if not (resources or sources):
                    continue
                path = str(Path(_string(event.get("path"), "path")).resolve())
                self._observe_artifact(event, path, resources, sources)
            except (ValueError, OSError):
                pass
        # Capture milestones now, even when evaluate() follows a later observation.
        for check in self._spec["checks"]:
            if check["scope"] == "milestone" and check["kind"] not in ("download", "paper_download") and check["id"] not in self._milestones:
                result = self._page_check(check)
                if result["status"] == "passed":
                    self._milestones[check["id"]] = result

    @staticmethod
    def _result(check, status, reason, source, **evidence):
        return {"id": check["id"], "kind": check["kind"], "scope": check["scope"],
                "status": status, "reason": reason, "source": source, **evidence}

    def _page_check(self, check):
        result = lambda status, reason, **e: self._result(check, status, reason, "browser_ax", **e)
        if check['kind'] == 'unresolved_requirement':
            return result('unknown', 'a user requirement is not yet supported by deterministic verification')
        if len(self._pages) != 1:
            return result("unknown", "a fresh, unambiguous browser page observation is required")
        page = self._pages[0]
        try:
            actual_url = _url(page.get("url"))
        except ValueError:
            return result("unknown", "observed page URL is unavailable")
        if "url" in check and actual_url != check["url"]:
            return result("failed", "observed page URL differs from the required URL", actual_url=actual_url)
        if check["kind"] == "url_equals":
            return result("passed", "current page URL matches", actual_url=actual_url)
        nodes = page.get("nodes")
        if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes):
            return result("unknown", "observed AX nodes are unavailable")
        if any(n.get("states") is not None and not isinstance(n["states"], dict) for n in nodes):
            return result("unknown", "observed AX states are malformed")
        nodes = [n for n in nodes if not n.get("ignored") and not (n.get("states") or {}).get("hidden")]
        missing = "unknown" if page.get("warnings") else "failed"
        if check["kind"] == "text_present":
            normalize = lambda text: " ".join(text.split()) if isinstance(text, str) else ""
            expected = normalize(check["text"])
            if any(expected == normalize(n.get(field)) for n in nodes for field in ("name", "text")):
                return result("passed", "required text is present in the current AX observation")
            return result(missing, "required text was not found in the current AX observation")
        if page.get("warnings"):
            return result("unknown", "incomplete AX observation cannot establish a unique element")
        matches = [n for n in nodes if n.get("role") == check["role"] and n.get("name") == check["name"]
                   and ("element_id" not in check or n.get("id") == check["element_id"])]
        if not matches:
            return result(missing, "required element was not found")
        if len(matches) != 1:
            return result("unknown", "role/name identifies multiple elements")
        node = matches[0]
        if "value" in check:
            if "value" not in node or node["value"] == "[redacted]":
                return result("unknown", "element value is unavailable or redacted")
            if node["value"] != check["value"]:
                return result("failed", "element value differs from the required value")
        states = node.get("states") or {}
        for key, expected in check.get("states", {}).items():
            if key not in states:
                return result("unknown", f"element state {key} is unavailable")
            if states[key] != expected:
                return result("failed", f"element state {key} differs from the required state")
        return result("passed", "unique element has the required value/states")

    def _download_checks(self, checks):
        if not checks:
            return {}
        current = {path: _read_artifact(path) for path in self._artifacts}
        parsed_pdfs, candidates, results, resources = {}, {}, {}, {}
        failure_priority = {}
        paper_task = any(c["kind"] == "paper_download" for c in checks)
        for check in checks:
            cid = check["id"]
            candidates[cid] = {}
            results[cid] = self._result(check, "unknown", "no matching completed download artifact", "artifact")
            binding = self._bindings.get(cid)
            if check['kind'] == 'paper_download' and 'title' not in check and binding is None:
                results[cid] = self._result(check, 'unknown', 'paper query has not been bound to an official identity', 'paper_resolver')
                continue
            effective = dict(check)
            if binding:
                effective.update(title=binding.full_title, arxiv_id=binding.canonical_id,
                                 _verified_arxiv_id=binding.canonical_id)
            for path, record in self._artifacts.items():
                if "url" in check and check["url"] not in record["urls"]:
                    continue
                evidence, initial = current[path], record["initial"]
                status, reason = evidence["status"], evidence["reason"]
                extra = {}
                if status == "passed":
                    if record.get('migration_error'):
                        status, reason = 'failed', record['migration_error']
                    elif binding and not self._artifact_matches_binding(record, binding):
                        status, reason = 'failed', 'download transport does not identify the bound official paper'
                    elif initial["status"] != "passed":
                        status, reason = "failed", "file was not verified at the successful download event"
                    elif (evidence["identity"], evidence["sha256"]) != (initial["identity"], initial["sha256"]):
                        status, reason = "failed", "file has changed since the successful download event"
                    elif evidence["bytes"] < check["min_bytes"]:
                        status, reason = "failed", "downloaded file is smaller than min_bytes"
                    elif check.get('workspace') == 'default' and self._workspace_dir is None:
                        status, reason = 'unknown', 'default project folder is not established by the environment'
                    elif check.get('workspace') == 'default' and str(Path(path).parent) != self._workspace_dir:
                        status, reason = 'failed', 'downloaded file is outside the default project folder'
                    elif "filename" in check and Path(path).name != check["filename"]:
                        status, reason = "failed", "downloaded filename does not match"
                    elif "sha256" in check and evidence["sha256"] != check["sha256"]:
                        status, reason = "failed", "downloaded file SHA-256 does not match"
                    elif check["kind"] == "paper_download":
                        if path not in parsed_pdfs:
                            parsed_pdfs[path] = _parse_pdf(path, evidence)
                        identity = _paper_identity(effective, parsed_pdfs[path])
                        status, reason = identity["status"], identity["reason"]
                        extra = {k: v for k, v in identity.items() if k not in ("status", "reason")}
                item = self._result(check, status, reason, "artifact", path=path,
                                    artifact_id=record.get('artifact_id'), version=record.get('version', 1),
                                    urls=sorted(record["urls"]), source_urls=sorted(record["source_urls"]),
                                    sha256=evidence.get("sha256"), **extra)
                if status == "passed":
                    # Copies of one paper must not count as several papers. For
                    # plain byte downloads preserve the physical-file contract.
                    identity = evidence["identity"]
                    resources[identity] = ("sha256", evidence["sha256"]) if paper_task else ("inode", identity)
                    candidates[cid][identity] = item
                else:
                    # Report the closest relevant artifact, not the first failure
                    # in arrival order (e.g. an unrelated book downloaded earlier).
                    priority = (int(binding is None or self._artifact_matches_binding(record, binding)),
                                int(reason == 'downloaded filename does not match'), int(status == 'failed'))
                    if cid not in failure_priority or priority > failure_priority[cid]:
                        failure_priority[cid] = priority
                        results[cid] = item
        # A private refinement must hold for the *same physical artifact*. Two
        # files satisfying the public title and private golden hash separately
        # cannot be joined into a false positive. Multiple bindings are ANDed.
        by_id = {c["id"]: c for c in checks}
        bindings = {}
        for check in checks:
            if "bind_to" in check:
                bindings.setdefault(check["bind_to"], []).append(check["id"])
        for parent, bound_ids in bindings.items():
            joint = set(candidates[parent])
            for cid in bound_ids:
                joint.intersection_update(candidates[cid])
            if candidates[parent] and not joint:
                results[parent] = self._result(by_id[parent], "unknown",
                    "no artifact jointly satisfies the independent checks for this goal", "artifact")
            candidates[parent] = {identity: item for identity, item in candidates[parent].items() if identity in joint}
        # Expand counts into assignment slots. Bound refinements reuse all slots
        # of their public parent; unbound checks remain distinct task objectives.
        owners, slots = {}, {}
        for check in checks:
            if "bind_to" in check:
                continue
            for index in range(check.get("count", 1)):
                slots[(check["id"], index)] = check["id"]

        def assign(slot, visited):
            cid = slots[slot]
            for identity in candidates[cid]:
                resource = resources[identity]
                if resource in visited:
                    continue
                visited.add(resource)
                if resource not in owners or assign(owners[resource][0], visited):
                    owners[resource] = (slot, identity)
                    return True
            return False

        for slot in slots:
            assign(slot, set())
        assigned = {c["id"]: [] for c in checks}
        for slot, identity in owners.values():
            cid = slots[slot]
            assigned[cid].append(identity)
        for check in checks:
            cid, expected = check["id"], check.get("count", 1)
            if "bind_to" in check:
                expected = by_id[check["bind_to"]].get("count", 1)
                assigned[cid] = assigned[check["bind_to"]]
            matches = [candidates[cid][identity] for identity in assigned[cid]]
            if len(matches) == expected:
                if expected == 1:
                    results[cid] = matches[0]
                else:
                    results[cid] = self._result(check, "passed", "all required distinct paper artifacts verified",
                                                "artifact", count=len(matches), artifacts=matches)
            elif candidates[cid]:
                results[cid] = self._result(check, "failed", "download checks require enough distinct local artifacts",
                                            "artifact", count=len(matches), required_count=expected)
        return results

    @staticmethod
    def _artifact_matches_binding(record, binding):
        identifiers = [arxiv_id_from_url(url) for url in record['urls']]
        identifiers = [identifier for identifier in identifiers if identifier]
        expected = binding.canonical_id
        if not identifiers:
            return False
        same = lambda value: (value == expected if re.search(r'v\d+$', expected) else
                              re.sub(r'v\d+$', '', value) == expected)
        return all(same(identifier) for identifier in identifiers)

    def evaluate(self):
        checks = self._spec["checks"]
        downloads = self._download_checks([c for c in checks if c["kind"] in ("download", "paper_download")])
        results = []
        for check in checks:
            if check["kind"] in ("download", "paper_download"):
                result = downloads[check["id"]]
            elif check["id"] in self._milestones:
                result = {**self._milestones[check["id"]], "source": "milestone"}
            else:
                result = self._page_check(check)
            results.append(result)
        status = ("unknown" if not results else "failed" if any(r["status"] == "failed" for r in results)
                  else "unknown" if any(r["status"] == "unknown" for r in results) else "passed")
        reason = ("no deterministic completion contract for this task" if not results
                  else "all completion checks passed" if status == "passed"
                  else "; ".join(f"{r['id']}: {r['reason']}" for r in results if r["status"] != "passed"))
        return {"status": status, "checks": copy.deepcopy(results), "reason": reason, "source": self.source}


def check_completion(task, info):
    """Legacy helper: navigation requires info['observation']; downloads reread bytes."""
    verifier = CompletionVerifier(task)
    info = info if isinstance(info, dict) else {}
    verifier.observe(info.get("observation"), info)
    if verifier.evaluate()["status"] != "passed":
        return ""
    return "navigation" if verifier.spec["checks"][0]["kind"] == "url_equals" else "download"
