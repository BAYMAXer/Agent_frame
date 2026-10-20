"""Bounded browser settlement, independent of policy-step orchestration.

The facade supplies explicit observation/event/cancellation ports. Cross-step
caches remain in StepState, and this service never dispatches a policy action.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .actions import Action
from .pipeline_types import Execution, StepState, WaitConfig
from .judge_evidence import select_frame
from .visual_readiness import is_pdf_observation, content_thumbnail, stable_content, likely_loading_page


@dataclass(frozen=True)
class SettlementPorts:
    wait_config: WaitConfig
    env: Any
    decision_judge: Any
    observe: Callable
    remaining: Callable
    browser_event_metadata: Callable
    download_id: Callable
    observation_page: Callable
    emit: Callable
    publish_diagnostic: Callable
    record_typed_judge: Callable
    wait_status: Callable
    pdf_observation: Callable


class SettlementService:
    def __init__(self, ports: SettlementPorts):
        self.wait_config = ports.wait_config
        self.env = ports.env
        self.decision_judge = ports.decision_judge
        self._observe = ports.observe
        self._task_time_remaining = ports.remaining
        self._browser_event_metadata = ports.browser_event_metadata
        self._download_id = ports.download_id
        self._observation_page = ports.observation_page
        self._emit = ports.emit
        self._publish_diagnostic = ports.publish_diagnostic
        self._record_typed_judge = ports.record_typed_judge
        self._wait_status = ports.wait_status
        self._is_pdf_observation = ports.pdf_observation

    def _settle_rendering(self, execution: Execution, state: StepState) -> Execution:
        """Wait for nonblank stable PDF content without spending policy steps.

        This is a bounded readiness heuristic, never completion evidence. A timed
        out document is offered to the policy once instead of waiting on every turn.
        """
        obs = execution.obs
        cfg = self.wait_config
        if not cfg.enabled or not self._is_pdf_observation(obs) or obs.info.get('dialog'):
            return execution
        key = tuple(self._observation_page(obs).get(k) for k in ('page_id', 'document_id', 'url'))
        previous = content_thumbnail(obs.screenshot)
        use_jev = self.decision_judge is not None and hasattr(self.decision_judge, 'readiness')
        cached = state.render_waits.get(key)
        if (cached == 'timeout' and previous is None) or (cached == 'completed' and previous is not None):
            obs = copy.deepcopy(obs)
            obs.info['render_wait'] = {'status': cached, 'reason': 'pdf_render'}
            if cached == 'timeout':
                obs.info['render_wait'].update(suppress_policy_wait=True,
                    instruction='框架已等待加载至预算上限；不要用决策wait继续等待，请检查视图或恢复路径。')
            execution.obs = obs
            return execution
        started = time.monotonic()
        # PDF rendering may take longer than the short visual heuristic budget.
        # An available fast judge owns this asynchronous wait up to the existing
        # operation/task limit, instead of handing it to repeated policy waits.
        timeout = cfg.operation_timeout_s if use_jev else cfg.render_timeout_s
        deadline = started + min(timeout, self._task_time_remaining())
        self._publish_diagnostic('framework_wait', status='waiting', reason='pdf_render', elapsed_s=0)
        status = 'timeout'
        last_update = started
        polls, last_jev, readiness = 0, -float('inf'), 'unknown'
        jev_available = use_jev
        while time.monotonic() < deadline:
            if jev_available and time.monotonic() - last_jev >= 1.0:
                judged = self.decision_judge.readiness(obs.screenshot,
                    frame=select_frame(obs, max_nodes=20),
                    remaining_s=min(2.5, max(0, deadline - time.monotonic())))
                if hasattr(self.decision_judge, 'last_call'):
                    self._record_typed_judge()
                polls += 1
                answer = judged.get('answers', {}).get('readiness', {})
                readiness = answer.get('label', 'unknown')
                last_jev = time.monotonic()
                self._emit('framework_wait', status=readiness, reason='pdf_readiness',
                           model=judged.get('model'), latency_ms=judged.get('latency_ms', 0),
                           confidence=answer.get('confidence'), policy_trainable=False)
                if judged.get('status') != 'answered':
                    # Keep observing within the same bounded wait if the optional
                    # service is down; do not repeatedly incur network timeouts.
                    jev_available = False
            delay = (max(0, 1.0 - (time.monotonic() - last_jev)) if jev_available else cfg.poll_interval_s)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            fresh = self._observe()
            if fresh.info.get('observation_error'):
                previous = None
                continue
            obs = fresh
            fresh_key = tuple(self._observation_page(obs).get(k) for k in ('page_id', 'document_id', 'url'))
            if obs.info.get('dialog') or not self._is_pdf_observation(obs):
                status = 'interrupted'
                break
            if fresh_key != key:
                if fresh_key[0] != key[0] or fresh_key[2] != key[2]:
                    status = 'interrupted'
                    break
                # Chromium can replace the loading document with the PDF plugin
                # in the same tab/URL. That is still this render operation.
                key, previous = fresh_key, None
            current = content_thumbnail(obs.screenshot)
            if stable_content(previous, current) and (not jev_available or readiness != 'waiting'):
                status = 'completed'
                break
            previous = current
            if time.monotonic() - last_update >= 1:
                last_update = time.monotonic()
                self._publish_diagnostic('framework_wait', status='waiting', reason='pdf_render',
                                         elapsed_s=round(last_update - started, 3))
        if status != 'interrupted':
            state.render_waits[key] = status
            # Keep a bounded cache even for long browsing sessions.
            if len(state.render_waits) > 32:
                state.render_waits.pop(next(iter(state.render_waits)))
        obs = copy.deepcopy(obs)
        result = {'status': status, 'reason': 'pdf_render', 'elapsed_s': round(time.monotonic() - started, 3),
                  'jev_polls': polls, 'readiness': readiness}
        if status == 'timeout':
            result.update(suppress_policy_wait=True,
                instruction='框架已在加载预算内持续观察；不要用决策wait重复等待。请检查桌面/PDF视图、恢复加载路径或报告受阻。')
        obs.info['render_wait'] = result
        execution.obs = obs
        execution.info['render_wait'] = result
        self._publish_diagnostic('framework_wait', **result)
        return execution

    def _settle_page_readiness(self, action: Action, execution: Execution) -> Execution:
        """Poll a visibly loading page with one-frame Jev calls, without policy steps."""
        if (not self.wait_config.enabled or self.decision_judge is None or action.action not in {'goto', 'click', 'press'}
                or not hasattr(self.decision_judge, 'readiness')
                or execution.obs is None or not self._waiting_page(execution.obs)
                or self._task_time_remaining() <= 0):
            return execution
        cached = (execution.obs.info or {}).get('page_readiness_wait', {})
        if cached.get('status') == 'timeout' and cached.get('suppress_policy_wait'):
            # Reusing the same timed-out observation is not a new navigation.
            # Keep its exhausted budget visible to the policy, rather than
            # opening another operation-sized wait on an unchanged page.
            execution.info['page_readiness_wait'] = copy.deepcopy(cached)
            return execution
        started = time.monotonic()
        deadline = started + min(self.wait_config.operation_timeout_s, self._task_time_remaining())
        obs = execution.obs
        status, polls = 'unknown', 0
        judge_available = True
        while time.monotonic() < deadline:
            poll_started = time.monotonic()
            if judge_available:
                judged = self.decision_judge.readiness(obs.screenshot,
                    frame=select_frame(obs, max_nodes=20),
                    remaining_s=min(2.5, deadline - time.monotonic()))
                if hasattr(self.decision_judge, 'last_call'):
                    self._record_typed_judge()
                polls += 1
                answer = judged.get('answers', {}).get('readiness', {})
                status = answer.get('label', 'unknown')
                self._emit('framework_wait', status=status, reason='page_readiness',
                           model=judged.get('model'), latency_ms=judged.get('latency_ms', 0),
                           confidence=answer.get('confidence'), policy_trainable=False)
                if judged.get('status') != 'answered':
                    judge_available = False
                elif status == 'ready' and not self._waiting_page(obs):
                    break
            remaining = min(max(0, 1.0 - (time.monotonic() - poll_started)),
                            deadline - time.monotonic())
            if remaining <= 0:
                if time.monotonic() >= deadline:
                    break
            else:
                time.sleep(remaining)
            if time.monotonic() >= deadline:
                break
            fresh = self._observe()
            if fresh.screenshot is None or fresh.info.get('observation_error'):
                break
            obs = fresh
            if not self._waiting_page(obs):
                status = 'ready_for_policy'
                break
        if time.monotonic() >= deadline and self._waiting_page(obs):
            status = 'timeout'
        result = {'status': status, 'polls': polls,
            'elapsed_s': round(time.monotonic() - started, 3)}
        if status == 'timeout':
            result.update(suppress_policy_wait=True,
                instruction='框架加载等待预算已耗尽；不要反复决策wait，请检查加载障碍或更换路径。')
        result['page'] = self._observation_page(obs)
        # The next decision consumes Observation.info, while trajectory
        # diagnostics consume Execution.info. Both need the same wait result.
        obs = copy.copy(obs)
        obs.info = copy.deepcopy(obs.info or {})
        obs.info['page_readiness_wait'] = copy.deepcopy(result)
        execution.obs = obs
        execution.info['page_readiness_wait'] = result
        return execution

    def _waiting_page(self, obs):
        """Sparse black/white loading frames; a URL alone never proves readiness."""
        if likely_loading_page(obs):
            return True
        if (obs is None or obs.screenshot is None or self._is_pdf_observation(obs)
                or (obs.info or {}).get('active_surface') == 'desktop'
                or (obs.info or {}).get('observation_error') or (obs.info or {}).get('dialog')):
            return False
        url = self._observation_page(obs).get('url')
        if not url or url in {'about:blank', 'chrome://newtab/', 'edge://newtab/'}:
            return False
        useful = [n for b in obs.context if b.get('kind') == 'browser_ax' for n in b.get('nodes', [])
                  if n.get('role') not in {'RootWebArea', 'WebArea', 'banner', 'main', 'generic'}
                  and (n.get('name') or n.get('value') or n.get('direct'))]
        return not useful and content_thumbnail(obs.screenshot) is None

    def _settle_execution(self, action: Action, execution: Execution, state: StepState,
                          *, _deadline=None, _refresh_depth=0) -> Execution:
        """Observe relevant asynchronous downloads without resampling or replaying an action.

        CDP events decide transfer state. During a download attempt Jev may
        inspect fresh screenshots once per second, but cannot declare a file
        complete without a browser event and an artifact on disk.
        """
        cfg = self.wait_config
        if not cfg.enabled or execution.kind not in {"browser_dom", "env", "browser", "finish_request"}:
            return execution
        metadata = self._browser_event_metadata()
        if metadata is None:
            return execution
        info = execution.info
        original_metadata = copy.deepcopy(state.obs.info if state.obs else {})
        page = self._observation_page(state.obs)
        if page.get("page_id") or page.get("url"):
            state.last_browser_page = dict(page)
        else:
            page = dict(state.last_browser_page)
        target_url = str(action.args.get("url") or "").split("#", 1)[0]
        from urllib.parse import urljoin
        nodes = [n for b in (state.obs.context if state.obs else []) if b.get("kind") == "browser_ax"
                 for n in b.get("nodes", [])]
        target_node = next((n for n in nodes if action.args.get("target_ref")
                            and n.get("ref") == action.args["target_ref"]), {})
        linked_url = urljoin(page.get("url") or "", str(target_node.get("href") or target_node.get("url") or "")) if target_node else ""
        related_urls = {u.split("#", 1)[0] for u in (target_url, linked_url) if u}
        candidates = list(original_metadata.get("download_candidates", []))
        for block in state.obs.context if state.obs else []:
            candidates.extend(block.get("download_candidates", []))
        candidate_urls = {str(c.get("url", "")).split("#", 1)[0] for c in candidates if isinstance(c, dict)}
        prior_ids = {self._download_id(item) for key in ("artifacts", "download_events")
                     for item in original_metadata.get(key, []) if isinstance(item, dict)}
        tracked = set(state.waiting_download_ids)
        # Adapter artifact lists are cumulative across tabs, not causal evidence.
        # Only the executor's top-level operation ID identifies its own transfer.
        explicit_ids = {self._download_id(info)} - {""}
        started = time.monotonic()
        grace_end = started + cfg.download_start_timeout_s
        operation_end = min(started + cfg.operation_timeout_s,
                            _deadline if _deadline is not None else float("inf"))
        last_ui = -float("inf")
        last_jev = -float("inf")
        jev_polls = []
        announced = False
        waited = False
        outcome = "no_signal"
        failure = None

        def merge(events):
            for key in ("artifacts", "download_events", "pending_downloads", "artifact_events", "dialogs", "dialog"):
                if key in events:
                    info[key] = copy.deepcopy(events[key])
                    if execution.obs is not None:
                        # Keep the decision's before-observation immutable even if an adapter reuses it.
                        execution.obs.info[key] = copy.deepcopy(events[key])
            if events.get("dialogs") and "dialog" not in events:
                info["dialog"] = copy.deepcopy(events["dialogs"][0])
                if execution.obs is not None:
                    execution.obs.info["dialog"] = copy.deepcopy(events["dialogs"][0])
            elif "dialogs" in events and not events["dialogs"] and not events.get("dialog"):
                info.pop("dialog", None)
                if execution.obs is not None:
                    execution.obs.info.pop("dialog", None)

        def related(item):
            ident = self._download_id(item)
            if ident in tracked or ident in explicit_ids:
                return True
            urls = {str(item.get(key) or "").split("#", 1)[0] for key in ("url", "final_url", "source_url")}
            if related_urls.intersection(urls):
                return True
            if item.get("page_id") and page.get("page_id"):
                return item["page_id"] == page["page_id"]
            return bool(item.get("page_url") and page.get("url") and item["page_url"] == page["url"])

        def pending_items(events):
            source = events.get("pending_downloads")
            if source is None:
                source = [d for d in events.get("download_events", []) if d.get("status") == "in_progress"]
            selected = []
            for item in source:
                if not isinstance(item, dict):
                    continue
                ident = self._download_id(item)
                if ident and related(item):
                    selected.append(item)
                    tracked.add(ident)
            return selected

        # Copy container metadata only; PIL images need not be duplicated.
        if execution.obs is not None:
            execution.obs = copy.copy(execution.obs)
            execution.obs.info = copy.deepcopy(execution.obs.info)
        # Executor evidence covers the small interval before the first event poll.
        seed = {**info, **metadata}
        pending_items(seed)
        candidate = (action.action in {"click", "double_click", "goto", "download", "press", "hotkey"}
                     and info.get("dispatched") is not False
                     and info.get("status") not in {"rejected", "failed", "error"}
                     and (execution.kind == "browser_dom" or page.get("page_id")))
        # An event reader returning {} has no active browser; it is not a wait signal.
        candidate = candidate and bool(metadata or tracked)
        shortcut_keys = {str(key).casefold() for key in action.args.get('keys', [])} if action.action == 'hotkey' else set()
        save_shortcut = action.action == 'hotkey' and 's' in shortcut_keys and bool(
            shortcut_keys & {'ctrl', 'control', 'cmd', 'command', 'meta'})
        # Metadata describes the page, not the clicked target. Never scan it
        # for download keywords when classifying an acknowledgement/navigation.
        download_hint = ' '.join((str(action.args.get('target') or ''),
                                  str(action.args.get('url') or ''),
                                  str(target_node.get('name') or ''),
                                  str(target_node.get('href') or '')))
        transfer_expected = (action.action == "download" or save_shortcut or bool(target_node.get("download"))
                             or bool(related_urls.intersection(candidate_urls))
                             or bool(re.search(r"pdf|pptx?|download|save|下载|保存|演示文稿", download_hint, re.I)))
        # Export can submit an asynchronous server job. Keep a brief event
        # grace for immediate exports without treating no file as business failure.
        export_candidate = bool(re.search(r"export|导出", download_hint, re.I))
        candidate = candidate and (transfer_expected or export_candidate)
        info['download_intent'] = {'kind': 'file_transfer' if transfer_expected else
                                  'export_action' if export_candidate else 'none',
                                  'transfer_expected': bool(transfer_expected)}
        jev_unavailable = False
        while True:
            merge(metadata)
            pending = pending_items(metadata)
            completed = {self._download_id(item) for item in metadata.get("artifacts", [])
                         if isinstance(item, dict) and item.get("completed") is True and related(item)}
            failed = [item for item in metadata.get("download_events", []) if isinstance(item, dict)
                      and item.get("status") in {"failed", "canceled", "cancelled", "timeout"}
                      and related(item) and (self._download_id(item) in tracked or self._download_id(item) not in prior_ids)]
            if metadata.get("dialog") or metadata.get("dialogs"):
                outcome = "dialog"
                state.recovery = {"status": "resolve_dialog", "instruction": "先处理浏览器原生对话框，再检查下载状态；不要重复触发下载。"}
                break
            if failed and not pending:
                outcome, failure = "failed", next((item['error_code'] for item in failed if item.get('error_code')), 'download_failed')
                break
            if (tracked and tracked.issubset(completed)) or (completed - prior_ids and not pending):
                outcome = "completed"
                break
            if not pending and not tracked and (not candidate or time.monotonic() >= grace_end):
                break
            if tracked and not pending:
                # Disappearance without a terminal event is not proof of completion.
                outcome, failure = "uncertain", "download_state_unavailable"
                break
            now = time.monotonic()
            if (cfg.download_visual_assist and not pending and candidate and transfer_expected and not jev_unavailable and self.decision_judge is not None
                    and hasattr(self.decision_judge, 'download_wait')
                    and now < operation_end and now - last_jev >= 1.0
                    and self._task_time_remaining() > 0):
                last_jev = now
                current = execution.obs if not jev_polls and execution.obs is not None else self._observe()
                if current is not None and current.screenshot is not None:
                    execution.obs = current
                    event_summary = {
                        'pending': len(pending),
                        'completed': len(completed - prior_ids),
                        'failed': len(failed),
                        'dialog': bool(metadata.get('dialog') or metadata.get('dialogs')),
                    }
                    judged = self.decision_judge.download_wait(
                        # Once CDP confirms an in-progress transfer, its events
                        # establish the action effect. Re-sending the identical
                        # pre-click image every second adds no new evidence.
                        None if jev_polls and pending else state.obs, current, action, event_summary,
                        remaining_s=min(2.5, self._task_time_remaining(), max(.01, operation_end - time.monotonic())))
                    if judged.get('status') != 'answered':
                        jev_unavailable = True
                    self._record_typed_judge()
                    answers = judged.get('answers') or {}
                    poll = {'elapsed_s': round(time.monotonic() - started, 2),
                            'effect': (answers.get('effect') or {}).get('label', 'unknown'),
                            'next': (answers.get('next') or {}).get('label', 'unknown'),
                            'latency_ms': judged.get('latency_ms', 0),
                            'browser_pending': len(pending)}
                    jev_polls.append(poll)
                    info['download_jev_polls'] = jev_polls
                    self._publish_diagnostic('download_jev_poll', **poll)
                    if poll['next'] == 'wait' and not pending:
                        grace_end = min(max(grace_end, time.monotonic() + 2.5), started + 8.0)
                now = time.monotonic()
            deadline = min(operation_end, now + self._task_time_remaining())
            for item in pending:
                if isinstance(item.get("deadline_at"), (float, int)):
                    deadline = min(deadline, now + max(0.0, item["deadline_at"] - time.time()))
            if now >= deadline:
                outcome = "task_timeout" if self._task_time_remaining() <= 0 else "timeout"
                failure = "download_timeout" if pending else None
                cancel = getattr(self.env, "cancel_download", None) or getattr(
                    getattr(self.env, "_browser_session", None), "cancel_download", None)
                for item in pending:
                    if cancel is not None:
                        try:
                            canceled = cancel(self._download_id(item))
                            self._emit("framework_wait_cancel", download_id=self._download_id(item),
                                       result=canceled, policy_trainable=False)
                        except Exception as exc:
                            self._emit("framework_wait_cancel", download_id=self._download_id(item),
                                       error_type=type(exc).__name__, policy_trainable=False)
                final_metadata = self._browser_event_metadata()
                if final_metadata is not None:
                    metadata = final_metadata
                    merge(metadata)
                break
            if not announced:
                self._emit("framework_wait", status="waiting", reason="pending_download" if pending else "download_start_grace",
                           download_ids=sorted(tracked), elapsed_s=0, policy_trainable=False)
                announced = True
            if now - last_ui >= 1:
                self._wait_status("waiting", "等待下载完成" if pending else "等待浏览器下载事件", now - started)
                last_ui = now
            sleep_for = min(cfg.poll_interval_s, max(0.0, deadline - now))
            if not pending:
                sleep_for = min(sleep_for, max(0.0, grace_end - now))
            time.sleep(sleep_for)
            waited = True
            metadata = self._browser_event_metadata()
            if metadata is None:
                outcome, failure = "uncertain", "browser_events_unavailable"
                break
        if waited or outcome == "dialog":
            execution.obs = self._observe()
            if execution.obs is not None:
                execution.obs = copy.copy(execution.obs)
                execution.obs.info = copy.deepcopy(execution.obs.info)
            # Observation may dispatch late download/dialog events. Its newer
            # arrays must win over metadata read before taking the screenshot.
            fresh = execution.obs.info if execution.obs is not None else {}
            metadata = {**(metadata or {}), **{key: fresh[key] for key in
                ("artifacts", "download_events", "pending_downloads", "artifact_events", "dialogs", "dialog")
                if key in fresh}}
            if "dialogs" in fresh and not fresh.get("dialog"):
                metadata.pop("dialog", None)
            merge(metadata)
        terminal_ids = {self._download_id(item) for key in ("artifacts", "download_events")
                        for item in (metadata or {}).get(key, []) if isinstance(item, dict) and
                        (item.get("completed") is True or item.get("status") in {"failed", "canceled", "cancelled", "timeout"})}
        late_pending = pending_items(metadata or {})
        state.waiting_download_ids = tracked - terminal_ids
        if (metadata or {}).get("dialog") or (metadata or {}).get("dialogs"):
            outcome = "dialog"
        elif late_pending and not failure and _refresh_depth < 4 and time.monotonic() < operation_end:
            # Preserve the original deadline across late-event refreshes.
            return self._settle_execution(action, execution, state, _deadline=operation_end,
                                          _refresh_depth=_refresh_depth + 1)
        if failure:
            info["wait_error_code"] = failure
            info["wait_reason"] = f"{failure}；先检查实际下载状态，不要自动重发原动作"
            # Preserve dispatch status; waiting does not make a failed policy action succeed.
            if info.get("status") not in {"rejected", "failed", "error"}:
                info["status"] = "uncertain"
                info["reason"] = info["wait_reason"]
        elif (outcome == "no_signal" and candidate and action.action == "click"
              and info.get("resolved") == "visual" and self._is_pdf_observation(state.obs)):
            # CDP can acknowledge a click on Chromium's PDF viewer toolbar
            # without the viewer starting a download. Do not present that
            # dispatch acknowledgement as a successful download operation.
            info["status"] = "uncertain"
            info["wait_reason"] = ("PDF 查看器的浏览器虚拟点击没有产生下载事件或文件。"
                                   "先观察是否出现保存对话框；若桌面输入可用，可 observe_desktop 后从新截图操作。"
                                   "否则改走查看器菜单或保存快捷键，不要重复同一图标坐标")
            info["reason"] = info["wait_reason"]
        elif outcome == 'no_signal' and candidate and transfer_expected and jev_polls:
            info['status'] = 'uncertain'
            info['wait_error_code'] = 'download_not_confirmed'
            info['wait_reason'] = ('未观察到本轮浏览器下载开始事件或落盘文件；'
                                   '请检查当前页面或保存对话框后换操作方式，不能按已下载处理。')
            info['reason'] = info['wait_reason']
        if announced or failure or outcome == "dialog":
            elapsed = time.monotonic() - started
            info["framework_wait"] = {"status": outcome, "elapsed_s": round(elapsed, 3), "download_ids": sorted(tracked)}
            self._emit("framework_wait", **info["framework_wait"], policy_trainable=False)
            wait_failed = bool(failure or info.get('wait_error_code'))
            self._wait_status("failed" if wait_failed else "completed",
                              "浏览器对话框需要处理" if outcome == "dialog" else "下载未确认" if wait_failed else "浏览器等待结束", elapsed)
        return execution
