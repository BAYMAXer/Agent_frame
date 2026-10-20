"""Semantic failure accounting with explicit observation ports.

The controller operates on the caller's StepState. Queue advancement and the
pipeline's control-state decisions remain owned by the pipeline.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .env import Observation
from .pipeline_types import Decision, Execution, StepState


_NO_CHANGE_DIFF_THRESHOLD = 2.0
_LOOP_HISTORY_LEN = 6
_FAILURE_REPEAT_LIMIT = 2


def _stable_action_key(action) -> str:
    """稳定的动作签名：忽略每次变化的 ref/id/坐标/href，用于「重复动作」卡死检测。

    action_signature 会把 element_id/target_ref 也算进去（每次快照 ref 都变），
    导致「连续点同一链接」被判成不同动作、卡死检测失效。这里只保留语义 + 稳定参数。
    """
    if not action:
        return ""
    a = action.action
    if a == "cli":
        import hashlib
        args = dict(action.args)
        args.pop("timeout_s", None)
        if "content" in args:
            args["content_sha256"] = hashlib.sha256(args.pop("content").encode()).hexdigest()
        return json.dumps({"action": a, **args}, ensure_ascii=False, sort_keys=True)
    target = str(action.args.get("target", "") or action.args.get("from_target", ""))
    target = re.sub(r"\(href[^)]*\)", "", target)          # 去掉 (href ...) 括号形式
    target = re.sub(r"href\s+[^\s,)]*", "href", target)    # 归一化 "href URL" → "href"
    target = re.sub(r"\[\d+\]", "", target)                # 去掉方括号数字 ref
    target = _normalize_target(target)
    stable = {"action": a, "target": target[:200]}
    for k in ("text", "option", "key", "url", "amount", "direction", "overwrite", "submit_key"):
        if k in action.args:
            stable[k] = action.args[k]
    return json.dumps(stable, ensure_ascii=False, sort_keys=True)


def _normalize_target(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _clear_stall(state):
    state.failure_counts.clear()
    state.failure_class = state.failure_key = ""
    state.memory.stall_count = 0
    state.no_change_count = 0
    state.action_keys.clear()
    state.shell_outputs.clear()
    state.semantic_targets.clear()
    state.memory.forbidden_repeats.clear()


def _semantic_observation(obs):
    if obs is None:
        return None
    return [{"kind": block.get("kind"), "url": block.get("url"),
             "nodes": [{k: n[k] for k in ("role", "name", "value", "checked", "selected", "expanded", "focused", "disabled", "states", "href", "url") if k in n}
                       for n in block.get("nodes", [])]}
            for block in obs.context if block.get("kind") == "browser_ax"]


@dataclass(frozen=True)
class ProgressPorts:
    """Observation facts and dynamic settings supplied by the orchestrator."""
    observation_page: Callable[[Optional[Observation]], dict]
    download_id: Callable[[dict], str]
    screen_diff: Callable[[Any, Any], float]
    early_stop_repeat: Callable[[], int]


class ProgressController:
    """Count actual failures and clear stale failure state after real progress."""

    def __init__(self, ports: ProgressPorts):
        self.ports = ports

    _stable_action_key = staticmethod(_stable_action_key)
    _normalize_target = staticmethod(_normalize_target)
    _clear_stall = staticmethod(_clear_stall)
    _semantic_observation = staticmethod(_semantic_observation)

    def _semantic_action_key(self, action, obs, state=None):
        signature = json.loads(self._stable_action_key(action)) if action else {"action": "parse"}
        alias = json.dumps([self.ports.observation_page(obs), signature], sort_keys=True, ensure_ascii=False)
        if action and obs is not None:
            nodes = [n for b in obs.context if b.get("kind") == "browser_ax" for n in b.get("nodes", [])]
            ref = action.args.get("target_ref") or action.args.get("element_id")
            node = next((n for n in nodes if ref and str(n.get("ref")) == str(ref)), None)
            if node is None:
                target = self._normalize_target(action.args.get("target") or action.args.get("from_target") or "")
                candidates = [n for n in nodes if self._normalize_target(n.get("name", ""))
                              and self._normalize_target(n.get("name", "")) in target]
                if len(candidates) == 1:
                    node = candidates[0]
            if node:
                signature["target"] = {"role": node.get("role"), "name": self._normalize_target(node.get("name", "")),
                                       "href": node.get("href") or node.get("url") or ""}
                if state is not None:
                    state.semantic_targets[alias] = signature["target"]
            elif state is not None and alias in state.semantic_targets:
                signature["target"] = state.semantic_targets[alias]
        return signature


    def _count_failure(self, state, failure_class, action, obs):
        key = json.dumps({"failure": failure_class, "page": self.ports.observation_page(obs),
                          "target": self._semantic_action_key(action, obs, state)}, ensure_ascii=False, sort_keys=True)
        state.failure_counts[key] = state.failure_counts.get(key, 0) + 1
        state.failure_count += 1
        state.failure_class = failure_class
        state.failure_key = key
        state.memory.stall_count = state.failure_counts[key]
        return state.memory.stall_count


    def _detect_stall(self, state: StepState, decision: Decision,
                      execution: Execution, before_screenshot, before_observation=None) -> Optional[str]:
        """Count one actual failure per decision, scoped to semantic target and navigation.

        Snapshot refs never participate; model self-assessment never supplies proof
        of progress. A changed page/value/file or verified effect clears old failures.
        """
        before_observation = before_observation or state.obs
        info = execution.info or {}
        failure = info.get("wait_error_code")
        if not failure and info.get("status") in {"rejected", "failed", "failure", "error", "uncertain"}:
            failure = str(info.get("error_code") or info.get("status"))
        if not failure and (info.get("pending_downloads") or info.get("status") in {"in_progress", "pending"}):
            return None
        if not failure:
            before_ids = {self.ports.download_id(a) for a in (before_observation.info if before_observation else {}).get("artifacts", [])}
            after_ids = {self.ports.download_id(a) for a in info.get("artifacts", []) if a.get("completed")}
            changed = (bool(info.get("effect_verified")) or bool(after_ids - before_ids)
                       or (execution.obs is not None and (
                           self.ports.observation_page(before_observation) != self.ports.observation_page(execution.obs)
                           or self._semantic_observation(before_observation) != self._semantic_observation(execution.obs))))
            visible_change = execution.obs is not None and self.ports.screen_diff(before_screenshot, execution.obs.screenshot) >= _NO_CHANGE_DIFF_THRESHOLD
            if changed or (visible_change and before_screenshot is not None):
                self._clear_stall(state)
                return None
            if execution.kind in {"shell", "cli"}:
                output = (json.dumps({k: info[k] for k in ("operation", "path", "command", "stdout", "stderr", "file", "entries", "exit_code") if k in info}, sort_keys=True, ensure_ascii=False) if execution.kind == "cli" else state.terminal_result or "")
                if state.shell_outputs and output != state.shell_outputs[-1]:
                    self._clear_stall(state)
                state.shell_outputs.append(output)
                failure = "unchanged_shell_output"
            elif execution.kind in {"env", "browser_dom"} and decision.action and decision.action.action not in {
                    "wait", "press", "hotkey", "handle_dialog", "switch_tab"}:
                failure = "no_progress"
            else:
                return None
        count = self._count_failure(state, failure, decision.action, before_observation)
        limit = max(1, self.ports.early_stop_repeat()) if failure in {"no_progress", "unchanged_shell_output"} else _FAILURE_REPEAT_LIMIT
        state.action_keys.append(state.failure_key)
        state.action_keys[:] = state.action_keys[-_LOOP_HISTORY_LEN:]
        if count >= limit:
            return f"同一页面与语义目标的 {failure} 已发生 {count} 次"
        return None
