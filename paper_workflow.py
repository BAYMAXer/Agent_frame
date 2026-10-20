"""Read-only paper identity binding. All downloads originate from UI interaction."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from .paper_resolver import ADAPTER_VERSION, arxiv_id_from_url, parse_observed_arxiv_metadata, resolve_arxiv_candidates
from .browser_results import paper_result_cards
from .scholar_search import is_scholar, scholar_result_cards

WORKFLOW_VERSION = "paper-workflow-v4-model-semantic-selection"


@dataclass
class WorkflowResult:
    observation: object
    operations: list = field(default_factory=list)
    messages: list = field(default_factory=list)


def observed_paper_urls(observation, query=''):
    """Prefer the selected paper; search pages retain all observed candidates."""
    info = getattr(observation, "info", {}) or {}
    blocks = getattr(observation, "context", []) or []
    urls = [info.get("url", "")]
    urls.extend(b.get("url", "") for b in blocks if b.get("kind") == "browser_ax")
    selected = [url for url in urls if arxiv_id_from_url(url)]
    if selected:
        return list(dict.fromkeys(selected))
    if query:
        scholar = [block for block in blocks if block.get('kind') == 'browser_ax'
                   and is_scholar(block.get('url') or info.get('url', ''))]
        if scholar:
            urls = []
            for block in scholar:
                for card in scholar_result_cards(block):
                    urls.append(card['href'])
                    urls.extend(link['href'] for link in card['pdf_links'])
            return list(dict.fromkeys(url for url in urls if arxiv_id_from_url(url)))
        cards = [card for block in blocks if block.get('kind') == 'browser_ax'
                 for card in paper_result_cards(block)]
        if cards:
            # Names are search hints, not identity predicates. A bounded batch
            # can be reviewed semantically; large pages require inspecting an
            # actual candidate rather than silently dropping nonliteral names.
            return list(dict.fromkeys(card['abs_url'] for card in cards
                if arxiv_id_from_url(card['abs_url'])))
    for container in [info, *blocks]:
        for candidate in container.get("download_candidates", []):
            if isinstance(candidate, dict):
                urls.append(candidate.get("url", ""))
        for node in container.get("nodes", []):
            if isinstance(node, dict):
                urls.append(node.get("href", ""))
    return list(dict.fromkeys(url for url in urls if arxiv_id_from_url(url)))


def binding_group_hash(bindings):
    """Stable evaluation identity, excluding timestamps and random artifact IDs."""
    semantic = {key: {field: value.get(field) for field in
                      ("query", "canonical_id", "full_title", "authors", "dois", "publication_years", "source_url", "pdf_url", "adapter_version")}
                for key, value in sorted(bindings.items())}
    return hashlib.sha256(json.dumps({"adapter": ADAPTER_VERSION, "bindings": semantic},
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PaperWorkflow:
    def __init__(self, *, resolver=None, identity_selector=None, task='', max_resolution_rounds=8,
                 retry_delay_s=2.0, max_transient_retries=1):
        self.resolver = resolver or resolve_arxiv_candidates
        self.identity_selector = identity_selector
        self.task = task
        self.max_resolution_rounds = max_resolution_rounds
        self._resolutions = set()
        self._resolution_cache = {}
        self._resolution_rounds = 0
        self.retry_delay_s = max(0.0, float(retry_delay_s))
        self.max_transient_retries = max(0, int(max_transient_retries))

    def advance(self, observation, completion, env, emit, *, remaining_s=None, allow_download=False):
        result = WorkflowResult(observation)
        session = getattr(env, "_browser_session", None)
        if observation is None or session is None:
            return result
        started = time.monotonic()

        def remaining():
            return float("inf") if remaining_s is None else remaining_s - (time.monotonic() - started)

        def record(event, **payload):
            payload.update(actor_role="framework", source="framework", policy_trainable=False)
            emit(event, **payload)

        info = getattr(observation, 'info', {}) or {}
        observed_metadata = None
        try:
            observed_metadata = parse_observed_arxiv_metadata(
                str(info.get('url') or ''), info.get('paper_metadata'),
                captured_at=info.get('captured_at'))
        except (TypeError, ValueError):
            pass
        for goal in completion.pending_paper_goals():
            urls = observed_paper_urls(observation, goal['query'])
            evidence = None
            if observed_metadata is not None:
                evidence = {field: getattr(observed_metadata, field) for field in (
                    'canonical_id', 'full_title', 'authors', 'source_url', 'pdf_url',
                    'adapter_version', 'dois', 'publication_years', 'abstract')}
            fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            key = (goal["check_id"], goal['query'], tuple(sorted(set(urls))), fingerprint)
            cached = self._resolution_cache.get(key)
            transient = cached and cached['reason_code'] in {'metadata_fetch_failed', 'resolution_timeout', 'semantic_unavailable'}
            retry = (transient and cached['attempts'] <= self.max_transient_retries
                     and time.monotonic() >= cached['retry_at'])
            if (not urls or (cached and not retry) or self._resolution_rounds >= self.max_resolution_rounds
                    or remaining() <= 0):
                continue
            self._resolutions.add(key)
            self._resolution_rounds += 1
            resolver_options = {'timeout': min(5.0, remaining()),
                                'total_timeout': min(30.0, remaining()),
                                'selection_context': {'task': self.task,
                                    'constraints': {k: goal[k] for k in ('author', 'year', 'doi', 'arxiv_id') if k in goal}}}
            if self.identity_selector is not None:
                resolver_options['identity_selector'] = self.identity_selector
            if observed_metadata is not None:
                resolver_options['metadata'] = observed_metadata
            resolution = self.resolver(goal["query"], urls, **resolver_options)
            reason_code = getattr(resolution, 'reason_code', '')
            self._resolution_cache[key] = {'reason_code': reason_code, 'status': resolution.status,
                'attempts': (cached['attempts'] if cached else 0) + 1,
                'retry_at': time.monotonic() + self.retry_delay_s}
            record("paper_resolution", check_id=goal["check_id"], observed_urls=urls,
                   status=resolution.status, reason=resolution.reason, reason_code=reason_code,
                   resolution_round=self._resolution_rounds, evidence_fingerprint=fingerprint)
            if resolution.status != "resolved" or resolution.binding is None:
                result.messages.append("论文身份尚未确认：" + resolution.reason)
                continue
            try:
                completion.bind_paper(goal["check_id"], resolution.binding)
            except ValueError as exc:
                record("paper_binding_rejected", check_id=goal["check_id"], reason=str(exc))
                result.messages.append("候选论文不满足任务约束：" + str(exc))
                continue
            record("goal_binding", check_id=goal["check_id"], binding=resolution.binding.to_dict())
            pdf = resolution.binding.pdf_url
            filename = goal.get("filename", "")
            result.messages.append(
                "模型已根据实际页面证据选定目标论文，代码已核对官方链接与标识：" + resolution.binding.full_title
                + "。请打开 PDF 页面 " + pdf
                + "，在浏览器里点击「下载 / Download」按钮保存 PDF"
                + (("，交付文件名应为 " + filename) if filename else "")
                + "；点击后浏览器会自动下载，框架会等待并核验文件名与内容。")

        completion.observe(observation)
        return result
