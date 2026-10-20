"""Step-review generation; queue advancement remains in the pipeline facade."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .browser_feedback import paper_review_error
from .paper_resolver import arxiv_id_from_url
from .step_verifier import code_review, unresolved_desktop_checks


@dataclass(frozen=True)
class ReviewPorts:
    completion: Any
    decision_judge: Any
    reviewer: Any
    remaining: Callable
    record_typed_judge: Callable
    recheck_jev: Callable
    publish_review: Callable
    record_call: Callable


class ReviewService:
    def __init__(self, ports: ReviewPorts):
        self.completion = ports.completion
        self.decision_judge = ports.decision_judge
        self.reviewer = ports.reviewer
        self._task_time_remaining = ports.remaining
        self._record_typed_judge = ports.record_typed_judge
        self._recheck_jev = ports.recheck_jev
        self._publish_review = ports.publish_review
        self._record_call = ports.record_call

    def _code_step_review(self, state, step, execution_info, allow_contract=True):
        started = time.monotonic()
        public = self.completion.public_report(state.verification)
        public['download_progress'] = self.completion.download_progress()
        public['download_transfers'] = self.completion.download_transfers(state.consumed_downloads)
        public['consumed_artifact_ids'] = list(state.consumed_downloads)
        state.download_facts = self.completion.download_facts()
        public['download_facts'] = state.download_facts
        report = code_review(step, state.obs, execution_info, public)
        error = paper_review_error(state.obs, self.completion, public, step)
        if error and report.get('source') != 'download_artifact_milestone':
            report = {'method': 'code', 'status': 'failed', 'reason': error, 'evidence': [error],
                      'checks': [*report.get('checks', []), {'kind': 'paper_identity', 'status': 'failed', 'reason': error}]}
        elif step.get('phase') in {'identify', 'locate_fulltext'} and execution_info.get('status') not in {'rejected', 'failed', 'uncertain'}:
            goals = self.completion.paper_goals()
            linked = set(step.get('check_ids', []))
            selected = [g for g in goals if g['check_id'] in linked] if linked else goals
            if selected and all(g['status'] == 'resolved' for g in selected):
                binding_ids = {re.sub(r'v\d+$', '', g['binding']['canonical_id'], flags=re.I) for g in selected}
                if step['phase'] == 'identify':
                    report = {'method': 'code', 'status': 'passed', 'source': 'official_paper_binding',
                                  'reason': '模型已根据页面证据选定目标论文，代码已核对官方元数据与论文 ID',
                              'evidence': sorted(binding_ids), 'checks': report.get('checks', [])}
                else:
                    observed = [((state.obs.info or {}).get('url') or '')]
                    observed.extend(c.get('url', '') for c in (state.obs.info or {}).get('download_candidates', []) if isinstance(c, dict))
                    observed.extend(n.get('href', '') for b in (state.obs.context or []) for n in b.get('nodes', []))
                    pdf_ids = {re.sub(r'v\d+$', '', i, flags=re.I)
                               for u in observed if '/pdf/' in u if (i := arxiv_id_from_url(u))}
                    if binding_ids.issubset(pdf_ids):
                        report = {'method': 'code', 'status': 'passed', 'source': 'official_pdf_link',
                                  'reason': '已观察到与官方论文身份一致的 PDF 入口',
                                  'evidence': sorted(pdf_ids), 'checks': report.get('checks', [])}
        if allow_contract and state.verification.get('status') == 'passed':
            report = {'method': 'code', 'status': 'passed', 'reason': '整项任务的全部确定性验收条件已满足',
                      'evidence': ['completion_contract'], 'checks': report.get('checks', [])}
        report['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
        return report, public

    def _review_visual(self, state, step, execution_info, code, identity, before=None):
        from .report_delivery import report_system_evidence
        from .report_review import text_only_task, review_packet
        text_review = (step.get('id') in {'whole_task', 'final'}
                       and bool(getattr(state, 'report_delivery', None)) and text_only_task(state.task, code))
        if text_review:
            packet, error = review_packet(state.task, state.report_delivery)
            if error:
                report = {**identity, 'method': 'code', 'status': 'unknown', 'review_scope': 'text_report',
                          'reason': error, 'evidence': []}
                self._publish_review(report)
                return report
            code = {**code, 'report_review': packet}
        code = {**code, 'file_delivery_evidence': report_system_evidence(state),
                'file_review_instruction': '写入成功只证明保存，不证明正文正确。对照真实读回正文、网页证据与用户要求核对名单及数量；总数与实际条目矛盾不能通过。读回是写入时快照，不代表之后未改变。'}
        if getattr(state, 'report_sources', None):
            # Collection spans pages: earlier observed content remains evidence,
            # even though the current viewport shows only the last page.
            sources = state.report_sources
            # Final report review needs the source rows, not only site navigation.
            final = step.get('id') in {'whole_task', 'final'}
            limit = min(18000 if final else 2500, (48000 if final else 12000) // max(1, len(sources)))
            code = {**code, 'observed_sources': [{**s, 'text': s['text'][:limit],
                    'excerpt_truncated': len(s['text']) > limit} for s in sources]}
        if getattr(state, 'report_delivery', None):
            code = {**code, 'report_delivery': {k: v for k, v in state.report_delivery.items() if k != 'sources'},
                    'report_review_instruction': '核对正文是否基于已观察来源并覆盖用户要求；正文不是框架自证，不能覆盖明确代码失败。'}
        # Explicit Jev selection covers both step and final semantic review.
        # Unknown stays unknown; deterministic gates are enforced by callers.
        if unresolved_desktop_checks(code):
            return {**identity, **code}
        typed = None
        exclusive = self.decision_judge and self.decision_judge.config.review_mode == 'exclusive'
        if self.decision_judge and (text_review or exclusive or step.get('id') not in {'whole_task', 'final'}):
            self._publish_review({**identity, 'method': 'decision_model', 'status': 'running',
                'reason': 'Jev 正在核对文字结果与已观察来源' if text_review else 'Decision 评估器正在核查动作前后证据'})
            typed = self.decision_judge.review(state.task, step, before, state.obs, execution_info, code,
                                               remaining_s=self._task_time_remaining(), checkpoints=state.judge_checkpoints)
            self._record_typed_judge()
            if exclusive and not text_review:
                typed = self._recheck_jev(state, step, execution_info, code, typed, before, identity)
            self._publish_review({**identity, **typed})
            state.routing_hint['surface'] = typed.get('judgment', {}).get('answers', {}).get('surface', {})
            if typed['status'] in {'passed', 'failed'} or (exclusive and not text_review):
                return {**identity, **typed}
        self._publish_review({**identity, "method": "model", "status": "running",
            "reason": "独立文字审查：核对答案与来源，不重新操作网页" if text_review else "独立核查截图与补充证据"})
        previous = len(self.reviewer.calls)
        report = self.reviewer.visual_review(state.task, step, state.obs, execution_info, code,
                                             remaining_s=self._task_time_remaining())
        for call in self.reviewer.calls[previous:]:
            self._record_call(call, "verifier")
        report = {**identity, **report}
        if typed:
            report['decision_judgment'] = typed
            report['latency_ms'] = round(report.get('latency_ms', 0) + typed.get('latency_ms', 0), 2)
        if text_review:
            report['review_scope'] = 'text_report'
            report['evidence_digest'] = packet['evidence_digest']
            report['coverage'] = [{'requirement': '核对文字回答的覆盖、来源和范围', 'status': report['status']}]
        self._publish_review(report)
        return report
