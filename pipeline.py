"""主循环流水线：观察 → 决策（证据先行）→ 定位 → 执行 → 控制状态更新。

闭环设计（核心）：
- 操作员输出 next_action 和一句话 summary；
- 执行后由框架代码或独立核查员检查结果，维护执行队列；
- 完整计划、验收证据与控制状态由框架保存，操作员只接收当前子目标；
- request_finish 只是请求，框架验证后才真正结束。

阶段（run() 里按序调用）：
  ① preprocess   预处理：确保观察 + 记录截图 + 构建记忆文本
  ② build_input  构造输入：当前子目标 + 近期摘要 + 按预算选择的结构和截图
  ③ decide       决策：next_action + summary（兼容旧字段）
  ④ ground       grounding：描述→坐标，记录坐标供下一步标记
  ⑤ execute      执行：skill / shell / subagent / terminal(request_finish|fail) / env
  ⑥ postprocess  后处理：记录 expected/observed + 控制状态 + 卡死检测 + Finish 验证
"""
from __future__ import annotations

from .report_delivery import wants_report

import copy
import os
import json
import time
import hashlib
import math
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from .actions import (
    Action, validate_action, action_signature, get_action_spec, ACTION_REGISTRY,
    parse_decision, action_from_decision,
)
from .env import Environment, Observation
from .model import ChatModel, Grounding, resolve_coords, mark_coordinate
from .model.decision_model import encode_image, ImageEncodingError
from .scene import scene_hint
from .memory import StructuredMemory
from .skills import SkillContext
from .prompts import build_format_feedback, build_planner_prompt
from .context import PromptCompiler, ContextBudgetError, ObservationIncompleteError
from .verifiers import CompletionVerifier
from .task_specs import task_profile, task_analysis, audit_requirement_proposals, TASK_ANALYSIS_VERSION
from .step_progress import ProgressConfig, StepProgressMonitor
from .model.decision_judge import DecisionJudge, DecisionJudgeConfig
from .model.judge_selection import RuntimeDecisionJudge
from .judge_evidence import effect_review, evidence_fingerprint, checkpoint, target_identity, select_frame
from .paper_workflow import PaperWorkflow, WORKFLOW_VERSION, binding_group_hash
from .paper_identity import PaperIdentitySelector
from .paper_sources import SOURCE_POLICY_VERSION, paper_search_policy
from .paper_resolver import ADAPTER_VERSION, arxiv_id_from_url
from .paper_search import PaperSearchRecovery
from .execution_queue import ExecutionQueue, normalize_verification, MAX_PLAN_STEPS, compile_download_milestones, refine_paper_plan
from .decision_context import action_scope, system_sections, selected_memory, current_brief
from .decision_modes import decision_mode, mode_capability, mode_evidence_key
from .step_verifier import StepReviewer, code_review, make_review_model, unresolved_desktop_checks
from .actions import BASIC_ACTIONS
from .browser_feedback import active_paper, paper_action_error, paper_download_preflight_error, redundant_sort_selection_error, search_navigation_error, operational_facts, exposed_nodes, paper_review_error
from .visual_readiness import is_pdf_observation, content_thumbnail, stable_content, likely_loading_page
from .pipeline_types import WaitConfig, AgentResult, StepState, Decision, Execution
from . import pipeline_format
from .plan_compiler import normalize_plan_actions
from .phase_timing import timed_phase
from .parallel_work import measured_call, prepare_model_client
from .download_review_scope import named_download_receipts
from .planning_service import PlanningService
from .settlement import SettlementService, SettlementPorts
from .review_service import ReviewService, ReviewPorts
from . import progress_controller
from .download_click_guard import repeated_download_click_error, remember_unconfirmed_download_click
from .progress_controller import ProgressController, ProgressPorts
from .progress_controller import _NO_CHANGE_DIFF_THRESHOLD, _LOOP_HISTORY_LEN, _FAILURE_REPEAT_LIMIT

# 实时状态推送（可选）：execute 阶段进度推给 viz 前端，失败不影响主流程
try:
    from .viz.logging import log_status as _viz_log_status
except Exception:  # noqa: BLE001
    _viz_log_status = None

# Re-export existing stall constants; the controller owns their definitions.


# --------------------------------------------------------------------------- #
# Runtime types are imported above and re-exported for existing callers.
# --------------------------------------------------------------------------- #










# --------------------------------------------------------------------------- #
# 主循环流水线
# --------------------------------------------------------------------------- #
class Pipeline:
    """主循环流水线。run() 只列 6 个阶段。"""

    def __init__(self, *, model: ChatModel, grounding: Grounding, env: Environment,
                 system_prompt: str, code_agent=None, max_steps: int = 40,
                 max_retries: int = 3, enable_reflection: bool = True,
                 early_stop_repeat: int = 3, step_callback=None,
                 completion_spec=None, context_config=None, private_completion_spec=None,
                 planner_model=None, event_path=None, run_id=None, run_manifest=None, max_replans=3,
                 wait_config=None, verifier_model=None, progress_config=None, decision_judge=None, mode_prompts=None, knowledge_method="all", knowledge_jev_url=""):
        from .prompt_settings import validate_mode_prompts
        self.mode_prompts = validate_mode_prompts(mode_prompts)
        from .knowledge_selection import validate_method
        self.knowledge_method = validate_method(knowledge_method)
        self.knowledge_jev_url = knowledge_jev_url
        self.model = model
        self.grounding = grounding
        self.env = env
        self.code_agent = code_agent
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.enable_reflection = enable_reflection
        self.early_stop_repeat = early_stop_repeat
        self.step_callback = step_callback  # 每步结束后回调(step_no, decision, execution, state)
        self.compiler = PromptCompiler(context_config)
        self.completion_spec = completion_spec
        self.private_completion_spec = private_completion_spec
        self.planner_model = planner_model or model
        self.event_path = event_path
        self.run_id = run_id or uuid.uuid4().hex
        self.run_manifest = copy.deepcopy(run_manifest or {})
        self.run_manifest["decision_prompt_overrides"] = copy.deepcopy(self.mode_prompts)
        self.event_writer = None
        self.events = []
        self._current_step = 0
        self.max_replans = max(0, int(max_replans))
        self._replans = 0
        self.completion = None
        self.planning_calls = []
        self._verification = None         # 最近一次 finish 验证的调用记录
        self.wait_config = wait_config if isinstance(wait_config, WaitConfig) else WaitConfig(**(wait_config or {}))
        self.progress_config = progress_config if isinstance(progress_config, ProgressConfig) else ProgressConfig(**(progress_config or {}))
        judge_options = dict(decision_judge or {}) if not isinstance(decision_judge, DecisionJudgeConfig) else {}
        selection_policy = judge_options.pop('selection_policy', 'threshold')
        if not isinstance(selection_policy, str) or selection_policy not in {'threshold', 'maximum_probability'}:
            raise ValueError('selection_policy must be threshold or maximum_probability')
        judge_config = decision_judge if isinstance(decision_judge, DecisionJudgeConfig) else DecisionJudgeConfig(**judge_options)
        self.decision_judge = RuntimeDecisionJudge(judge_config, selection_policy=selection_policy) if judge_config.url else None
        self._task_deadline = None
        self.verifier_model = verifier_model if verifier_model is not None else make_review_model(model)
        self.reviewer = StepReviewer(self.verifier_model, self.compiler)
        self._active_actions = None
        self._model_final_report = None
        self._reviewed_download_facts = []
        self._paper_search_recovery = PaperSearchRecovery()
        self.paper_identity_selector = PaperIdentitySelector(make_review_model(model))

    # ================= 主循环 ================= #
    def run(self, task: str) -> AgentResult:
        self._run_started = time.perf_counter()
        self._phase_timings = []
        self._task_deadline = time.monotonic() + self.wait_config.task_timeout_s
        self._replans = 0
        self._current_step = 0
        self._knowledge_selection = None
        self._report_delivery = {}
        self._report_required = wants_report(task)
        self.reviewer.calls.clear()
        self._model_final_report = None
        self._reviewed_download_facts = []
        self.completion = CompletionVerifier(task, self.completion_spec, self.private_completion_spec)
        self.paper_identity_selector.calls.clear()
        self._paper_workflow = PaperWorkflow(identity_selector=self._select_paper_identity, task=task)
        self._paper_search_recovery = PaperSearchRecovery(task=task)
        self.events = []
        manifest = self._make_manifest(task)
        result = AgentResult(task=task, run_id=self.run_id, run_manifest=manifest,
            private_evaluation={"spec": copy.deepcopy(self.private_completion_spec), "verifier_version": "3.1"})
        if self.event_path:
            from .training_events import TrainingEventWriter
            path = str(self.event_path).replace("{run_id}", self.run_id)
            if Path(path).suffix != ".jsonl":
                path = str(Path(path) / self.run_id / "events.jsonl")
            self.event_writer = TrainingEventWriter(path, run_id=self.run_id, manifest=manifest)
            self.events = self.event_writer.events
        else:
            self._emit("episode_start", manifest=manifest)
        result.run_manifest["phase_timings"] = self._phase_timings
        result.events = self.events
        result.review_calls = self.reviewer.calls
        try:
            self._publish_diagnostic('workbench_start', instruction=task, max_steps=self.max_steps)
            result = self._run(task, result)
            delivery = getattr(self, '_report_delivery', {})
            if delivery:
                delivery['verification_status'] = ('passed' if result.success else
                    'unknown' if result.termination_reason == 'completion_inconclusive' else 'not_passed')
                result.run_manifest['report_delivery'] = copy.deepcopy(delivery)
                if delivery.get('text'):
                    result.final_answer = delivery['text']
                self._publish_diagnostic('report_verification', status=delivery['verification_status'], version=delivery.get('version'))
            result.run_manifest["goal_bindings"] = self.completion.bindings
            result.run_manifest["goal_binding_hash"] = binding_group_hash(self.completion.bindings)
            elapsed = round(time.perf_counter() - self._run_started, 3)
            self._emit("episode_end", outcome={
                "success": result.success, "score": result.score,
                "reward": result.score if result.evaluation_available else None,
                "reward_valid": result.evaluation_available, "reward_source": result.reward_source,
                "termination_reason": result.termination_reason, "verification": result.verification,
                "final_answer": result.final_answer,
                "elapsed_s": elapsed}, elapsed_s=elapsed,
                goal_bindings=result.run_manifest["goal_bindings"],
                goal_binding_hash=result.run_manifest["goal_binding_hash"])
            from .run_metrics import summarize_events
            result.run_manifest['run_metrics'] = summarize_events(self.events)
            if _viz_log_status:
                _viz_log_status({'event': 'run_metrics', 'run_id': self.run_id, 'metrics': result.run_manifest['run_metrics']})
            session = getattr(self.env, '_browser_session', None)
            if callable(getattr(session, 'stop_preview', None)):
                session.stop_preview()
            result.run_manifest['knowledge_retrieval'] = copy.deepcopy(self._knowledge_selection)
            return result
        except BaseException as exc:
            self._emit("episode_error", error_type=type(exc).__name__,
                       step=self._current_step, reward=None, reward_valid=False,
                       elapsed_s=round(time.perf_counter() - self._run_started, 3))
            raise
        finally:
            session = getattr(self.env, '_browser_session', None)
            if callable(getattr(session, 'stop_preview', None)):
                session.stop_preview()
            if self.event_writer:
                self.event_writer.close()

    def _emit(self, event_type, **payload):
        if self._current_step:
            payload.setdefault("parent_decision_id", f"a_{self._current_step}")
        if self.event_writer:
            event = self.event_writer.emit(event_type, **payload)
        else:
            event = {"event_id": f"{self.run_id}:e{len(self.events) + 1}", "run_id": self.run_id,
                 "event_type": event_type, "timestamp": time.time(), "actor_role": "framework",
                 "source": "framework", **copy.deepcopy(payload)}
            self.events.append(event)
        if _viz_log_status and event_type in {'action_context', 'transition', 'transition_finalized',
                'model_call', 'execution', 'route', 'episode_end', 'episode_error', 'plan_revision'}:
            try:
                _viz_log_status({'event': 'workbench_record', 'run_id': self.run_id,
                    'record': {'event_id': event.get('event_id'), 'run_id': self.run_id,
                               'event_type': event_type, 'timestamp': time.time(), **payload}})
            except Exception:
                pass
        return event

    def _make_manifest(self, task):
        def model_record(model):
            cfg = getattr(getattr(model, "inner", model), "cfg", None)
            return {key: getattr(cfg, key, None) for key in (
                "name", "policy_revision", "tokenizer_revision", "processor_revision",
                "chat_template_revision", "temperature", "max_tokens", "capture_rollout")}
        root = Path(__file__).resolve().parent
        source_hash = hashlib.sha256()
        for name in ("pipeline.py", "pipeline_types.py", "pipeline_format.py", "plan_compiler.py", "knowledge_base.py", "knowledge_selection.py", "replanning.py", "run_metrics.py",
                     "model/grounding_usage.py", "model/grounding.py", "adapters/qwen_grounding.py", "adapters/uitars_grounding.py", "adapters/uivenus2_grounding.py",
                     "planning_service.py", "phase_timing.py", "parallel_work.py", "download_review_scope.py", "report_delivery.py", "report_review.py", "settlement.py", "review_service.py", "progress_controller.py",
                     "browser_interaction.py", "download_click_guard.py", "browser_profile.py", "persistent_chrome.py", "artifact_download.py", "prompts.py", "context.py", "browser_session.py", "verifiers.py",
                     "execution_queue.py", "judge_evidence.py", "model/judge_selection.py", "step_verifier.py", "step_progress.py", "decision_context.py", "decision_modes.py", "browser_feedback.py", "paper_search.py", "scholar_search.py", "browser_results.py", "visual_readiness.py", "memory/structured_memory.py", "desktop_state.py", "desktop_readiness.py", "desktop_capture.py", "windows_text.py",
                     "adapters/windows_env.py", "adapters/vmware_env.py", "task_specs.py", "paper_sources.py", "paper_workflow.py", "paper_identity.py", "paper_resolver.py", "cli_executor.py", "adapters/cli_env.py", "actions.py", "config/action.yaml", "model/decision_model.py", "model/decision_judge.py"):
            source_hash.update(name.encode())
            source_hash.update((root / name).read_bytes())
        manifest = {"run_id": self.run_id, "task": task, "framework_source_hash": source_hash.hexdigest(),
                    "compiler_version": self.compiler.version, "verifier_version": "3.1",
                    "paper_resolver_version": ADAPTER_VERSION, "paper_workflow_version": WORKFLOW_VERSION,
                    "decision_model": model_record(self.model), "planner_model": model_record(self.planner_model),
                    "planner_trainable": self.planner_model is self.model,
                    "grounder_trainable": False, "verifier_trainable": False,
                    "verifier_model": model_record(self.verifier_model), "max_steps": self.max_steps,
                    "public_completion": self.completion.public_spec,
                    "task_analysis": task_analysis(task, self.completion.public_spec),
                    "paper_search_policy": paper_search_policy(task) if self.completion.paper_goals() else None,
                    "completion_hash": hashlib.sha256(json.dumps(self.completion.spec, sort_keys=True).encode()).hexdigest(),
                    **self.run_manifest}
        manifest["run_id"] = self.run_id
        runtime = {"context": asdict(self.compiler.config), "max_steps": self.max_steps,
                   "max_retries": self.max_retries, "max_replans": self.max_replans,
                   "early_stop_repeat": self.early_stop_repeat,
                   "failure_repeat_limit": _FAILURE_REPEAT_LIMIT, "wait_config": asdict(self.wait_config),
                   "progress_config": asdict(self.progress_config), "task_analysis_version": TASK_ANALYSIS_VERSION,
                   "paper_source_policy_version": SOURCE_POLICY_VERSION,
                   "decision_judge": self.decision_judge.config.public() if self.decision_judge else None,
                   "decision_judge_selection_policy": getattr(self.decision_judge, 'selection_policy', 'threshold'),
                   "paper_resolver_version": ADAPTER_VERSION, "paper_workflow_version": WORKFLOW_VERSION,
                   "enable_reflection": self.enable_reflection, "verifier_model": model_record(self.verifier_model),
                   "decision_context_version": "current_step_brief_v3", "paper_search_recovery_version": "1.0", "max_plan_steps": MAX_PLAN_STEPS, "step_verifier_version": "1.1", "judge_evidence_version": "9b_v2_alias",
                   "decision_model": model_record(self.model), "planner_model": model_record(self.planner_model)}
        manifest["runtime_config"] = runtime
        manifest["runtime_config_hash"] = hashlib.sha256(
            json.dumps(runtime, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return manifest

    def _record_call(self, call, actor_role="decision"):
        call.setdefault("call_id", f"{self.run_id}:call:{len(self.events) + 1}")
        call["actor_role"] = actor_role
        call["policy_trainable"] = actor_role == "decision" or (actor_role == "planner" and self.planner_model is self.model)
        self._emit("model_call", actor_role=actor_role, source="model", call=call)

    def _run(self, task: str, result: AgentResult) -> AgentResult:
        state = self._init_state(task)
        self._prepare_initial_state(task, state, result)
        # 规划是执行前置条件。小模型规划失败时不盲目开始操作。
        planned = self._plan(task, state)
        result.plan = state.memory.plan
        result.planning_calls = self.planning_calls
        if not planned:
            result.termination_reason = "task_timeout" if self._task_time_remaining() <= 0 else "planning_failed"
            result.final_answer = "任务拆解失败，未执行任何操作。请检查规划模型输出。"
            result.verification = {"status": "unknown", "reason": result.termination_reason, "checks": []}
            return result

        return self._run_steps(task, state, result)

    @timed_phase('startup_preparation')
    def _prepare_initial_state(self, task, state, result):
        # All inputs still reach the planner. Only independent prerequisites
        # overlap; browser ownership and all event/state publication stay here.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='agent-prepare') as pool:
            client = pool.submit(measured_call, prepare_model_client, self.planner_model)
            classification = (pool.submit(measured_call, self.decision_judge.classify_task,
                task, remaining_s=self._task_time_remaining()) if self.decision_judge else None)
            environment_started = time.perf_counter()
            self._prepare_initial_environment(task, state, result)
            environment_ms = (time.perf_counter() - environment_started) * 1000
            preparation, client_ms = client.result()
            if classification is not None:
                judgment, judge_ms = classification.result()
                state.routing_hint['task'] = judgment
                self._record_typed_judge()
                state.task_profile['execution_hint'] = judgment
                result.run_manifest['task_routing_hint'] = copy.deepcopy(judgment)
                self._publish_diagnostic('decision_judgment', judgment=judgment)
            else:
                judge_ms = 0
        self._publish_diagnostic('parallel_preparation', client=preparation,
            client_ms=client_ms, classification_ms=judge_ms,
            environment_ms=round(environment_ms, 2),
            reason='客户端准备、任务分类与初始环境准备并行；全部汇合后才规划')

    def _prepare_initial_environment(self, task, state, result):
        if callable(getattr(self.env, 'prepare_workspace', None)):
            workspace = self.env.prepare_workspace(self.run_id)
            result.run_manifest['workspace'] = copy.deepcopy(workspace)
            self._publish_diagnostic('workspace_ready', workspace=workspace)
        from .cli_executor import cli_capability
        if not hasattr(self.env, 'initial_state'):
            self.env.initial_state = {}
        self.env.initial_state['cli_capability'] = cli_capability(self.env)
        state.task_profile = task_profile(task, self.completion.public_spec)
        self._publish_diagnostic('task_analysis', analysis=task_analysis(task, self.completion.public_spec))
        unresolved = [c["text"] for c in self.completion.public_spec.get("checks", [])
                      if c.get("kind") == "unresolved_requirement"]
        if unresolved:
            # 这些要求无法确定性验收，交由 request_finish 时的「模型验收器」判断，不提前终止。
            self._emit("contract_unresolved", requirements=unresolved, policy_trainable=False)
        # A preloaded browser page is part of the environment, not the task.
        # Observe it before planning so the planner can use an existing search
        # form instead of inventing a new portal from the title alone.
        if getattr(self.env, 'browser_start_url', None):
            state.obs = self._initial_browser_observation(task)
            self._set_download_deadline()
            self.completion.observe(state.obs)

    def _run_steps(self, task, state, result):
        for step_no in range(1, self.max_steps + 1):
            if self._task_time_remaining() <= 0:
                result.termination_reason = "task_timeout"
                break
            started = time.perf_counter()
            state.step_id = step_no
            self._current_step = step_no
            result.steps = step_no
            state = self.preprocess(state)
            if step_no == 1:
                initial = copy.deepcopy(getattr(self.env, "episode_manifest", {}) or
                                        getattr(self.env, "initial_state", {}) or {})
                if initial:
                    result.run_manifest["initial_state"] = initial
                    result.run_manifest["initial_state_id"] = initial.get("initial_state_id", "")
                    self._emit("environment_initialized", initial_state=initial)
            if state.obs is not None:
                result.screenshots[step_no] = state.obs.screenshot
            try:
                messages = self.build_input(state)
            except (ObservationIncompleteError, ImageEncodingError):
                state.obs = self._observe()
                state.screenshot_history[-1] = state.obs.screenshot
                try:
                    messages = self.build_input(state)
                except (ObservationIncompleteError, ImageEncodingError) as exc:
                    result.termination_reason = "observation_unavailable"
                    result.final_answer = "当前观察不完整，已停止操作并保存采集记录：" + str(exc)[:180]
                    self._publish_diagnostic('observation_unavailable', reason=str(exc)[:300])
                    self.completion.observe(None)
                    break
            except ContextBudgetError:
                result.termination_reason = "context_budget_exceeded"
                result.final_answer = "任务计划与当前截图超出输入预算，未继续执行。"
                break
            before = self._observation_record(state.obs, f"o_{step_no}")
            action_context = {'plan_revision': state.queue.revision if state.queue else None,
                'plan_step_id': (state.queue.current or {}).get('id') if state.queue else None,
                'plan_step': copy.deepcopy(state.queue.current or {}) if state.queue else {}}
            self._emit('action_context', step=step_no, observation=before, policy_input=copy.deepcopy(messages), **action_context)
            session = getattr(self.env, '_browser_session', None)
            if _viz_log_status and callable(getattr(session, 'start_preview', None)):
                def preview(frame):
                    if (state.obs.info or {}).get('active_surface', 'browser') == 'browser':
                        _viz_log_status({'event': 'live_frame', 'run_id': self.run_id,
                                         'step': self._current_step, **frame})
                session.start_preview(preview)
            decision = self.decide(messages, state)
            for call in decision.calls:
                call.setdefault("actor_role", "decision")
                call.setdefault("provenance", "sampled")
            if decision.action is None:
                # Retain every sampled output, but bound format/transport recovery.
                state.obs = self._observe()
                self.completion.observe(state.obs)
                state.verification = self.completion.evaluate()
                failure = "model_transport" if decision.calls and decision.calls[-1].get("error_type") else "parse_failure"
                state.execution_status = "rejected"
                validation_error = decision.calls[-1].get('validation_error', '') if decision.calls else ''
                state.execution_feedback = {"status": "rejected", "dispatched": False,
                                            "error_code": failure, "reason": "未获得合法动作，未执行环境操作"
                                            + ("：" + validation_error[:200] if validation_error else "")}
                self._count_failure(state, failure, None, state.obs)
                self._track_step_progress(state, None, state.execution_feedback, state.obs,
                                          review={"plan_step_id": state.queue.current['id'] if state.queue and state.queue.current else 'final',
                                                  "status": "unknown"})
                result.trajectory.append({
                    "step": step_no, "error": "动作解析失败",
                    **action_context,
                    "raw": (decision.raw or "")[-500:],
                    "observation": before,
                    "policy_input": copy.deepcopy(decision.calls[-1]["messages"] if decision.calls else messages),
                    "policy_output": decision.raw,
                    "decision_calls": decision.calls,
                    "model_calls": decision.calls,
                    "prompt_metadata": state.prompt_metadata,
                    "valid_action": False,
                    "terminated": False, "truncated": False,
                    "next_observation": self._observation_record(state.obs, f"o_{step_no+1}"),
                    "execution_info": copy.deepcopy(state.execution_feedback),
                    "execution_status": "rejected", "verification": copy.deepcopy(state.verification),
                    "step_progress": copy.deepcopy(state.step_progress),
                })
                self._emit("transition", transition=result.trajectory[-1])
                state.memory.add_event("⚠️ 上一步动作解析失败，请重新输出合法的 next_action JSON")
                if state.verification.get("status") == "passed" and not wants_report(state.task):
                    result.success = True
                    result.termination_reason = "verified"
                    break
                if self._task_time_remaining() <= 0:
                    result.termination_reason = "task_timeout"
                    break
                if state.memory.stall_count >= _FAILURE_REPEAT_LIMIT or state.step_progress.get('status') == 'stalled':
                    if self._recover_plan(state, result, step_no):
                        break
                continue
            ground_started = time.perf_counter()
            action = (self.ground(decision.action, state) if self._task_time_remaining() > 0
                      else copy.deepcopy(decision.action))
            self._emit("route", action=self._compact_action(decision.action),
                       routing=state.route, resolved_action=self._compact_action(action),
                       operation_mode=self._operation_mode(action, state.route))
            if _viz_log_status is not None:
                try:
                    _viz_log_status({"event": "route", "step": step_no,
                                     "operation_mode": self._operation_mode(action, state.route)})
                except Exception:  # noqa: BLE001
                    pass
            ground_ms = (time.perf_counter() - ground_started) * 1000
            execute_started = time.perf_counter()
            # execute 阶段推送进度（下载等长耗时动作让前端看到"执行中"而非干等）
            if _viz_log_status is not None:
                try:
                    _viz_log_status({"event": "executing", "step": step_no,
                                     "action": decision.action.action if decision.action else ""})
                except Exception:  # noqa: BLE001
                    pass
            self._set_download_deadline()
            execution = (self.execute(action, state) if self._task_time_remaining() > 0 else
                         Execution(kind="deadline", info={"status": "rejected", "dispatched": False,
                                   "error_code": "task_timeout", "reason": "任务总时限已到，未执行新动作"}))
            execution = self._settle_action(action, execution, state)
            self._emit("execution", action=self._compact_action(state.resolved_action or action),
                       execution_info=execution.info, outcome=execution.outcome,
                       next_observation=self._observation_record(execution.obs, f"o_{step_no+1}") if execution.obs else None)
            if _viz_log_status is not None:
                try:
                    _viz_log_status({"event": "executed", "step": step_no,
                                     "status": (execution.info or {}).get("status", ""),
                                     "dispatched": (execution.info or {}).get("dispatched"),
                                     "reason": (execution.info or {}).get("reason", ""),
                                     "error_code": (execution.info or {}).get("error_code", ""),
                                     "action": (execution.info or {}).get("action", "")})
                except Exception:  # noqa: BLE001
                    pass
            execute_ms = (time.perf_counter() - execute_started) * 1000
            state, should_break = self.postprocess(step_no, decision, execution, state, result)
            self._emit("verification", report=state.verification)
            result.steps = step_no
            entry = next((t for t in reversed(result.trajectory)
                          if t.get("action_id") == f"a_{step_no}"), None)
            if entry is not None:
                entry.update({
                    **action_context,
                    "observation": before,
                    "next_observation": self._observation_record(state.obs, f"o_{step_no+1}"),
                    "policy_input": copy.deepcopy(decision.calls[-1]["messages"] if decision.calls else messages),
                    "policy_output": decision.raw,
                    "decision_calls": decision.calls,
                    "model_calls": decision.calls,
                    "prompt_metadata": state.prompt_metadata,
                    "valid_action": True,
                    "routing": copy.deepcopy(state.route),
                    "operation_mode": self._operation_mode(action, state.route),
                    "execution_info": execution.info,
                    "framework_operations": copy.deepcopy(execution.info.get("framework_operations", [])),
                    "verification": copy.deepcopy(state.verification),
                    "terminated": bool(should_break and (result.success or execution.done)),
                    "truncated": bool(should_break and not (result.success or execution.done)),
                    "timing_ms": {"ground": round(ground_ms, 2), "execute_observe": round(execute_ms, 2),
                                  "total": round((time.perf_counter() - started) * 1000, 2)},
                })
                entry["step_reviews"] = [copy.deepcopy(e["report"]) for e in self.events
                    if e.get("event_type") == "step_verification" and e.get("report", {}).get("decision_step") == step_no
                    and e.get("report", {}).get("status") != "running"]
                entry["execution_status"] = execution.info.get("status", "unknown")
                entry["effect_verified"] = bool(execution.info.get("effect_verified", False))
                entry["schema_valid"] = True
                self._emit("transition", transition=entry)
            if self.step_callback is not None:
                try:
                    self.step_callback(step_no, decision, execution, state)
                except Exception:  # noqa: BLE001  回调失败不影响主流程
                    pass
            if should_break:
                break

        result.score = self._evaluate(result)
        if result.termination_reason == "unknown":
            result.termination_reason = "max_steps"
        # 补齐最后一步的 assessment（任务结束时没有下一步决策来评估 a_N）
        self._fill_final_assessment(result)
        transitions = [t for t in result.trajectory if "policy_input" in t]
        if transitions:
            last = transitions[-1]
            if result.success or result.termination_reason in {"evaluation_failed", "agent_failed", "environment_ended"}:
                last["terminated"] = True
            elif result.termination_reason in {"verification_changed", "evaluation_error"}:
                last["terminated"] = False
            last["verification"] = copy.deepcopy(result.verification)
            last["step_reviews"] = [copy.deepcopy(e["report"]) for e in self.events
                if e.get("event_type") == "step_verification" and e.get("report", {}).get("decision_step") == last["step"]
                and e.get("report", {}).get("status") != "running"]
            last["truncated"] = not last.get("terminated", False)
            last["done"] = True
            self._emit("transition_finalized", transition=last)
        return result

    @staticmethod
    def _observation_record(obs, observation_id):
        """把 Observation 转成可落盘的完整记录（截图 base64，dump 时外置去重）。"""
        if obs is None:
            return {"id": observation_id, "context": [], "screenshot": None}
        return {"id": observation_id, "context": copy.deepcopy(obs.context),
                "text": getattr(obs, "text", ""), "info": copy.deepcopy(obs.info),
                "screenshot": encode_image(obs.screenshot)}

    # ================= 六个阶段 ================= #
    @timed_phase("observation")
    def _initial_browser_observation(self, task):
        return self.env.reset(task)

    @timed_phase("observation")
    def preprocess(self, state: StepState) -> StepState:
        if state.obs is None:
            state.obs = self.env.reset(state.task)
            self._set_download_deadline()
        from .cli_executor import cli_capability
        state.obs.info['cli_capability'] = cli_capability(self.env)
        if getattr(mode_capability(state.obs, state.queue.current if state.queue else None), 'mode', None) != 'cli':
            state.obs = self._settle_rendering(Execution(kind="env", obs=state.obs), state).obs
        from .report_delivery import collect_sources
        collect_sources(state)
        self.completion.observe(state.obs)
        state.verification = self.completion.evaluate()
        state.download_facts = self.completion.download_facts()
        self._advance_ready_download_steps(state)
        state.screenshot_history.append(state.obs.screenshot)
        if len(state.screenshot_history) > 3:
            state.screenshot_history = state.screenshot_history[-3:]
        state.memory_text = self._build_memory_text(state)
        self._emit("observation", observation_id=f"o_{state.step_id}",
                   observation=self._observation_record(state.obs, f"o_{state.step_id}"))
        return state

    def _advance_ready_download_steps(self, state):
        # Late events or a previously completed download need no extra policy action.
        while state.queue and state.queue.current:
            current = state.queue.current
            predicates = current.get('verification', [])
            final_contract_ready = (current.get('phase') == 'verify'
                                    and state.verification.get('status') == 'passed')
            artifact_check = (predicates and all(p.get('kind') in {'download_complete', 'download_valid', 'artifact_download'}
                                                 for p in predicates))
            if not (artifact_check or final_contract_ready):
                break
            progress = {'download_progress': self.completion.download_progress(),
                        'download_transfers': self.completion.download_transfers(state.consumed_downloads),
                        'consumed_artifact_ids': list(state.consumed_downloads),
                        'download_facts': state.download_facts}
            if code_review(current, state.obs, {}, progress)['status'] != 'passed':
                break
            self._update_plan_progress(state, Execution(kind='framework_observation', obs=state.obs,
                                                       info={'status': 'observed', 'dispatched': False}))
            if state.queue.current is current:
                break

    @timed_phase("mode_selection")
    def _select_decision_mode(self, state):
        """Choose the mode after waits/queue updates and before policy inference.

        The action-transition question describes the previous goal. Re-evaluate
        against the actual current goal rather than letting that old answer or
        page-density heuristics silently control a different next action.
        """
        current = state.queue.current if state.queue else None
        from .cli_executor import cli_capability
        if state.obs is not None:
            state.obs.info['cli_capability'] = cli_capability(self.env)
        hard = mode_capability(state.obs, current)
        if hard is not None:
            profile = hard
        else:
            judge = self.decision_judge
            choose = getattr(judge, 'select_mode', None) if judge else None
            answer, reason = {}, 'jev_not_available'
            if callable(choose):
                key = mode_evidence_key(state.task, state.obs, current)
                cached = state.routing_hint.get('next_mode') or {}
                if cached.get('evidence_key') == key:
                    selection = cached
                else:
                    remaining = min(5.0, self._task_time_remaining())
                    if remaining <= 0:
                        judged = {'status': 'unknown', 'reason': 'task_deadline'}
                    else:
                        try:
                            judged = choose(state.task, current, state.obs, remaining_s=remaining)
                            self._record_typed_judge()
                        except Exception as exc:  # Optional judge failure must remain bounded.
                            judged = {'status': 'unknown', 'reason': 'mode_judge_' + type(exc).__name__}
                    if not isinstance(judged, dict):
                        judged = {'status': 'unknown', 'reason': 'mode_judge_invalid_response'}
                    selected = (judged.get('answers') or {}).get('next_mode', {})
                    selection = {'evidence_key': key, 'answer': selected if isinstance(selected, dict) else {},
                                 'status': judged.get('status'), 'reason': judged.get('reason', ''),
                                 'latency_ms': judged.get('latency_ms', 0),
                                 'plan_step_id': (current or {}).get('id')}
                    state.routing_hint['next_mode'] = selection
                if selection.get('status') == 'answered':
                    answer = selection.get('answer') or {}
                reason = selection.get('reason') or 'jev_unknown'
            profile = decision_mode(state.obs, current, judgment=answer, fallback_reason=reason)
        data = profile.to_dict()
        selection = state.routing_hint.get('next_mode') or {}
        if profile.source == 'jev' or data.get('fallback_reason') == 'jev_dom_without_current_ax':
            data['judgment'] = copy.deepcopy(selection.get('answer', {}))
        if state.obs is not None:
            state.obs.info['decision_mode'] = data
        self._publish_diagnostic('decision_mode', step=state.step_id, profile=data)
        return profile

    def build_input(self, state: StepState) -> List[dict]:
        page_source = state.page_source
        state.page_source = ""  # 一次性返回后清空，避免每步重复塞大段源码
        browser_result = state.browser_result
        state.browser_result = ""  # 一次性返回后清空
        # 结构观察块：browser AX snapshot（context）+ 桌面 AX（text）
        blocks = list(state.obs.context) if state.obs else []
        observation_info = state.obs.info if state.obs else {}
        browser_events = {k: observation_info[k] for k in (
            "dialog", "dialogs", "tabs", "active_modals", "browser_events", "artifacts",
            "pending_downloads", "download_events", "download_candidates", "document_kind", "content_type", "screenshot_stale")
            if observation_info.get(k)}
        # The raw observation keeps every candidate. The policy sees every
        # official arXiv PDF identifier without repeated page/document fields.
        candidates = browser_events.get("download_candidates")
        if isinstance(candidates, list):
            official, other = [], []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                url = str(candidate.get('url') or '')
                identifier = arxiv_id_from_url(url)
                if identifier and '/pdf/' in url:
                    official.append(identifier)
                elif url:
                    other.append({'url': url[:500], 'label': str(candidate.get('label') or '')[:100]})
            browser_events['download_candidates'] = {
                'total': len(candidates), 'arxiv_pdf_ids': list(dict.fromkeys(official)),
                'other': other}
        if browser_events:
            blocks.insert(0, {"kind": "browser_events", "text": json.dumps(browser_events, ensure_ascii=False)})
        if state.obs and state.obs.text:
            blocks.append({"kind": "desktop_a11y", "text": state.obs.text})
        # Fresh Jev target semantics select the prompt; capability checks guard
        # surface/AX validity, while page-density rules are fallback only.
        current = state.queue.current if state.queue else None
        profile = self._select_decision_mode(state)
        hint = scene_hint(state.obs)
        hint.update(mode='browser_' + profile.mode if profile.surface == 'browser' and profile.mode in {'dom', 'gui'} else profile.mode,
                    reason=profile.reason, mode_source=profile.source,
                    policy_mode=profile.mode, policy_surface=profile.surface)
        blocks.insert(0, {"kind": "scene", "text": "当前场景: " + json.dumps(hint, ensure_ascii=False)})
        tool_results = {k: v for k, v in {"page_source": page_source, "browser_result": browser_result}.items() if v}
        if state.execution_feedback.get('cli_result'):
            tool_results['cli_result'] = state.execution_feedback['cli_result']
        auxiliary = json.dumps(tool_results, ensure_ascii=False) if tool_results else ""
        self._active_actions = action_scope(current, state.obs,
                                            bool(getattr(self.env, "browser_only", False)), profile=profile)
        sections = system_sections(self._active_actions, state.obs, getattr(self.env, "os_name", "linux"),
                                   profile=profile, task=state.task, step=current, task_profile=state.task_profile,
                                   mode_prompts=self.mode_prompts)
        system = "\n".join(text for _, text in sections)
        mode = (state.obs.info or {}).get('control_mode', '') if state.obs else ''
        if mode and mode != state.control_mode:
            state.control_mode = mode
            self._publish_diagnostic('control_mode', mode=mode)
        facts = operational_facts(state.obs, self.completion,
                                  self.completion.public_report(state.verification) if self.completion else {}, current,
                                  os_name=getattr(self.env, "os_name", ""), search_recovery=self._paper_search_recovery)
        if facts != state.operation_facts:
            state.operation_facts = facts
            self._publish_diagnostic('operation_feedback', facts=facts)
        required = current_brief(state, self.completion.public_spec.get('checks', []) if self.completion else [], facts,
                                 knowledge=getattr(self, '_knowledge_selection', None))
        images = self._build_images(state) if getattr(self.model, "vision", True) and profile.mode != "cli" else []
        if observation_info.get("dialog"):
            # Native JS dialogs may block a new screenshot. Their fresh event
            # permits only dialog recovery; any retained image is explicitly stale.
            images = [(label, shot) for label, shot in images if shot is not None]
        user_text, images, metadata = self.compiler.compile(
            system=system, task=state.task, memory=state.memory_text,
            blocks=blocks, auxiliary=auxiliary, focus=(current or {}).get("goal", ""),
            required=required, required_title="当前子任务", memory_title="近期行动摘要（当时的观察与计划，非完成证据）", recent_memory=True,
            images=images,
            require_image=bool(getattr(self.model, "vision", True)) and profile.mode != "cli" and not bool(observation_info.get("dialog")))
        metadata.update(selected_actions=self._active_actions, plan_window=state.queue.window() if state.queue else {},
                        system_estimate=self.compiler.count(system), system_bytes=len(system.encode('utf-8')),
                         prompt_profile=copy.deepcopy((state.obs.info or {}).get('decision_mode', profile.to_dict())) if state.obs else profile.to_dict(),
                         prompt_rules=[name for name, _ in sections],
                        context_mode="current_step_brief_v4")
        state.prompt_metadata = metadata
        state.prompt_text = user_text
        if state.obs:
            state.obs.info["exposed_refs"] = metadata["exposed_refs"]
        return [
            {"role": "system", "content": system},
            self.model.build_vision_message(user_text, images),
        ]

    @timed_phase("decision")
    def decide(self, messages: List[dict], state: Optional[StepState] = None) -> Decision:
        raw, action = "", None
        calls: List[dict] = []
        base_messages = copy.deepcopy(messages)
        messages = copy.deepcopy(base_messages)
        dom_repair_used = False
        attempts = max(1, self.max_retries)
        for attempt in range(attempts):
            if self._task_time_remaining() <= 0:
                break
            try:
                raw = self.model.chat(messages, json_mode=True)
            except Exception as exc:  # noqa: BLE001  传输失败：记录本次调用，不进入重试
                call = copy.deepcopy(getattr(self.model, "last_call", {}))
                call.update({"messages": copy.deepcopy(messages),
                             "error_type": type(exc).__name__, "valid_action": False})
                calls.append(call)
                self._record_call(call)
                return Decision(raw="", action=None, calls=calls)
            call = copy.deepcopy(getattr(self.model, "last_call", {}))
            call.update({"messages": copy.deepcopy(messages), "response": raw, "valid_action": False,
                         "format_attempt": attempt + 1})
            calls.append(call)
            obj = parse_decision(raw)
            action = action_from_decision(obj or {})
            if action is not None and isinstance((obj or {}).get('next_action'), str):
                # Retain the exact sampled output/tokens for SFT/RL provenance.
                # The effective action is separately recorded, never fabricated
                # as a new model response or as evidence of completion.
                call['parser_normalization'] = 'finish_string_with_existing_explanation'
                call['parsed_action'] = {'type': action.action, **copy.deepcopy(action.args)}
            if action is not None:
                err = self._validate_action(action)
                error_type = 'invalid_action'
                if err is None and state is not None:
                    err = search_navigation_error(action, state.task,
                        state.queue.current if state.queue else None, state.obs, self.completion) or None
                    if err:
                        error_type = 'unobserved_search_result'
                if err is None and state is not None:
                    err = self._paper_search_action_error(action, state) or None
                    if err:
                        error_type = 'redundant_search_action'
                if err is None and state is not None:
                    err = redundant_sort_selection_error(action, state.obs) or None
                    if err:
                        error_type = 'redundant_control_action'
                if err is None and state is not None and action.action == 'wait':
                    info = state.obs.info or {} if state.obs else {}
                    render = next((info.get(key, {}) for key in ('render_wait', 'page_readiness_wait')
                                   if info.get(key, {}).get('suppress_policy_wait')), {})
                    prior_loading = state.execution_feedback.get('page_readiness_wait', {})
                    prior_page = prior_loading.get('page', {})
                    current_page = self._observation_page(state.obs)
                    if (prior_loading.get('suppress_policy_wait') and prior_page
                            and all(prior_page.get(k) == current_page.get(k) for k in ('page_id', 'document_id', 'url'))):
                        render = prior_loading
                    if render.get('suppress_policy_wait'):
                        err = ('框架已在时间预算内按秒等待并核查页面加载，继续 wait 不会推进任务。'
                               '请根据新观察换入口/恢复页面，或如实报告加载失败；不要再消耗决策步等待。')
                        error_type = 'redundant_loading_wait'
                if err is None and state is not None and not dom_repair_used and attempt + 1 < attempts:
                    err = self._dom_reference_feedback(action, state)
                    if err:
                        dom_repair_used = True
                        error_type = 'missing_dom_reference'
                patch = (obj or {}).get("state_update") or {}
                # Legacy auxiliary memory is optional. An invalid patch must
                # not prevent an otherwise valid primitive from executing.
                if not isinstance(patch, dict) or not isinstance(patch.get('known_fact_add', {}), dict):
                    call['ignored_fields'] = ['state_update']
                    patch = {}
                if err is None:
                    call["valid_action"] = True
                    def field(name):
                        value = obj.get(name)
                        return value if isinstance(value, str) else ''
                    summary = field('summary') or field('screen_analysis')
                    expected_effect = field('expected_effect') or summary
                    self._record_call(call)
                    return Decision(raw=raw, action=action, summary=summary,
                                    screen_analysis=field('screen_analysis') or summary,
                                    observed_effect=field('observed_effect'), action_status=field('action_status'),
                                    state_update=patch, expected_effect=expected_effect,
                                    calls=calls)
            else:
                if obj is None:
                    err, error_type = '输出不是合法 JSON 对象', 'invalid_json'
                elif 'next_action' not in obj:
                    err, error_type = '缺少 next_action 对象，需要 type 和本轮列出的必填参数', 'missing_action'
                else:
                    err, error_type = 'next_action 必须为包含 type 的对象', 'invalid_action'
            call.update(validation_error=err, validation_error_type=error_type)
            self._record_call(call)
            retry = attempt + 1 < attempts
            self._publish_diagnostic('decision_format_error', status='retry' if retry else 'exhausted',
                                     reason=err, error_type=error_type, attempt=attempt + 1, dispatched=False)
            if retry:
                # One bounded repair pair; do not append every malformed answer
                # to the next attempt or carry it into the next decision step.
                feedback = build_format_feedback(self.compiler.clip(err, 300))
                budget = max(0, 1280 - self.compiler.count(feedback) - 128)
                messages = [*copy.deepcopy(base_messages),
                            {'role': 'assistant', 'content': self.compiler.clip(raw, budget)},
                            {'role': 'user', 'content': feedback}]
        return Decision(raw=raw, action=None, calls=calls)

    def _paper_search_action_error(self, action, state):
        if self.completion is None:
            return ''
        goals = self.completion.paper_goals()
        if len(goals) != 1 or goals[0].get('status') != 'pending':
            return ''
        return self._paper_search_recovery.action_error(action, state.obs, goals[0]['query'])

    def _dom_reference_feedback(self, action, state):
        """One pre-dispatch repair; unavailable AX and explicit desktop remain visual."""
        if action.action not in {'click', 'type', 'select', 'double_click', 'right_click'}:
            return None
        if action.args.get('target_ref') or action.args.get('element_id'):
            return None
        from .scene import observation_scene
        selected_mode = ((state.obs.info or {}).get('decision_mode') or {}).get('mode') if state.obs else None
        if ((selected_mode == 'gui' or (selected_mode is None and observation_scene(state.obs).get('mode') == 'browser_gui'))
                and action.action in {'click', 'double_click', 'right_click', 'type'}):
            return None
        # A PDF document may expose only its viewer/container or a sparse
        # partial AX tree. Never demand a made-up DOM reference on that surface.
        # Explicit refs and semantic DOM routing still work in ground().
        if is_pdf_observation(state.obs):
            return None
        from .browser_session import INTERACTIVE
        nodes = [n for n in exposed_nodes(state.obs)
                 if n.get('direct') and n.get('role') in INTERACTIVE
                 and not n.get('states', {}).get('disabled')]
        if action.action == 'type':
            nodes = [n for n in nodes if n.get('role', '').lower() in {'textbox', 'searchbox', 'combobox'}]
        if not nodes:
            return None
        try:
            route = self.env.route_action(copy.deepcopy(action), state.obs)
        except Exception:
            return None
        if route and route.get('channel') == 'browser_dom':
            return None
        return ('当前 AX 有可操作元素，但此动作没有可确定的 DOM 引用。请从本轮 AX 中选择正确的 '
                'target_ref/element_id，结合论文标题和链接核对目标，不能猜编号；若目标确实是浏览器工具栏或系统窗口，'
                '先 observe_desktop。网页画布等无对应 AX 元素时可保留视觉 target。此候选动作尚未执行。')

    def ground(self, action: Action, state: StepState) -> Action:
        action = copy.deepcopy(action)
        if action.action == 'cli':
            state.route = {'channel': 'cli'}
            state.resolved_action = action
            state.last_grounding_coord = None
            return action
        if state.obs and state.obs.info.get("dialog") and action.action not in {
                "handle_dialog", "observe_desktop", "observe_browser", "fail", "list_tabs"}:
            state.route = {"channel": "reobserve", "error_code": "blocked",
                           "reason": "原生对话框仍打开，请先使用 handle_dialog 明确接受或取消"}
            state.resolved_action = action
            return action
        state.last_grounding_target = str(action.args.get("target", "")
                                          or action.args.get("from_target", ""))
        # 先路由：带 target_ref 且引用有效 → browser_dom；否则走 GUI grounding
        try:
            profile = (state.obs.info.get('decision_mode') or {}) if state.obs else {}
            visual_target = (profile.get('mode') == 'gui' and profile.get('source') == 'jev'
                             and action.action in {'click', 'double_click', 'right_click', 'type', 'drag_and_drop'}
                             and not action.args.get('target_ref') and not action.args.get('element_id'))
            state.route = None if visual_target else self.env.route_action(action, state.obs)
        except Exception:  # noqa: BLE001
            state.route = None
        search_error = search_navigation_error(action, state.task,
            state.queue.current if state.queue else None, state.obs, self.completion)
        input_error = self._paper_search_action_error(action, state)
        error = (input_error or search_error or redundant_sort_selection_error(action, state.obs, state.route)
                 or paper_download_preflight_error(action, state.obs, self.completion, state.route)
                 or paper_action_error(action, state.obs,
                                      active_paper(self.completion, state.queue.current if state.queue else None), state.route))
        if error:
            state.route = {"channel": "reobserve", "error_code": "redundant_search_action" if input_error else "unobserved_search_result" if search_error else "paper_identity_mismatch", "reason": error}
        if action.args.get('submit_key') and (state.route or {}).get('channel') not in {'browser_dom', 'reobserve'}:
            state.route = {'channel': 'reobserve', 'error_code': 'unsupported',
                           'reason': '输入后提交仅支持当前 DOM 输入框；请重新观察或分开 type 和 press'}
        if action.action == 'select' and state.route is None:
            state.route = {'channel': 'reobserve', 'error_code': 'unsupported',
                           'reason': 'select 仅支持当前原生下拉框的 DOM 引用。请使用 C/select 控件的 target_ref，匿名控件 target 留空；自定义下拉框改用 click 展开后点击实际选项。动作尚未执行。'}
        if state.route is not None:
            state.last_grounding_coord = None
            state.resolved_action = action
            return action
        resolved = self._resolve(action, state.obs)
        self._attach_observation_space(resolved, state.obs)
        state.last_grounding_coord = self._coordinate_of(resolved)
        state.resolved_action = resolved
        if get_action_spec(action.action) and get_action_spec(action.action).resolve != "none":
            self._emit("grounding", actor_role="grounder", source="frozen_model",
                       observation_id=f"o_{state.step_id}", target=state.last_grounding_target,
                       resolved_action=self._compact_action(resolved), policy_trainable=False)
        return resolved

    @staticmethod
    def _attach_observation_space(action, obs):
        if obs is not None and any(k in action.args for k in ("x", "x1")):
            info = obs.info or {}
            action.args["coordinate_space"] = info.get("screenshot_space", "screen_pixels")
            action.args["observation_metadata"] = {
                k: copy.deepcopy(info[k]) for k in (
                    "screenshot_space", "screenshot_size", "page_id", "document_id", "snapshot_id",
                    "captured_at", "url", "scroll_offset", "screen_origin", "virtual_desktop", "desktop_capture_id")
                if k in info}

    def _observe(self):
        """Refresh without resetting the task; failure must not reuse stale evidence."""
        try:
            obs = self.env.observe()
            if obs is not None:
                from .cli_executor import cli_capability
                obs.info["cli_capability"] = cli_capability(self.env)
                if getattr(self, "_last_cli_result", None):
                    obs.info["cli_result"] = copy.deepcopy(self._last_cli_result)
                return obs
        except Exception as exc:
            return Observation(info={"observation_error": type(exc).__name__})
        return Observation(info={"observation_error": "unavailable"})

    def _recover_browser(self, action, step_result, state):
        """One bounded visual recovery, only before input was dispatched.

        Even rejected DOM actions may have scrolled the page. Ground exclusively
        against a new observation. Stale/blocked/ambiguous actions are replanned.
        """
        info = step_result.info or {}
        self._emit("execution_attempt", action=self._compact_action(action), execution_info=info,
                   observation=self._observation_record(step_result.observation, "after_dom"), policy_trainable=False)
        if (info.get("status") != "rejected" or info.get("dispatched") is not False
                or action.args.get('submit_key')
                or info.get("error_code") not in {"not_found", "unsupported"}
                or action.action not in {"click", "double_click", "right_click", "type", "drag_and_drop"}):
            return step_result
        fresh = self._observe()
        self._emit("observation", observation_id="recovery", observation=self._observation_record(fresh, "recovery"),
                   policy_trainable=False)
        old_info = state.obs.info if state.obs is not None else {}
        if (fresh.screenshot is None or fresh.info.get("screenshot_space") != "css_viewport"
                or not old_info.get("page_id")
                or fresh.info.get("page_id") != old_info.get("page_id")):
            step_result.observation = fresh
            return step_result
        visual = self._resolve(action, fresh)
        self._attach_observation_space(visual, fresh)
        if visual.args.get("grounding_failed"):
            step_result.observation = fresh
            return step_result
        self._emit("grounding", actor_role="grounder", source="frozen_model", observation_id="recovery",
                   resolved_action=self._compact_action(visual), policy_trainable=False)
        recovered = self._step_environment(visual)
        self._emit("execution_attempt", action=self._compact_action(visual), execution_info=recovered.info,
                   policy_trainable=False)
        recovered.info = dict(recovered.info or {})
        recovered.info["recovery"] = {"from": copy.deepcopy(info),
                                      "action": self._compact_action(visual),
                                      "observation": self._observation_record(fresh, "recovery")}
        state.resolved_action = visual
        # The DOM route was abandoned; do not reintroduce its ref in review.
        state.route = None
        state.last_grounding_coord = self._coordinate_of(visual)
        return recovered

    def _step_environment(self, action):
        context = getattr(self.env, 'framework_wait_context', None)
        if not callable(context):
            return self.env.step(action)
        with context(lambda event: self._publish_diagnostic('framework_wait', **event), self._task_time_remaining):
            return self.env.step(action)

    @timed_phase("dispatch")
    def execute(self, action: Action, state: StepState) -> Execution:
        name = action.action
        # 技能路由：已注册的确定性 skill（calculate / verify_file / ilearning_* 等）
        from .skills import is_skill, run_skill
        if is_skill(name):
            try:
                result = run_skill(name, action.args, state.skill_ctx)
                state.skill_results[name] = result
                state.browser_result = result
                return Execution(kind="skill", executed=f"技能: {name}", obs=self._observe(),
                                 outcome=result, info={"status": "executed", "action": name, "skill_result": result})
            except Exception as exc:  # noqa: BLE001
                return Execution(kind="skill", executed=f"技能: {name}", obs=self._observe(),
                                 outcome=f"技能 {name} 执行失败：{type(exc).__name__}: {exc}",
                                 info={"status": "failed", "action": name, "error": str(exc)})
        if action.args.get('submit_key') and (state.route or {}).get('channel') not in {'browser_dom', 'reobserve'}:
            return Execution(kind='rejected', obs=state.obs, info={'status': 'rejected', 'dispatched': False,
                'error_code': 'unsupported', 'reason': '输入后提交需要有效 DOM 路由，不回退为未核实的视觉连按'})
        if name not in BASIC_ACTIONS:
            return Execution(kind="rejected", executed="rejected", obs=state.obs,
                info={"status": "rejected", "dispatched": False, "error_code": "non_basic_action"})
        if name == 'cli':
            from .cli_executor import execute_cli
            self._publish_diagnostic('cli_execution', status='running', action=self._compact_action(action))
            info = execute_cli(self.env, action.args, remaining_s=self._task_time_remaining())
            session = getattr(self.env, '_browser_session', None)
            if (getattr(self.env, 'cli_target', None) == 'local' and session is not None
                    and callable(getattr(session, 'record_cli_relocation', None))):
                try:
                    info['artifact_relocated'] = session.record_cli_relocation(info)
                except Exception as exc:
                    info['artifact_tracking_error'] = type(exc).__name__
            if info.get('effect_verified') and info.get('operation') in {'rename', 'move'}:
                if not hasattr(self, '_cli_relocations'):
                    self._cli_relocations = {}
                self._cli_relocations[info['path']] = info['destination']
            self._last_cli_result = copy.deepcopy(info)
            if info.get('operation') == 'write' and info.get('effect_verified'):
                state.written_files = [r for r in getattr(state, 'written_files', [])
                                       if r.get('path') != info.get('path')]
                state.written_files.append(copy.deepcopy(info))
            info['cli_result'] = copy.deepcopy(info)
            obs = self._observe()
            obs.info['cli_result'] = copy.deepcopy(info['cli_result'])
            self._publish_diagnostic('cli_execution', **info['cli_result'])
            return Execution(kind='cli', executed=self._compact_action(action), outcome=json.dumps(info['cli_result'], ensure_ascii=False), obs=obs, info=info)
        repeated = repeated_download_click_error(action, state)
        if repeated:
            self._publish_diagnostic('repeated_download_click', **repeated)
            return Execution(kind='rejected', executed='rejected', outcome=repeated['reason'],
                             obs=state.obs, info=repeated)
        guard = getattr(self.env, 'prevent_duplicate_download', None)
        if callable(guard) and name in {'click', 'double_click', 'hotkey', 'press'}:
            try:
                duplicate = guard(action, state.obs, state.route)
            except Exception as exc:
                return Execution(kind='rejected', obs=self._observe(), info={'status': 'rejected', 'dispatched': False,
                    'error_code': 'download_guard_unavailable', 'reason': f'重复下载检查暂不可用：{type(exc).__name__}'})
            if duplicate:
                self._publish_diagnostic('duplicate_download', **duplicate)
                return Execution(kind='rejected', outcome=duplicate['reason'], obs=self._observe(), info=duplicate)
        # browser_dom 通道：路由成功后走 DOM 定位执行（跳过 grounding）
        if state.route and state.route.get("channel") == "browser_dom":
            try:
                step_result = self.env.execute_routed(action, state.route)
                step_result = self._recover_browser(action, step_result, state)
                info = step_result.info if isinstance(step_result.info, dict) else {}
                return Execution(kind="browser_dom", executed=self._compact_action(action),
                                 outcome=json.dumps(info, ensure_ascii=False),
                                 obs=step_result.observation, info=info,
                                 reward=step_result.reward, done=step_result.done)
            except Exception as exc:  # noqa: BLE001
                return Execution(kind="browser_dom", executed=self._compact_action(action),
                                 outcome=f"结构化执行异常，需重新观察: {type(exc).__name__}",
                                 obs=self._observe(),
                                 info={"status": "uncertain", "error_type": type(exc).__name__})
        if state.route and state.route.get("channel") == "reobserve":
            return Execution(kind="browser_dom", outcome=state.route.get("reason", "需重新观察"),
                             executed="rejected", obs=self._observe(),
                             info={"status": "rejected", "dispatched": False,
                                   "reason": state.route.get("reason", "需重新观察"),
                                   "error": state.route.get("reason", "需重新观察"),
                                   "error_code": state.route.get("error_code", "stale")})

        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"
        if kind not in {"env", "browser", "terminal"}:
            return Execution(kind="rejected", info={"status": "rejected", "dispatched": False,
                             "error_code": "non_basic_executor"}, obs=state.obs)

        if kind == "browser":
            if name == "get_page_source":
                try:
                    result = self.env.browser_action(name, action.args)
                    state.page_source = result[:12000] + ("\n...(源码过长已截断)" if len(result) > 12000 else "")
                    return Execution(kind="browser", executed="浏览器: get_page_source", obs=self._observe(),
                                     info={"status": "executed", "action": name}, outcome="页面源码将在下一步返回")
                except Exception as exc:
                    return Execution(kind="browser", obs=self._observe(), info={"status": "failed", "error_code": "source_unavailable"},
                                     outcome=f"页面源码读取失败：{type(exc).__name__}")
            if name == "run_browser_js":
                try:
                    result = self.env.browser_action(name, action.args)
                    state.browser_result = result
                    return Execution(kind="browser", executed="浏览器: run_browser_js", obs=self._observe(),
                                     info={"status": "executed", "action": name}, outcome="JS 已执行，结果将在下一步返回")
                except Exception as exc:
                    return Execution(kind="browser", obs=self._observe(), info={"status": "failed", "error_code": "js_failed"},
                                     outcome=f"JS 执行失败：{type(exc).__name__}: {exc}")
            step_result = self._step_environment(action)
            result = json.dumps(step_result.info or {}, ensure_ascii=False)
            state.browser_result = result
            return Execution(kind="browser", executed=f"浏览器: {name}",
                             outcome=result, info=step_result.info or {}, obs=step_result.observation,
                             done=step_result.done, reward=step_result.reward)

        if kind == "terminal":
            if name == "fail":
                return Execution(kind="terminal", executed=name, outcome="任务失败",
                                 done=True, success=False, answer="")
            # request_finish：不立即结束，交给 postprocess 验证
            return Execution(kind="finish_request", executed=name, outcome="请求结束，待验证",
                             obs=self._observe(), done=False, success=False,
                             answer=str(action.args.get("answer", "")))

        # Basic GUI action, after coordinate resolution.
        try:
            step_result = self._step_environment(action)
        except Exception as exc:  # noqa: BLE001
            return Execution(kind=kind, executed=self._compact_action(action),
                             outcome=f"执行异常: {exc}", obs=self._observe(),
                             info={"status": "uncertain", "error_type": type(exc).__name__})
        outcome = self._describe_outcome(step_result)
        # type 落点校验结果透传：把「疑似打错输入框」的提示拼进 outcome，模型下一轮能看到并纠正
        if isinstance(step_result.info, dict):
            tc = step_result.info.get("type_check")
            if step_result.info.get("type_wrong_target") and tc:
                outcome += f"；⚠️ 输入落点校验：{tc}"

        return Execution(kind=kind, executed=self._compact_action(action),
                         outcome=outcome,
                         obs=step_result.observation, done=step_result.done,
                         info=step_result.info or {}, reward=step_result.reward)

    def _task_time_remaining(self) -> float:
        deadline = getattr(self, "_task_deadline", None)
        return max(0.0, deadline - time.monotonic()) if deadline is not None else float("inf")

    def _follow_ambiguous_tabs(self, action, execution, state):
        """Use Jev only when one dispatched action created multiple real tabs."""
        observed = getattr(execution, 'obs', None)
        follow = (observed.info or {}).get('tab_follow_candidates') if observed is not None else None
        candidates = (follow or {}).get('candidates') or []
        if (not 2 <= len(candidates) <= 8 or self.decision_judge is None
                or not hasattr(self.decision_judge, 'select_opened_tab')
                or self._task_time_remaining() <= 0):
            return execution
        current = state.queue.current if state.queue else None
        judged = self.decision_judge.select_opened_tab(
            state.task, (current or {}).get('goal', ''), action.args.get('target', ''), candidates,
            getattr(state.obs, 'screenshot', None), remaining_s=min(3.0, self._task_time_remaining()))
        self._record_typed_judge()
        answer = (judged.get('answers') or {}).get('tab') or {}
        label = answer.get('label', 'unknown')
        choice = int(label[4:]) if isinstance(label, str) and re.fullmatch(r'tab_\d+', label) else -1
        if not 0 <= choice < len(candidates):
            execution.info['tab_follow'] = {'status': 'ambiguous', 'candidate_count': len(candidates)}
            return execution
        tab_id = candidates[choice]['tab_id']
        try:
            switched = self._step_environment(Action('switch_tab', {'tab_id': tab_id}))
        except Exception:
            switched = None
        if switched is not None and (switched.info or {}).get('status') == 'executed':
            execution.obs = switched.observation
            execution.info['tab_follow'] = {'status': 'jev_selected', 'tab_id': tab_id,
                                            'candidate_count': len(candidates)}
            self._publish_diagnostic('tab_follow', **execution.info['tab_follow'])
        return execution

    def _select_paper_identity(self, query, candidates, **kwargs):
        selector = self.paper_identity_selector
        previous = len(selector.calls)
        report = selector.select(query, candidates, **kwargs)
        for call in selector.calls[previous:]:
            call['decision_step'] = self._current_step
            self.reviewer.calls.append(call)
            self._record_call(call, 'verifier')
        self._publish_diagnostic('paper_identity_review', query=query, report=report,
            calls=selector.calls[previous:])
        return report

    def _advance_paper_workflow(self, action, execution, state):
        if execution.kind in {"terminal", "finish_request", "deadline"} or self._task_time_remaining() <= 0:
            return execution
        workflow = getattr(self, "_paper_workflow", None)
        if workflow is None or not self.completion.paper_goals():
            return execution

        def workflow_event(event, **payload):
            self._emit(event, **payload)
            if event == 'paper_resolution':
                state.paper_resolution = {k: copy.deepcopy(payload[k]) for k in
                    ('status', 'reason', 'reason_code', 'check_id', 'observed_urls', 'resolution_round') if k in payload}
            elif event == 'goal_binding':
                state.paper_resolution = {}
            if event == "framework_operation_started":
                self._wait_status("waiting", "框架正在下载已确认的论文", 0)
            elif event == "framework_operation_finished":
                status = payload.get("result", {}).get("status")
                self._wait_status("completed" if status in {"executed", "already_downloaded"} else "failed",
                                  "论文传输结束，正在验收" if status in {"executed", "already_downloaded"} else "论文传输需要检查",
                                  payload.get("elapsed_s", 0))
        # Framework transport/renaming never turns a rejected sampled click into
        # a positive policy action. Uncertain dispatch must be resolved first.
        for _ in range(2):
            self.completion.observe(execution.obs or state.obs, execution.info)
            if self.completion.evaluate().get("status") == "passed":
                break
            info = execution.info
            self._set_download_deadline()
            # Read-only identity binding; all downloading and renaming stays in GUI/DOM.
            update = workflow.advance(execution.obs or state.obs, self.completion, self.env, workflow_event,
                remaining_s=self._task_time_remaining(), allow_download=False)
            for message in update.messages:
                state.memory.add_event(message)
            state.task_profile["goals"] = self.completion.paper_goals()
            for goal in state.task_profile["goals"]:
                binding = goal.get("binding") or {}
                if binding:
                    state.memory.known_facts["paper:" + goal["check_id"]] = {
                        "title": binding.get("full_title"), "id": binding.get("canonical_id"),
                        "pdf_url": binding.get("pdf_url"), "filename": goal.get("filename")}

            execution.obs = update.observation
            if not update.operations:
                break
            execution.info.setdefault("framework_operations", []).extend(update.operations)
            metadata = self._browser_event_metadata() or {}
            for key in ("artifacts", "artifact_events", "download_events", "pending_downloads"):
                if key in metadata:
                    execution.info[key] = copy.deepcopy(metadata[key])
            raced = any(op.get("result", {}).get("status") == "in_progress" for op in update.operations)
            if not raced:
                break
            execution = self._settle_execution(action, execution, state)
            execution = self._settle_rendering(execution, state)
        return execution

    def _set_download_deadline(self) -> None:
        # Do not create a browser session for desktop-only tasks.
        session = getattr(self.env, "_browser_session", None)
        options = getattr(session, "options", None)
        if options is not None and hasattr(options, 'download_max_bytes'):
            options.download_max_bytes = self.wait_config.download_max_bytes
        if options is not None and hasattr(options, "download_timeout_s"):
            options.download_timeout_s = max(0.001, min(self.wait_config.operation_timeout_s,
                                                       self._task_time_remaining()))

    def _browser_event_metadata(self):
        reader = getattr(self.env, "browser_event_metadata", None)
        if reader is None:
            reader = getattr(getattr(self.env, "_browser_session", None), "event_metadata", None)
        if reader is None:
            return None
        try:
            value = reader()
            return value if isinstance(value, dict) else None
        except Exception as exc:
            self._emit("framework_wait", status="failed", reason="browser_events_unavailable",
                       error_type=type(exc).__name__, policy_trainable=False)
            return None

    @staticmethod
    def _download_id(item):
        return str(item.get("download_id") or item.get("artifact_id") or item.get("id") or "")

    @staticmethod
    def _observation_page(obs):
        if obs is None:
            return {}
        block = next((b for b in obs.context if b.get("kind") == "browser_ax"), {})
        return {key: obs.info.get(key, block.get(key)) for key in ("page_id", "document_id", "url")}

    @timed_phase("settlement")
    def _settle_action(self, action, execution, state):
        if execution.kind == 'cli':
            # Background operations cannot be waiting on PDF pixels/window focus.
            return execution
        action = state.resolved_action or action
        execution = self._follow_ambiguous_tabs(action, execution, state)
        execution = self._settle_execution(action, execution, state)
        execution = self._settle_rendering(execution, state)
        execution = self._settle_page_readiness(action, execution)
        return self._advance_paper_workflow(action, execution, state)

    def _settlement_service(self):
        # Resolve ports at call time to preserve adapter/config replacement.
        return SettlementService(SettlementPorts(
            wait_config=self.wait_config, env=self.env, decision_judge=self.decision_judge,
            observe=self._observe, remaining=self._task_time_remaining,
            browser_event_metadata=self._browser_event_metadata, download_id=self._download_id,
            observation_page=self._observation_page, emit=self._emit,
            publish_diagnostic=self._publish_diagnostic, record_typed_judge=self._record_typed_judge,
            wait_status=self._wait_status, pdf_observation=is_pdf_observation))

    def _settle_rendering(self, execution: Execution, state: StepState) -> Execution:
        return self._settlement_service()._settle_rendering(execution, state)

    def _settle_page_readiness(self, action: Action, execution: Execution) -> Execution:
        return self._settlement_service()._settle_page_readiness(action, execution)

    def _settle_execution(self, action: Action, execution: Execution, state: StepState,
                          *, _deadline=None, _refresh_depth=0) -> Execution:
        return self._settlement_service()._settle_execution(
            action, execution, state, _deadline=_deadline, _refresh_depth=_refresh_depth)

    @staticmethod
    def _wait_status(status, reason, elapsed):
        if _viz_log_status is not None:
            try:
                _viz_log_status({"event": "framework_wait", "status": status,
                                 "reason": reason, "elapsed_s": round(elapsed, 1)})
            except Exception:
                pass

    def postprocess(self, step_no: int, decision: Decision, execution: Execution,
                    state: StepState, result: AgentResult) -> Tuple[StepState, bool]:
        """⑥ 后处理：记录 expected/observed + 控制状态 + 卡死检测 + Finish 验证。"""
        remember_unconfirmed_download_click(state, decision.action, state.resolved_action, execution)
        # 1) 记录模型对上一步的判断（observed / action_status）
        state.memory.observed_effect = decision.observed_effect
        state.memory.action_status = decision.action_status
        # 2) 根据 action_status 记录事件/失败（针对上一步动作）
        if state.last_action:
            desc = self._action_desc(state.last_action, state.last_target)
            if decision.action_status == "failure":
                state.memory.add_failure(f"{desc} 失败：{decision.observed_effect or '无可见变化'}")
                state.memory.record_failed_strategy(desc)
                state.memory.add_event(f"{desc} → 失败")
            else:
                state.memory.add_event(f"{desc} → {decision.observed_effect or decision.action_status or '已执行'}")
        # 3) 应用 state_update 补丁
        state.memory.apply_state_update(decision.state_update)
        # 4) 回填上一步动作 a_{t-1} 的 assessment（decision 的 observed_effect/action_status
        #    评价的是上一步动作，而不是本步 next_action）
        self._fill_prev_assessment(result, decision)
        # 4.1) 记录这一步的 transition：a_t 的 assessment 留空，下一步回填
        result.trajectory.append({
            "step": step_no,
            "action_id": f"a_{step_no}",
            "observation_id": f"o_{step_no}",
            "screen_analysis": decision.screen_analysis,
            "summary": decision.summary,
            "state_update": decision.state_update,
            "page_info": state.obs.text if state.obs else "",  # 决策时的页面信息（可交互元素+selector 或空）
            "action": self._compact_action(decision.action),
            "grounding": {
                "target": state.last_grounding_target,
                "coord": list(state.last_grounding_coord) if state.last_grounding_coord else None,
            },
            "routing": state.route,
            "resolved_action": self._compact_action(state.resolved_action) if state.resolved_action is not None else None,
            "prompt_metadata": state.prompt_metadata,
            "prompt_text": state.prompt_text,
            "observation": {
                "context_kinds": [b.get("kind") for b in (state.obs.context if state.obs else [])],
                "exposed_refs_count": len((state.obs.info or {}).get("exposed_refs") or []) if state.obs else 0,
            },
            "expected_effect": decision.expected_effect,
            "executed": execution.executed,
            "outcome": execution.outcome,
            "reward": execution.reward,
            "done": False,
            "assessment": None,
        })
        # 5) 更新程序信息（当前动作 → 供下一轮）
        self._update_program_info(decision.action, execution, state)
        # 5.5) 记录动作历史（程序侧，让模型能回顾从头到尾做过了什么、结果如何）
        # 用精简 outcome（不存完整 JSON），否则动作历史快速膨胀、挤掉「最近事件/已知事实」等关键记忆
        if decision.action:
            a = decision.action
            adesc = self._action_desc(a.action, self._target_of(a))
            aoutcome = self._brief_outcome(execution)
            state.memory.add_action(step_no, adesc, aoutcome)
            state.memory.add_decision_note(step_no, decision.summary or decision.screen_analysis, adesc,
                                           str(execution.info.get('status') or ''))
        # 6) env 动作更新观察
        before_observation = state.obs
        before_screenshot = state.screenshot_history[-1] if state.screenshot_history else None
        if execution.obs is not None:
            state.obs = execution.obs
        from .report_delivery import collect_sources, wants_report
        collect_sources(state)
        if wants_report(state.task) and (execution.kind == 'finish_request' or
                (state.queue and (state.queue.current or {}).get('phase') == 'report')):
            self._ensure_report(state)
        self.completion.observe(state.obs, execution.info)
        state.verification = self.completion.evaluate()
        state.download_facts = self.completion.download_facts()
        if execution.info.get("error_code") == "blocked":
            state.recovery = {"status": "resolve_obstruction", "original_action": self._compact_action(decision.action),
                              "blocker": execution.info.get("blocker"),
                              "blocker_ref": execution.info.get("blocker_ref"),
                              "active_modals": execution.info.get("active_modals", []),
                              "instruction": "先根据当前截图和AX处理障碍，再重新定位原目标；不要直接重复原点击。"}
        elif state.recovery and execution.info.get("status") == "executed":
            state.recovery["status"] = "reobserve_original_target"
        self._update_plan_progress(state, execution, before_observation=before_observation,
                                   expected_effect=decision.expected_effect, action=state.resolved_action or decision.action)
        # A finish request can advance a preceding semantic step. Reconcile
        # already-proven download milestones before final review ends the run.
        self._advance_ready_download_steps(state)
        execution.obs = state.obs
        result.trajectory[-1]["step_verification"] = copy.deepcopy(state.step_review)
        result.trajectory[-1]["execution_queue"] = state.queue.snapshot() if state.queue else {}
        # 6.5) 单步动作有效性校验：env 动作执行后屏幕无变化 → 当场提示换策略
        # （不等到连续 2 步 STALL，每一步点空/点错都立即提示，避免反复点同一无效目标）
        if execution.kind == "env" and execution.obs is not None and before_screenshot is not None:
            if self._screen_diff(before_screenshot, execution.obs.screenshot) < _NO_CHANGE_DIFF_THRESHOLD:
                state.memory.add_event(
                    "⚠️ 这一步动作后屏幕没有可见变化：可能点空 / 点错 / 焦点不对。"
                    "下一步请换一种方式（键盘快捷键 / 不同菜单路径 / 重新定位目标），不要重复同样的点击。")
        # 7) 控制状态 + 确定性卡死检测
        self._compute_control_state(state, decision, execution, before_screenshot, before_observation)
        result.trajectory[-1]["step_progress"] = copy.deepcopy(state.step_progress)
        if state.step_review.get("status") == "passed":
            self._clear_stall(state)
            state.memory.control_state = "ADVANCE"
        should_break = False
        report_required = wants_report(state.task)
        report_milestone = bool(report_required and state.queue and state.queue.current and state.queue.current.get('phase') == 'report')
        if report_required and state.queue is not None and (state.queue.current is None or report_milestone):
            self._ensure_report(state)
        # 8) 设置下一步的 expected_effect（当前决策的 expected_effect → 下一步对比用）
        state.memory.expected_effect = decision.expected_effect
        # All accepted task conditions must be backed by framework evidence.
        if state.verification.get("status") == "passed" and execution.kind != "terminal" and not report_required:
            result.success = True
            result.final_answer = execution.answer or "框架已逐项验证所有任务完成条件。"
            result.termination_reason = "verified"
            should_break = True
        if not should_break and (execution.kind == "finish_request" or report_milestone or
                                 (state.queue is not None and state.queue.current is None and execution.kind != "terminal")):
            confirmed, reason = self._verify_finish(state, execution.answer)
            # Repair textual omissions in the reporting phase, not with more
            # browser clicks. One revision is allowed; never weaken the judge.
            coverage = state.final_review_state.get('report', {}).get('coverage', [])
            if (report_required and not confirmed and state.report_delivery.get('status') == 'complete'
                    and state.report_delivery.get('version', 0) < 2 and any(c['status'] == 'failed' for c in coverage)):
                state.report_delivery.update(status='needs_revision', revision_feedback=coverage)
                self._ensure_report(state)
                confirmed, reason = self._verify_finish(state, execution.answer)
            if confirmed:
                if report_milestone:
                    report_review = {**(self._model_final_report or {}), 'plan_step_id': state.queue.current['id'],
                                     'goal': state.queue.current['goal'], 'status': 'passed', 'decision_step': state.step_id}
                    state.queue.accept_review(report_review)
                    state.queue.sync_memory(state.memory)
                    self._publish_review(report_review)
                    self._publish_queue(state)
                    result.trajectory[-1]['execution_queue'] = state.queue.snapshot()
                result.success = True
                result.termination_reason = "verified"
                result.final_answer = execution.answer or "独立核查确认原始任务的全部要求已满足。"
                should_break = True
            elif (report_required and state.report_delivery.get('version', 0) >= 2
                  and any(c.get('status') == 'failed' for c in state.final_review_state.get('report', {}).get('coverage', []))):
                result.termination_reason = 'report_review_failed'
                result.final_answer = '文字结果修订后仍有要求未通过独立核查；保留正文与来源供检查。'
                should_break = True
            elif getattr(self.env, "evaluation_available", False):
                # OSWorld evaluation may run postconfig; invoke it once at episode end.
                result.termination_reason = "evaluation_requested"
                result.final_answer = execution.answer
                should_break = True
            elif (report_required and state.final_review_state.get('report', {}).get('review_scope') == 'text_report'
                  and state.final_review_state.get('report', {}).get('status') == 'unknown'
                  and state.final_review_state.get('report', {}).get('method') != 'code'):
                result.termination_reason = 'completion_inconclusive'
                result.final_answer = '正文与来源已保留，独立语义审查仍无法确认完成；停止对同一文字证据重复判断。'
                should_break = True
            elif report_required and state.final_review_state.get('repeated_unchanged'):
                result.termination_reason = 'completion_inconclusive'
                result.final_answer = '正文与来源已保留，但语义核查仍不确定；相同证据不再重复审查或重新浏览。'
                should_break = True
            elif state.download_facts and state.final_review_state.get('repeated_unchanged'):
                # A repeated finish request cannot create new evidence. Keep
                # the file result explicit and stop an unchanged semantic
                # review from consuming the policy budget or replanning a
                # download that already happened.
                result.termination_reason = 'completion_inconclusive'
                saved = [f['path'] for f in state.download_facts if f.get('status') == 'passed']
                result.final_answer = ('文件下载及完整性已核实：' + '、'.join(saved) + '。' if saved else '') + (
                    '整项任务仍有要求未获独立核查确认；证据未变化，已停止重复结束请求，保留已下载文件。')
                should_break = True
            elif not self.completion.spec.get("checks") and self.verifier_model is None and self.decision_judge is None:
                result.termination_reason = "completion_unverifiable"
                result.final_answer = "执行结束，但系统尚未验证任务完成。模型答复：" + execution.answer
                should_break = True
            else:
                remaining = []
                if state.queue is not None:
                    for s in (state.queue.plan or {}).get('steps', []):
                        if (s.get('status') or '') != 'done':
                            remaining.append(str(s.get('goal', '')).strip())
                remaining = [g for g in remaining if g][:6]
                hint = ('；任务尚未完成，剩余子任务：' + '；'.join(remaining)) if remaining else '；任务尚未完成'
                state.finish_feedback = (reason or '') + hint
                state.memory.add_event(
                    f"request_finish 未通过系统验证：{reason}{hint}"
                    f"。请用 click/type/goto 等动作继续完成剩余子任务，不要再次 request_finish，"
                    f"除非所有子任务都已在页面上真正完成。")
        if execution.done:
            if execution.kind == "terminal":
                result.success = False
                result.termination_reason = "agent_failed"
            elif not result.success:
                result.termination_reason = "environment_ended"
            result.final_answer = execution.answer or result.final_answer
            should_break = True
        # Verification has priority over recovery. Reserve the final decision's
        # aftermath for settlement/verification; a plan with no action slot is useless.
        if not should_break and self._task_time_remaining() <= 0:
            result.termination_reason = "task_timeout"
            should_break = True
        if not should_break and state.memory.control_state == "STALL":
            should_break = self._recover_plan(state, result, step_no)
        # （reflection 已关闭：旧实现调了模型但结果被丢弃，属无效调用，浪费一次推理）

        return state, should_break

    # ================= 内部工具 ================= #
    def _recover_plan(self, state: StepState, result: AgentResult, step_no: int) -> bool:
        if step_no >= self.max_steps:
            return False
        goal = (state.queue.current or {}).get('goal', '') if state.queue else ''
        lesson = f"受阻子任务：{goal}；失败类别：{state.failure_class}；证据：{state.execution_feedback.get('reason') or state.step_review.get('reason') or state.step_progress.get('reason', '')}"
        state.memory.add_failure(lesson[:1000])
        state.memory.record_failed_strategy(lesson[:1000])
        self._publish_diagnostic("framework_recovery", status="requested", failure_class=state.failure_class,
                   failure_count=state.memory.stall_count, failure_key=state.failure_key,
                   step_progress=state.step_progress,
                   remaining_decisions=self.max_steps - step_no)
        repaired = False
        if self._replans < self.max_replans and self._task_time_remaining() > 0:
            self._replans += 1
            repaired = self._plan(state.task, state, repair=True)
        if repaired:
            result.plan = state.memory.plan
            self._clear_stall(state)
            state.memory.control_state = "RECOVER"
            state.memory.add_event("已根据实际失败证据重新规划；继续使用当前环境，不重置任务。")
        else:
            result.trajectory.append({"step": step_no, "early_stop": "持续失败且恢复预算耗尽"})
            result.termination_reason = "task_timeout" if self._task_time_remaining() <= 0 else "stalled"
        self._publish_diagnostic("framework_recovery", status="replanned" if repaired else "exhausted",
                   remaining_replans=max(0, self.max_replans - self._replans))
        return not repaired

    def _fill_prev_assessment(self, result: AgentResult, decision: Decision) -> None:
        """把 decision 的 observed_effect/action_status 回填到上一步动作的 transition。

        时序对齐：decision 里的 observed_effect/action_status 评价的是【上一步动作 a_{t-1}】，
        而不是本步的 next_action。所以把它们回填到最后一条「action 非空、assessment 未填」
        的 transition（即 a_{t-1} 那条），而不是记在本步 transition 里。
        """
        for t in reversed(result.trajectory):
            if t.get("action") and t.get("assessment") is None:
                t["assessment"] = {
                    "observed_effect": decision.observed_effect,
                    "action_status": decision.action_status,
                    "source": "model_annotation",
                    "verified": False,
                }
                break

    def _fill_final_assessment(self, result: AgentResult) -> None:
        """任务结束时补齐最后一步的 assessment。

        最后一步动作 a_N 执行后没有下一步决策来评估它（任务已结束），导致它的
        assessment 缺失——但最后一个关键动作（如 save）恰恰需要后验观察。用任务
        最终得分补上这条 assessment。
        """
        for t in reversed(result.trajectory):
            if t.get("action") and t.get("assessment") is None:
                # 使用框架最终验收；没有可用证据时保留 uncertain，避免生成虚假的失败标签。
                ok = bool(result.success) or result.score >= 1.0
                t["assessment"] = {
                    "observed_effect": ("任务结束，成功" if ok else
                        f"任务结束，最终得分 {result.score}" if result.evaluation_available else
                        "任务结束，缺少可用的最终验收证据"),
                    "action_status": "success" if ok else "failure" if result.evaluation_available else "uncertain",
                    "source": "episode_outcome", "scope": "episode",
                    "verified": result.evaluation_available,
                }
                t["done"] = True
                break

    def _init_state(self, task: str) -> StepState:
        return StepState(
            task=task,
            memory=StructuredMemory(),
            skill_ctx=SkillContext(env=self.env),
        )

    @staticmethod
    def _plan_actions(names):
        return normalize_plan_actions(names)

    @timed_phase("planning")
    def _plan(self, task: str, state: StepState, repair=False) -> bool:
        """Delegate planning; retain one owner for queue and memory installation."""
        if not repair:
            self.planning_calls = []
            from .knowledge_base import load_knowledge
            from .knowledge_selection import select_knowledge, configure_selection_transport
            selection_model = selection_judge = None
            snapshot = None
            started = time.perf_counter()
            try:
                snapshot = load_knowledge()
                if self.knowledge_method == 'decision':
                    selection_model = make_review_model(self.model)
                    if selection_model:
                        selection_model.cfg.max_tokens = 4096
                        selection_model.cfg.timeout = max(.1, min(120, self._task_deadline-time.monotonic())) if self._task_deadline else 120
                        configure_selection_transport(selection_model)
                elif self.knowledge_method == 'jev':
                    cfg = self.decision_judge.config if self.decision_judge else DecisionJudgeConfig()
                    from dataclasses import replace
                    from .model.decision_judge import DecisionJudge
                    selection_judge = DecisionJudge(replace(cfg, url=self.knowledge_jev_url or cfg.url))
                self._knowledge_selection = select_knowledge(task, snapshot, method=self.knowledge_method,
                    model=selection_model, judge=selection_judge,
                    remaining_s=max(0, self._task_deadline-time.monotonic()) if self._task_deadline else None)
                for call in self._knowledge_selection.pop('calls', []):
                    self._record_call(call, actor_role='knowledge_selector')
            except Exception as exc:
                failed_calls = getattr(exc, 'calls', None) or [client.last_call for client in (selection_model, selection_judge) if getattr(client, 'last_call', None)]
                for call in failed_calls:
                    self._record_call(copy.deepcopy(call), actor_role='knowledge_selector')
                self._knowledge_selection = (select_knowledge(task, snapshot, method='all') if snapshot
                                             else {'entries': [], 'method': 'all'})
                self._knowledge_selection.pop('calls', None)
                self._knowledge_selection.update(requested_method=self.knowledge_method, status='fallback',
                    error=type(exc).__name__, latency_ms=round((time.perf_counter()-started)*1000, 2),
                    reason='知识筛选失败，回退为全部启用知识；文件读取失败时按原任务规划')
            finally:
                if selection_model: selection_model.close()
                if selection_judge: selection_judge.close()
            self._publish_diagnostic('knowledge_retrieval', **self._knowledge_selection)

        def accept_plan(plan, call):
            previous = state.queue
            previous_snapshot = previous.snapshot() if previous else None
            state.memory.plan = plan
            normalized = plan['steps']
            state.memory.task_state["goal"] = task
            state.memory.task_state["current_subgoal"] = normalized[0]["goal"]
            state.queue = ExecutionQueue(state.memory.plan, revision=self._replans, previous=previous if repair else None)
            state.progress_monitor = StepProgressMonitor(self.progress_config)
            state.step_progress = {}
            state.queue.sync_memory(state.memory)
            self._emit('plan_revision', revision=self._replans, previous=previous_snapshot,
                       plan=copy.deepcopy(state.memory.plan), queue=state.queue.snapshot(),
                       reason=plan.get('strategy_change', '') or ('依据失败证据调整剩余计划' if repair else '初始计划'))
            self._publish_diagnostic('task_analysis', analysis={**task_analysis(task, self.completion.public_spec),
                'requirement_proposals': state.memory.plan['requirements']})
            self._publish_queue(state)
            if repair:
                self._publish_diagnostic('replan_strategy', strategy_change=plan.get('strategy_change', ''),
                                         revision=self._replans)
            state.memory.add_event(f"已制定任务计划：{len(normalized)} 步。只有系统证据可核实步骤完成。")
            call["valid_plan"] = True
            self._record_call(call, "planner")

        service = PlanningService(self.planner_model, self._task_time_remaining,
                                  self._record_call, self._publish_diagnostic)
        return service.plan(
            task, state, self.completion.public_spec, self.completion.paper_goals(),
            self.planning_calls, getattr(self.env, 'initial_state', {}), accept_plan,
            repair=repair,
            knowledge=getattr(self, '_knowledge_selection', None),
            verification_feedback=self.completion.public_report(state.verification) if repair else None)

    def _publish_review(self, report):
        self._emit("step_verification", report=report, policy_trainable=False)
        if _viz_log_status:
            try:
                _viz_log_status({"event": "step_verification", "run_id": self.run_id, "step": self._current_step, **report})
            except Exception:
                pass

    def _publish_diagnostic(self, event, **payload):
        self._emit(event, **payload, policy_trainable=False)
        if _viz_log_status:
            try:
                _viz_log_status({'event': event, 'step': self._current_step, 'run_id': self.run_id, **payload})
            except Exception:
                pass

    def _track_step_progress(self, state, action, info, before, review=None):
        if state.queue is None:
            return
        if state.progress_monitor is None:
            state.progress_monitor = StepProgressMonitor(self.progress_config)
        report = state.step_review if review is None else review
        step = next((s for s in state.queue.plan['steps'] if s['id'] == report.get('plan_step_id')), state.queue.current)
        state.step_progress = state.progress_monitor.observe(step, report, before, state.obs, action, info, collected_sources=state.report_sources)
        self._publish_diagnostic('step_progress', report=state.step_progress)
        if state.step_progress['status'] == 'stalled':
            state.failure_class = 'step_no_progress'
            state.failure_key = state.step_progress['plan_step_id']
            state.memory.stall_count = state.step_progress['idle_decisions']
            state.memory.add_event(state.step_progress['reason'])

    def _publish_queue(self, state):
        if not state.queue:
            return
        snapshot = state.queue.snapshot()
        self._emit("execution_queue", queue=snapshot, policy_trainable=False)
        if _viz_log_status:
            try:
                _viz_log_status({"event": "execution_queue", "step": self._current_step, "run_id": self.run_id, "queue": snapshot})
            except Exception:
                pass

    def _record_typed_judge(self):
        if self.decision_judge and self.decision_judge.last_call:
            call = copy.deepcopy(self.decision_judge.last_call)
            call['decision_step'] = self._current_step
            self.reviewer.calls.append(call)
            self._record_call(call, 'verifier')
            # Publish the real typed request/response, not the VLM's operator
            # prompt. Authentication headers are never present in last_call.
            self._publish_diagnostic('jev_call', call=call)

    def _review_service(self):
        return ReviewService(ReviewPorts(
            completion=self.completion, decision_judge=self.decision_judge, reviewer=self.reviewer,
            remaining=self._task_time_remaining, record_typed_judge=self._record_typed_judge,
            recheck_jev=self._recheck_jev, publish_review=self._publish_review, record_call=self._record_call))

    def _code_step_review(self, state, step, execution_info, allow_contract=True):
        from .cli_executor import file_check_samples
        if state.obs is not None:
            state.obs.info['cli_checks'] = file_check_samples(self.env, step.get('verification', []), self._task_time_remaining)
        return self._review_service()._code_step_review(state, step, execution_info, allow_contract)

    def _recheck_jev(self, state, step, execution_info, code, typed, before, identity):
        """One observation per subtask, and one additional call only on new evidence."""
        config = self.decision_judge.config
        if (not config.recheck_unknown or typed.get('status') != 'unknown'
                or (typed.get('step_outcome') == 'progress' and not typed.get('progress_hint'))
                or typed.get('action_effect', {}).get('status') == 'passed'
                or typed.get('judgment', {}).get('error_type')):
            return typed
        key = step.get('id', 'final')
        if key in state.judge_rechecks:
            typed['recheck'] = {'status': 'budget_exhausted', 'reason': '本子任务已经补充观察过，继续由执行器恢复或重规划'}
            return typed
        if self._task_time_remaining() <= 0 or (getattr(state.obs, 'info', None) or {}).get('pending_downloads'):
            return typed
        state.judge_rechecks.add(key)
        started = time.monotonic()
        previous = evidence_fingerprint(state.obs, step, execution_info)
        self._publish_diagnostic('judge_recheck', status='observing', plan_step_id=key,
                                 reason='Jev 证据不足，框架补充一次观察；不执行操作、不消耗决策步数')
        fresh = self._observe()
        state.obs = fresh
        self.completion.observe(fresh)
        state.verification = self.completion.evaluate()
        self._emit('observation', observation_id=f'jev_recheck:{identity["review_id"]}',
                   observation=self._observation_record(fresh, f'jev_recheck:{identity["review_id"]}'), policy_trainable=False)
        result = typed
        retry_status, reason = 'unavailable', '补充观察缺少有效截图，保留不确定'
        if fresh.screenshot is not None and not (fresh.info or {}).get('screenshot_stale'):
            if step.get('id') in {'whole_task', 'final'}:
                public = self.completion.public_report(state.verification)
                hard_block = not self._model_review_allowed(state.verification)
                refreshed = self._final_code_evidence(state, state.verification)
                hard_block = hard_block or any(c.get('status') != 'passed'
                    for c in [*refreshed.get('download_checks', []), *refreshed.get('file_checks', [])])
                refreshed['evidence'] = ['fresh_completion_contract']
                for report_key in ('report_delivery', 'observed_sources', 'report_review_instruction',
                                   'file_delivery_evidence', 'file_review_instruction'):
                    if report_key in code:
                        refreshed[report_key] = code[report_key]
            else:
                refreshed, _ = self._code_step_review(state, step, execution_info)
                hard_block = refreshed['status'] == 'failed' or unresolved_desktop_checks(refreshed) or any(
                    p.get('kind') in {'download_complete', 'download_valid', 'artifact_download'} for p in step.get('verification', []))
            if hard_block or (refreshed['status'] == 'passed' and not (step.get('id') in {'whole_task', 'final'} and wants_report(state.task))):
                result = {**refreshed, 'prior_judgment': typed.get('judgment')}
                retry_status, reason = 'code_resolved', '补充观察后由代码核查裁定，不再调用 Jev'
            elif (fresh.info or {}).get('pending_downloads'):
                retry_status, reason = 'pending', '系统报告真实待处理下载，交回框架等待'
            elif self._task_time_remaining() <= 0:
                retry_status, reason = 'deadline', '任务时间已用尽，不再调用 Jev'
            elif evidence_fingerprint(fresh, step, execution_info) == previous:
                retry_status, reason = 'unchanged', '没有新的观察证据，跳过重复模型请求'
            else:
                result = self.decision_judge.review(state.task, step, before, fresh, execution_info, refreshed,
                    remaining_s=self._task_time_remaining(), checkpoints=state.judge_checkpoints)
                if self.decision_judge.last_call:
                    self.decision_judge.last_call['recheck'] = True
                self._record_typed_judge()
                retry_status, reason = 'reviewed', '已根据新证据复核一次；仍不确定时交回执行器恢复'
        result['recheck'] = {'status': retry_status, 'reason': reason, 'observation_count': 1,
                             'model_calls': int(retry_status == 'reviewed')}
        result['model_latency_ms'] = round(typed.get('latency_ms', 0) +
            (result.get('latency_ms', 0) if retry_status == 'reviewed' else 0), 2)
        result['latency_ms'] = round(typed.get('latency_ms', 0) + (time.monotonic()-started)*1000, 2)
        self._publish_diagnostic('judge_recheck', plan_step_id=key, **result['recheck'])
        return result

    def _remember_judge_checkpoint(self, state, step, report):
        if not self.decision_judge:
            return
        item = checkpoint(state.obs, step, report, state.step_id)
        if item is None:
            return
        state.judge_checkpoints = [c for c in state.judge_checkpoints if c['id'].split(':a')[0] != step['id']]
        state.judge_checkpoints.append(item)
        state.judge_checkpoints[:] = state.judge_checkpoints[-4:]
        self._publish_diagnostic('judge_checkpoint', checkpoint={k: item[k] for k in ('id', 'goal', 'decision_step', 'sha256')},
                                 reason='保存阶段观察作为最终核查的历史证据；不替代当前系统验收')

    def _review_visual(self, state, step, execution_info, code, identity, before=None):
        return self._review_service()._review_visual(state, step, execution_info, code, identity, before)

    def _parallel_action_review(self, state, step, execution, action, before, review_execution, effect):
        enabled = (self.decision_judge is not None
            and getattr(getattr(self.decision_judge, 'config', None), 'transition_diagnostics', False)
            and hasattr(self.decision_judge, 'assess_transition') and action is not None
            and execution.kind not in {'terminal', 'finish_request', 'deadline'}
            and self._task_time_remaining() > 0)
        if not enabled:
            report, public = self._code_step_review(state, step, execution.info,
                                                    allow_contract=execution.kind != 'terminal')
            return report, public, None
        # The four questions depend on before/after observations and the local
        # action effect, not the subtask contract result. No other Jev call may
        # start until this future is joined (last_call has a single owner).
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='agent-action-review') as pool:
            future = pool.submit(measured_call, self.decision_judge.assess_transition,
                state.task, step, before, state.obs, review_execution, effect,
                remaining_s=min(12.0, self._task_time_remaining()))
            (report, public), code_ms = measured_call(self._code_step_review,
                state, step, execution.info, allow_contract=execution.kind != 'terminal')
            transition, judge_ms = future.result()
        self._record_typed_judge()
        self._publish_diagnostic('parallel_action_review', plan_step_id=step['id'],
            code_ms=code_ms, judge_ms=judge_ms,
            elapsed_ms=round((time.perf_counter()-started)*1000, 2),
            reason='代码核查与 Jev 四问并行；等待两者结束后才推进队列，代码失败不能被模型覆盖')
        return report, public, transition

    @timed_phase("step_review")
    def _update_plan_progress(self, state: StepState, execution: Execution, before_observation=None, expected_effect='', action=None) -> None:
        """Review after each action, then advance exactly one queue entry on evidence."""
        state.finish_feedback = ""
        step = state.queue.current if state.queue else None
        if step is None:
            step = {"id": "final", "goal": state.task, "expected_evidence": "原始任务的全部要求得到满足",
                    "verification": [{"kind": "visual"}], "check_ids": []}
        identity = {"review_id": f"{self.run_id}:a{state.step_id}:{step['id']}",
                    "plan_step_id": step["id"], "goal": step["goal"], "decision_step": state.step_id}
        self._publish_review({**identity, "method": "code", "status": "running", "reason": "检查当前步骤的系统证据"})
        review_execution = {k: execution.info[k] for k in ('status', 'error_code', 'action', 'dispatched') if k in execution.info}
        if execution.kind == 'cli':
            review_execution.update({k: copy.deepcopy(execution.info[k]) for k in ('channel', 'cli_result', 'effect_verified', 'path', 'destination', 'exit_code') if k in execution.info})
        scroll = execution.info.get('scroll_feedback')
        if isinstance(scroll, dict):
            # Full container diagnostics stay in the trajectory; model review
            # receives only the measured local effect, not an unbounded chain.
            review_execution['scroll_feedback'] = {k: copy.deepcopy(scroll[k]) for k in (
                'available', 'requested_delta', 'actual_delta', 'moved', 'noop',
                'at_top', 'at_bottom', 'target_container') if k in scroll}
        if action is not None:
            review_execution['action'] = action.action
            review_execution['args'] = {k: copy.deepcopy(action.args[k]) for k in (
                'operation', 'path', 'destination', 'command', 'cwd', 'target', 'target_ref', 'element_id', 'text', 'url', 'key', 'keys', 'option', 'submit_key') if k in action.args}
            if execution.info.get('compound_action'):
                review_execution['compound_action'] = copy.deepcopy(execution.info['compound_action'])
            if state.route and state.route.get('target_ref') and 'target_ref' not in review_execution['args']:
                review_execution['args']['target_ref'] = str(state.route['target_ref'])
        review_execution['target_node'] = target_identity(before_observation, review_execution.get('args') or {})
        review_execution['expected_effect'] = expected_effect
        observation_only = execution.kind == 'framework_observation'
        effect = ({'status': 'not_applicable', 'source': 'framework', 'scope': 'observation',
                   'reason': '仅核查已经观察到的状态，本项没有新的动作派发'} if observation_only else
                  effect_review(before_observation, state.obs, review_execution))
        report, public_report, transition = self._parallel_action_review(
            state, step, execution, action, before_observation, review_execution, effect)
        report = {**identity, **report, 'action_effect': effect,
                  'action_scope': 'framework_observation' if observation_only else 'action'}
        if transition is not None:
            answers = transition.get('answers', {})
            model_effect = answers.get('correct', {}).get('label', 'unknown')
            transition['effective_correct'] = (
                'yes' if effect['status'] == 'passed' else
                'no' if effect['status'] == 'failed' else
                'waiting' if effect['status'] == 'pending' else model_effect)
            ax_usable = any(node.get('direct') and node.get('role') in
                            {'button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio'}
                            for block in (state.obs.context or []) if block.get('kind') == 'browser_ax'
                            for node in block.get('nodes', []))
            transition['dom_available'] = ax_usable
            # This question was asked for the goal just reviewed. It must not
            # overwrite the fresh, evidence-bound selection used by build_input.
            state.routing_hint['transition_next_mode'] = answers.get('next_mode', {})
            transition['next_mode_scope'] = 'reviewed_goal_advisory'
            self._publish_diagnostic('jev_transition', decision_step=state.step_id,
                                     plan_step_id=step['id'], transition=transition)
            report['action_transition'] = transition
        identity_pending = (step.get('phase') == 'identify' and self.completion.paper_goals()
                            and any(goal['status'] != 'resolved' for goal in self.completion.paper_goals()))
        if identity_pending and report['status'] == 'unknown':
            report['reason'] = '尚未从官方论文页元数据核实目标身份；继续查找候选，不能凭模型记忆或截图推进'
        self._publish_review(report)
        code_checks = copy.deepcopy(report.get('checks', []))
        if (report["status"] == "unknown" and execution.kind not in {"terminal", "deadline"}
                and not (state.obs.info if state.obs else {}).get("pending_downloads")
                and execution.info.get("status") not in {"pending", "in_progress"}
                and not identity_pending
                and not any(p.get("kind") in {'download_complete', 'download_valid', 'artifact_download'} for p in step.get("verification", []))):
            paper = active_paper(self.completion, step)
            if paper:
                report['bound_paper'] = {k: paper['binding'][k]
                                                    for k in ('full_title', 'canonical_id', 'pdf_url')}
            report = self._review_visual(state, step, review_execution, report, identity, before=before_observation)
        report.setdefault('action_effect', effect)
        if transition is not None:
            report['action_transition'] = transition
        if report['action_effect'].get('method') == 'decision_model' and report['action_effect'].get('status') == 'passed':
            report['action_effect']['evidence_fingerprint'] = evidence_fingerprint(state.obs, step, review_execution)
        report['code_checks'] = copy.deepcopy(report.get('checks', code_checks))
        state.step_review = report
        if state.queue:
            advanced = state.queue.accept_review(report)
            state.queue.sync_memory(state.memory)
            if advanced:
                self._remember_judge_checkpoint(state, step, report)
                self._clear_stall(state)
                for check in report.get('checks', []):
                    state.consumed_downloads.update(check.get('artifact_ids', []))
                if any(p.get('kind') == 'download_valid' for p in step.get('verification', [])):
                    state.consumed_downloads.update(a.get('artifact_id') or a.get('download_id')
                        for a in (state.obs.info or {}).get('artifacts', [])
                        if a.get('completed') and (a.get('artifact_id') or a.get('download_id')))
            self._publish_queue(state)
        state.memory.add_event(f"系统步骤核查[{report['method']}]: {report['status']}，{report['reason']}")
        # No extra policy call is needed for content already proven by code.
        current = state.queue.current if state.queue else None
        if current is not step and (current or {}).get('phase') == 'locate_fulltext':
            # Opening the verified paper often exposes its PDF link in the
            # same observation that completes identity. Review that fact now
            # instead of asking the policy to navigate away and rediscover it.
            pdf_review, _ = self._code_step_review(state, current,
                {'status': 'observed', 'dispatched': False}, allow_contract=False)
            if pdf_review.get('source') == 'official_pdf_link' and pdf_review['status'] == 'passed':
                self._update_plan_progress(state, Execution(kind='framework_observation', obs=state.obs,
                    info={'status': 'observed', 'dispatched': False}))
            current = state.queue.current if state.queue else None
        predicates = (current or {}).get('verification', [])
        if current is not step and predicates and all(p.get('kind') == 'download_valid' for p in predicates):
            if code_review(current, state.obs, {}, public_report)['status'] == 'passed':
                self._update_plan_progress(state, Execution(kind='framework_observation', obs=state.obs,
                    info={'status': 'observed', 'dispatched': False}))
            current = state.queue.current if state.queue else None
        if (current is not step and (current or {}).get('phase') == 'verify'
                and state.verification.get('status') == 'passed'):
            self._update_plan_progress(state, Execution(kind='framework_observation', obs=state.obs,
                info={'status': 'observed', 'dispatched': False}))

    def _programmatic_done(self, state: StepState, execution: Execution) -> str:
        return "contract" if state.verification.get("status") == "passed" else ""

    def _build_memory_text(self, state: StepState) -> str:
        return selected_memory(state.memory, state.step_review)

    def _program_info_text(self, state: StepState) -> str:
        coord = state.last_grounding_coord
        return "\n".join([
            "## 程序信息(program)",
            f"- step_id: {state.step_id}",
            f"- last_action: {state.last_action or '（无，这是第一步）'}",
            f"- last_target: {state.last_target or '（无）'}",
            f"- grounding_coordinate: {list(coord) if coord else '（无）'}",
            f"- execution_status: {state.execution_status or '（无）'}",
            f"- execution_feedback: {json.dumps(state.execution_feedback, ensure_ascii=False)}",
            f"- failure_count: {state.failure_count}",
        ])

    def _build_images(self, state: StepState) -> List[Tuple[str, Any]]:
        history = state.screenshot_history
        n = len(history)
        images: List[Tuple[str, Any]] = []
        if n >= 3:
            images.append(("【上上步截图 S(t-2)，原图】", history[-3]))
        if n >= 2:
            s_t1 = history[-2]
            if state.last_grounding_coord is not None:
                x, y = state.last_grounding_coord
                s_t1 = mark_coordinate(s_t1, x, y)
                images.append(("【上一步截图 S(t-1)，红圈=上一步实际点击位置】", s_t1))
            else:
                images.append(("【上一步截图 S(t-1)，原图】", s_t1))
        images.append(("【当前截图 S(t)，原图】", history[-1]))
        return images

    def _compute_control_state(self, state: StepState, decision: Decision,
                               execution: Execution, before_screenshot, before_observation=None) -> str:
        stall_reason = self._detect_stall(state, decision, execution, before_screenshot, before_observation)
        self._track_step_progress(state, decision.action, execution.info, before_observation or state.obs)
        if state.step_progress.get('status') == 'stalled':
            stall_reason = state.step_progress['reason']
        if stall_reason:
            state.memory.control_state = "STALL"
            sig = self._stable_action_key(decision.action)
            state.memory.add_forbidden_repeat(sig)
            state.memory.add_event(f"⚠️ STALL：{stall_reason}。请依据系统反馈改变策略；任务是否完成由系统验收判断。")
            return "STALL"
        if state.memory.stall_count:
            state.memory.control_state = "RECOVER"
        elif execution.info.get("effect_verified"):
            state.memory.control_state = "ADVANCE"
        else:
            state.memory.control_state = "CONTINUE"
        return state.memory.control_state

    @staticmethod
    def _brief_outcome(execution) -> str:
        """把执行结果精简成一句话，供动作历史使用（避免冗长 JSON 撑爆记忆预算）。"""
        info = execution.info if isinstance(execution.info, dict) else {}
        status = info.get("status")
        reason = str(info.get("reason") or info.get("error") or info.get("error_code") or execution.outcome or "缺少执行反馈")
        if status in {"rejected", "failed", "error"}:
            dispatched = "未下发" if info.get("dispatched") is False else "已下发" if info.get("dispatched") is True else "下发状态未知"
            return f"{status}（{dispatched}）：{reason[:160]}"
        if status == "uncertain":
            return f"结果不确定：{reason[:120]}；需要重新观察并验证，不能直接重复执行"
        if status in {"in_progress", "pending"}:
            return "操作仍在进行，等待框架事件验证，不能重复提交"
        if info.get("action") == "download":
            if info.get("status") == "already_downloaded":
                return "已下载过（跳过）"
            if info.get("verified") or any(a.get("completed") is True for a in info.get("artifacts", []) if isinstance(a, dict)):
                return f"下载已产生文件 {info.get('bytes', '?')}B，任务匹配由系统验收判断"
            return "已请求下载，尚无下载完成证据"
        if info.get("action") == "goto" and status == "executed":
            return f"已打开 {str(info.get('url', ''))[:60]}"
        if status == "executed":
            return "已执行"
        return (execution.outcome or "")[:80]



    def _progress_controller(self):
        return ProgressController(ProgressPorts(
            observation_page=self._observation_page, download_id=self._download_id,
            screen_diff=self._screen_diff, early_stop_repeat=lambda: self.early_stop_repeat))

    def _semantic_action_key(self, action, obs, state=None):
        return self._progress_controller()._semantic_action_key(action, obs, state)

    def _count_failure(self, state, failure_class, action, obs):
        return self._progress_controller()._count_failure(state, failure_class, action, obs)



    def _detect_stall(self, state: StepState, decision: Decision,
                      execution: Execution, before_screenshot, before_observation=None) -> Optional[str]:
        return self._progress_controller()._detect_stall(
            state, decision, execution, before_screenshot, before_observation)

    def _model_review_allowed(self, report):
        # Models may review only genuinely unspecified/semantic requirements.
        # A missing PDF, failed URL, or private check can never be waved through.
        unresolved = {c["id"] for c in self.completion.public_spec.get("checks", [])
                      if c.get("kind") == "unresolved_requirement"}
        checks = report.get("checks", [])
        return all(c.get("status") == "passed" or c.get("id") in unresolved for c in checks)

    def _final_code_evidence(self, state, report):
        """Revalidate episode receipts independently of the current popup/page.

        These facts prove transfer/integrity, never whole-task correctness. In
        particular consumed_downloads prevents a second acquisition from using
        the same file; it must not hide that file from final inspection.
        """
        state.download_facts = self.completion.download_facts()
        public = self.completion.public_report(report)
        code = {'method': 'code', 'status': report.get('status', 'unknown'),
                'reason': public.get('reason', '明确条件已检查，剩余语义要求需独立核查'),
                'checks': public.get('checks', [])}
        # Only terminal-step file requirements are final invariants; intermediate
        # files may intentionally be renamed/removed by later steps.
        steps = (getattr(state.memory, 'plan', None) or {}).get('steps', [])
        predicates = []
        from .cli_executor import cli_capability
        cap = cli_capability(self.env)
        relocations = getattr(self, '_cli_relocations', {})
        for item in steps:
            for original in item.get('verification', []):
                if original.get('kind') not in {'file_exists', 'file_absent', 'file_content', 'files_equal'}:
                    continue
                predicate = copy.deepcopy(original)
                # Only earlier milestones follow framework-observed moves.
                # Explicit final path requirements are never rewritten.
                if item.get('phase') not in {'verify', 'final'} and predicate['kind'] != 'file_absent' and cap['target'] == 'local':
                    for key in ('path', 'other_path'):
                        if key not in predicate:
                            continue
                        path = os.path.abspath(os.path.join(cap['workspace'], predicate[key]))
                        seen = set()
                        while path in relocations and path not in seen:
                            seen.add(path)
                            path = relocations[path]
                        if seen:
                            predicate[key] = path
                # Later explicit conditions on a path supersede intermediate
                # existence/content states, e.g. intentional deletion.
                family = 'presence' if predicate['kind'] in {'file_exists', 'file_absent'} else predicate['kind']
                predicates = [p for p in predicates if not (p.get('path') == predicate['path'] and
                    ('presence' if p['kind'] in {'file_exists', 'file_absent'} else p['kind']) == family)]
                predicates.append(predicate)
        if predicates and state.obs is not None:
            from .cli_executor import file_check_samples
            state.obs.info['cli_checks'] = file_check_samples(self.env, predicates, self._task_time_remaining)
            file_report = code_review({'verification': predicates}, state.obs, {}, {})
            code['file_checks'] = file_report['checks']
            code['checks'] = [*code['checks'], *file_report['checks']]
        if state.download_facts:
            code['download_facts'] = copy.deepcopy(state.download_facts)
            code['download_evidence_scope'] = (
                '来自本轮浏览器完成事件，并在本次验收重新读取真实文件核验。'
                'status=passed 已证明该文件下载完成且未改变；弹窗关闭、换页或截图看不到下载提示不能否定该事实。'
                '若交付物是ZIP，应结合包内已校验成员与contained_formats判断格式，不要求网站必须直接交付裸PPT/PDF。'
                '这不证明对象身份、来源区域、数量、命名等全部任务要求满足；仅核对尚未证明的要求。')
        # Generic tasks have no complete deterministic contract, but their
        # compiled file predicates still cannot be waived by a visual model.
        # Inspect all required formats even if a navigation goal is stuck at
        # the front of the queue; repeated inspections do not need new files.
        formats = {p.get('format', 'any')
            for item in (getattr(state.memory, 'plan', None) or {}).get('steps', [])
            for p in item.get('verification', []) if p.get('kind') == 'artifact_download'}
        if formats:
            from .artifact_download import verified_download
            valid = [f for f in state.download_facts if f.get('status') == 'passed']
            code['download_checks'] = []
            for fmt in sorted(formats):
                artifact = verified_download(valid, format=fmt)
                reason = ('已核实本轮实际下载文件及所需格式（允许已验证的ZIP内成员）' if artifact else
                    '尚无所需格式的有效下载文件；已有交付物：' +
                    ', '.join(f.get('format', 'unknown') for f in valid) +
                    '。请检查详情页附件或选择正确文件；若是打包ZIP，必须实际包含所需格式，不要重复请求结束。')
                code['download_checks'].append({'kind': 'artifact_download', 'format': fmt,
                    'status': 'passed' if artifact else 'unknown', 'reason': reason,
                    'path': artifact['path'] if artifact else None})
        return code

    def _finish_evidence_fingerprint(self, obs, facts):
        base = self._review_fingerprint(obs)
        if not facts:
            return base
        return hashlib.sha256((base + json.dumps(facts, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()

    @staticmethod
    def _download_proof_invalidated(previous, current):
        """A model cannot restore an accepted file that was removed or changed.

        Only files actually present in the previous review's passed receipts
        participate. An unrelated earlier failed download does not block a
        later valid file; a verified content-preserving rename can be reviewed
        again for its new filename without being treated as corrupted bytes.
        """
        now = {f['artifact_id']: f for f in current}
        return any(f.get('status') == 'passed' and (
            f['artifact_id'] not in now or now[f['artifact_id']].get('status') != 'passed'
            or any(now[f['artifact_id']].get(key) != f.get(key)
                   for key in ('sha256', 'bytes', 'file_identity')))
            for f in previous)

    @timed_phase("final_review")
    def _verify_finish(self, state: StepState, answer: str) -> Tuple[bool, str]:
        from .report_delivery import wants_report
        report_required = wants_report(state.task)
        if report_required and state.report_delivery.get('status') != 'complete':
            return False, '信息整理结果尚未生成完成，不能验收通过'
        report = self.completion.evaluate()
        state.verification = report
        self._verification = copy.deepcopy(report)
        if report.get("status") == "passed" and not report_required:
            return True, "全部确定性验收条件通过"
        if not self._model_review_allowed(report):
            public = self.completion.public_report(report)
            return False, public.get("reason") or "确定性验收条件尚未满足，模型不能覆盖"
        code = self._final_code_evidence(state, report)
        if report_required:
            code['report_delivery'] = copy.deepcopy(state.report_delivery)
            code['report_review_instruction'] = '正文是待核查交付物，不是完成证据；依据 observed_sources 核对覆盖原始要求、事实与引用，不以当前截图是否展示正文来判断。'
        missing_download = next((c for c in [*code.get('download_checks', []), *code.get('file_checks', [])] if c['status'] != 'passed'), None)
        if missing_download:
            self._publish_review({**code, 'plan_step_id': 'whole_task', 'decision_step': state.step_id})
            return False, missing_download['reason']
        if self._model_final_report and self._download_proof_invalidated(
                self._reviewed_download_facts, state.download_facts):
            self._model_final_report = None
            return False, '已接受验收所依据的下载文件丢失或改变，原成功结论失效'
        from .report_review import text_only_task, delivery_digest
        text_review = report_required and text_only_task(state.task, code)
        fingerprint = (delivery_digest(state.task, state.report_delivery) if text_review else
                       self._finish_evidence_fingerprint(state.obs, state.download_facts))
        previous = state.final_review_state
        if ((state.download_facts or text_review) and previous.get('fingerprint') == fingerprint
                and previous.get('report', {}).get('status') == 'unknown'):
            previous['repeated_unchanged'] = True
            self._publish_diagnostic('finish_review_unchanged',
                reason='文字与来源快照未变化，不重复验收或重新浏览' if text_review else '下载收据与页面证据未变化，不重复模型验收或重新规划下载',
                download_facts=state.download_facts)
            return False, previous['report']['reason']
        state.final_review_state = {'repeated_unchanged': False}
        step = {"id": "whole_task", "goal": state.task,
                "expected_evidence": "核对原始任务的所有要求（包含数量、对象身份、保存或提交结果）；不能只核对当前步骤或规划摘要。",
                "planner_review_hint": str((getattr(state.memory, 'plan', None) or {}).get('final_check') or '')[:2000],
                "unresolved_requirements": [c['text'] for c in self.completion.public_spec.get('checks', [])
                                            if c.get('kind') == 'unresolved_requirement'],
                "verification": [{"kind": "visual"}]}
        if state.download_facts:
            step['expected_evidence'] += (
                '代码提供的 download_facts 是当前重新验证的真实文件证据：passed 文件的下载完成与完整性已经确认。'
                '不能因下载弹窗已关闭或当前截图不显示下载记录而重新判定未下载；继续核对原任务其余要求。')
        identity = {"review_id": f"{self.run_id}:a{state.step_id}:whole_task",
                    "plan_step_id": "whole_task", "goal": state.task, "decision_step": state.step_id}
        code = {**identity, **code}
        submitted_facts = copy.deepcopy(code.get('download_facts', []))
        self._publish_review(code)
        reviewed = self._review_visual(state, step, {}, code, identity)
        state.download_facts = self.completion.download_facts()
        if reviewed['status'] == 'passed' and self._download_proof_invalidated(submitted_facts, state.download_facts):
            reviewed = {**identity, 'method': 'code', 'status': 'failed',
                'reason': '送审时有效的下载文件在核查期间丢失或改变，模型结论不能覆盖文件校验失败',
                'checks': [], 'download_facts': copy.deepcopy(state.download_facts), 'evidence': []}
            self._publish_review(reviewed)
        self._model_final_report = reviewed if reviewed["status"] == "passed" and reviewed.get("method") != "code" else None
        self._reviewed_state = state
        # Never mark facts appearing only after the model call as reviewed.
        self._reviewed_download_facts = submitted_facts
        receipt_scope = named_download_receipts(state.task, submitted_facts) if self._model_final_report else []
        if receipt_scope:
            self._model_final_report['revalidation_scope'] = 'named_download_receipts'
            self._model_final_report['receipt_ids'] = receipt_scope
        self._reviewed_observation = (delivery_digest(state.task, state.report_delivery) if text_review else
                                      self._finish_evidence_fingerprint(state.obs, submitted_facts))
        state.final_review_state = {'fingerprint': self._reviewed_observation,
            'report': copy.deepcopy(reviewed), 'repeated_unchanged': False}
        return reviewed["status"] == "passed", reviewed["reason"]

    def _ensure_report(self, state):
        from .report_delivery import generate_report
        state.report_delivery = generate_report(state, self.model, self._publish_diagnostic,
            self._record_call, self._task_time_remaining)
        self._report_delivery = state.report_delivery

    @classmethod
    def _review_fingerprint(cls, obs):
        if obs is None:
            return ""
        return hashlib.sha256((encode_image(obs.screenshot) + json.dumps(
            {"context": cls._semantic_observation(obs), "text": obs.text,
             "runtime": (obs.info or {}).get("runtime_state"),
             "pending_downloads": (obs.info or {}).get("pending_downloads"),
             "artifacts": (obs.info or {}).get("artifacts")}, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()

    @staticmethod
    def _screen_diff(a, b) -> float:
        """比较两张截图差异（64x64 灰度 mean-abs-diff，0~255 尺度，越小越相似）。

        纯 PIL 实现，不依赖 numpy。
        """
        if a is None or b is None:
            return 255.0
        try:
            from PIL import ImageChops
            ia = a.convert("L").resize((64, 64)) if hasattr(a, "convert") else None
            ib = b.convert("L").resize((64, 64)) if hasattr(b, "convert") else None
            if ia is None or ib is None:
                return 255.0
            hist = ImageChops.difference(ia, ib).histogram()
            return sum(i * count for i, count in enumerate(hist)) / (64 * 64)
        except Exception:  # noqa: BLE001
            return 255.0

    @staticmethod
    def _format_code_agent_result(result: Dict[str, Any]) -> str:
        lines = [
            f"- 任务: {result.get('task_instruction', '')}",
            f"- 执行步数: {result.get('steps_executed', 0)}/{result.get('budget', 0)}",
            f"- 完成状态: {result.get('completion_reason', '')}",
            f"- 摘要: {result.get('summary') or '（无）'}",
        ]
        notes = result.get("notes") or {}
        if notes:
            lines.append("- 子Agent 存下的笔记:")
            for k, v in notes.items():
                lines.append(f"  - {k}: {v}")
        hist = result.get("execution_history") or []
        if hist:
            shown = hist[:3] + (hist[-2:] if len(hist) > 3 else [])
            lines.append("- 关键执行历史（前3+后2步）:")
            for h in shown:
                lines.append(f"  - [{h.get('type')}] {h.get('code', '')[:80]} → {h.get('result', '')[:80]}")
        return "\n".join(lines)

    def _update_program_info(self, action: Action, execution: Execution, state: StepState) -> None:
        state.last_action = action.action
        state.last_target = self._target_of(action)
        if execution.kind == "finish_request":
            state.execution_status = "request_finish"
        elif execution.kind == "terminal":
            state.execution_status = "fail"
        else:
            state.execution_status = str(execution.info.get("status") or "unknown")
        state.execution_feedback = {key: copy.deepcopy(execution.info[key]) for key in (
            "cli_result", "status", "dispatched", "reason", "error", "error_code", "wait_error_code", "wait_reason", "effect_verified", "framework_wait", "scroll_feedback", "page_readiness_wait")
            if key in execution.info}
        state.execution_feedback.setdefault("status", state.execution_status)
        state.execution_feedback.setdefault("dispatched", None)
        state.execution_feedback.setdefault("reason", execution.info.get("error") or execution.info.get("error_code")
                                            or execution.outcome[:200])

    def _validate_action(self, action: Action) -> Optional[str]:
        from .skills import is_skill, validate_skill
        if is_skill(action.action):
            return validate_skill(action.action, action.args)
        if action.action not in BASIC_ACTIONS:
            return "只允许已注册的 GUI/DOM/CLI 动作"
        if self._active_actions is not None and action.action not in self._active_actions:
            return "本轮可用动作：" + ", ".join(self._active_actions)
        return validate_action(action)

    def _locate_recorded(self, screenshot, target):
        previous = getattr(self.grounding, 'last_calls', None)
        if isinstance(previous, list):
            previous.clear()
        try:
            return self.grounding.locate(screenshot, target)
        finally:
            for call in getattr(self.grounding, 'last_calls', []):
                self._record_call(copy.deepcopy(call), 'grounder')

    def _resolve(self, action: Action, obs: Observation) -> Action:
        spec = get_action_spec(action.action)
        if spec is None or spec.resolve == "none":
            return action
        # grounding 坐标空间 = 截图尺寸：浏览器场景截图是 viewport（CDP 截），桌面是全屏，
        # 这里按截图实际尺寸动态适配 grounding 的 width/height，坐标自然落在正确空间。
        if obs.screenshot is not None:
            try:
                w, h = obs.screenshot.size
                if hasattr(self.grounding, "width"):
                    self.grounding.width = int(w)
                    self.grounding.height = int(h)
            except Exception:  # noqa: BLE001
                pass
        # Visual grounding cannot validate DOM identities. Keep the semantic
        # operands for downstream evidence, but never carry an abandoned ref/id
        # into the resolved action. The model's original action is unchanged.
        extra = copy.deepcopy({k: v for k, v in action.args.items()
                               if k not in {"target_ref", "element_id"}})
        if spec.resolve == "point":
            gr = self._locate_recorded(obs.screenshot, str(action.args.get("target", "")))
            coord = resolve_coords(gr)
            if coord is None:
                # UI-TARS 失败：尝试 OCR 文字定位兜底（对齐 AgentS3 的第二条 grounding 腿）
                coord = self._ocr_fallback(obs.screenshot, str(action.args.get("target", "")))
            if coord is None:
                # grounding 失败：不带可执行坐标，由执行器拒绝（空转），不做坐标钳制
                return Action(action.action, {**extra, "grounding_failed": True})
            x, y = coord
            return Action(action.action, {**extra, "x": x, "y": y})
        if spec.resolve == "select":
            gr = self._locate_recorded(obs.screenshot, str(action.args.get("target", "")))
            coord = resolve_coords(gr)
            if coord is None:
                coord = self._ocr_fallback(obs.screenshot, str(action.args.get("target", "")))
            if coord is None:
                return Action(action.action, {**extra, "grounding_failed": True})
            x, y = coord
            return Action(action.action, {**extra, "x": x, "y": y})
        if spec.resolve == "drag":
            g1 = self._locate_recorded(obs.screenshot, str(action.args.get("from_target", "")))
            g2 = self._locate_recorded(obs.screenshot, str(action.args.get("to_target", "")))
            c1, c2 = resolve_coords(g1), resolve_coords(g2)
            if c1 is None or c2 is None:
                return Action(action.action, {**extra, "grounding_failed": True})
            return Action(action.action, {**extra, "x1": c1[0], "y1": c1[1], "x2": c2[0], "y2": c2[1]})
        return action

    def _ocr_fallback(self, screenshot, target: str):
        """确定性的 OCR 兜底：仅定位归一化后唯一精确匹配的词或同行连续词组。

        不调用决策模型，也不生成定位训练样本。缺少依赖、OCR 失败、描述无法
        精确匹配或文本重复时返回 None，由正常决策循环重新观察和选择动作。
        """
        try:
            import unicodedata
            import pytesseract
            from PIL import Image
            from io import BytesIO
        except ImportError:
            return None

        def normalize(text):
            return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())

        needle = normalize(target or "")
        if not needle:
            return None

        try:
            img = screenshot
            if isinstance(screenshot, (bytes, bytearray)):
                img = Image.open(BytesIO(screenshot))
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            words = []
            for i, text in enumerate(data.get("text") or []):
                text = normalize(text or "")
                if not text:
                    continue
                left, top, width, height = (int(data[key][i]) for key in ("left", "top", "width", "height"))
                if left < 0 or top < 0 or width <= 0 or height <= 0:
                    return None
                words.append({
                    "text": text, "left": left, "top": top,
                    "right": left + width, "bottom": top + height,
                    "line": tuple(data[key][i] if key in data else None
                                  for key in ("page_num", "block_num", "par_num", "line_num")),
                    "word_num": int(data["word_num"][i]) if "word_num" in data else None,
                })
        except Exception:  # noqa: BLE001
            return None

        matches = []
        for start, first in enumerate(words):
            phrase = ""
            for end in range(start, len(words)):
                word = words[end]
                if word["line"] != first["line"]:
                    break
                if end > start and word["word_num"] is not None and (
                        word["word_num"] != words[end - 1]["word_num"] + 1):
                    break
                phrase = f"{phrase} {word['text']}".strip()
                if phrase == needle:
                    match = words[start:end + 1]
                    left, right = min(w["left"] for w in match), max(w["right"] for w in match)
                    top, bottom = min(w["top"] for w in match), max(w["bottom"] for w in match)
                    matches.append(((left + right) // 2, (top + bottom) // 2))
                    if len(matches) > 1:
                        return None
                    break
                if not needle.startswith(phrase + " "):
                    break
        return matches[0] if matches else None

    def _final_code_snapshot(self):
        return self.completion.evaluate(), self.completion.download_facts()

    def _parallel_final_snapshot(self):
        # The browser stays on its owning execution path. File checks run in a
        # worker; completion.observe() cannot mutate their ledger until joined.
        # These are preliminary checks only: the post-observation checks below
        # still catch changes/deletion during capture and callback execution.
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='agent-final-check') as pool:
            future = pool.submit(measured_call, self._final_code_snapshot)
            fresh, observation_ms = measured_call(self._observe)
            (report, facts), code_ms = future.result()
        self._publish_diagnostic('parallel_final_evidence', code_ms=code_ms,
            observation_ms=observation_ms,
            elapsed_ms=round((time.perf_counter()-started)*1000, 2),
            reason='最终观察与文件预检并行；观察合入后仍重新检查文件与验收条件')
        return report, facts, fresh

    @timed_phase("final_revalidation")
    def _evaluate(self, result: AgentResult) -> float:
        def refresh_final_pages(observation=None):
            checks = self.completion.spec.get("checks", [])
            if not any(check.get("scope") == "final" and check.get("kind") in {
                    "url_equals", "text_present", "element_state"} for check in checks):
                return False
            # Evaluators, callbacks and redirects can change the page after the
            # last action observation. Final page predicates need fresh evidence;
            # milestones stay latched and artifact-only contracts avoid this call.
            if observation is None:
                observation = self._observe()
            self.completion.observe(observation)
            self._emit("observation", observation_id="o_final", stage="final_evaluation",
                       observation=self._observation_record(observation, "o_final"))
            return True

        if not getattr(self.env, "evaluation_available", False):
            fresh = None
            fresh_applied = False
            if self._model_final_report and self._model_final_report.get('review_scope') != 'text_report':
                report, download_facts, fresh = self._parallel_final_snapshot()
                # Final page predicates still require the newly captured page,
                # even when a failed file proof prevents another model review.
                fresh_applied = refresh_final_pages(fresh)
                if fresh_applied:
                    report = self.completion.evaluate()
            else:
                refresh_final_pages()
                report, download_facts = self._final_code_snapshot()
            if download_facts:
                report['download_facts'] = download_facts
                if result.termination_reason == 'completion_inconclusive':
                    report['reason'] = '文件结果已单独核实，整任务语义验收仍不确定；没有新的证据，停止重复结束请求'
            was_success = result.success
            model_report = self._model_final_report
            if model_report and self._download_proof_invalidated(self._reviewed_download_facts, download_facts):
                model_report = self._model_final_report = None
                report['reason'] = '最终模型验收所依据的下载文件已丢失或改变，原成功结论失效'
            if model_report and self._model_review_allowed(report) and (report.get("status") != "passed" or getattr(self, "_report_required", False)):
                # Observe again after callbacks; revoke/review if visible evidence changed.
                state = self._reviewed_state
                if model_report.get('review_scope') == 'text_report':
                    from .report_review import delivery_digest
                    if delivery_digest(state.task, state.report_delivery) != model_report.get('evidence_digest'):
                        confirmed, _ = self._verify_finish(state, "")
                        model_report = self._model_final_report if confirmed else None
                    else:
                        self._publish_diagnostic('final_review_reused',
                            reason='文字正文与已观察来源快照未变，复用已通过的语义审查；动态页面变化不使文字交付失效',
                            evidence_digest=model_report['evidence_digest'], review_scope='text_report')
                else:
                    if fresh is None:
                        fresh = self._observe()
                    if not fresh_applied:
                        self.completion.observe(fresh)
                        report = self.completion.evaluate()
                    if facts := self.completion.download_facts():
                        report['download_facts'] = facts
                    if self._download_proof_invalidated(self._reviewed_download_facts, report.get('download_facts', [])):
                        model_report = self._model_final_report = None
                        report['reason'] = '补充最终观察时下载文件已丢失或改变，原成功结论失效'
                    elif not self._model_review_allowed(report):
                        model_report = None
                    elif (model_report.get('revalidation_scope') == 'named_download_receipts'
                          and set(model_report.get('receipt_ids', [])) == set(named_download_receipts(state.task, report.get('download_facts', [])))
                          and report.get('download_facts', []) == self._reviewed_download_facts):
                        # The original whole-task model review is retained, not promoted
                        # to deterministic success. Code re-read the same file above.
                        # Notification disappearance is unrelated to this durable result.
                        self._publish_diagnostic('final_review_reused',
                            reason='指定文件的收据与内容未变，复用已通过的整任务语义核查；文件仍由代码重新检查',
                            receipt_ids=model_report['receipt_ids'])
                    elif self._finish_evidence_fingerprint(fresh, report.get('download_facts', [])) != self._reviewed_observation:
                        state.obs = fresh
                        confirmed, _ = self._verify_finish(state, "")
                        model_report = self._model_final_report if confirmed else None
                if model_report:
                    result.success = True
                    result.evaluation_available = False
                    result.reward_source = "independent_model_review"
                    result.termination_reason = "verified_by_model"
                    result.verification = {**copy.deepcopy(model_report), "source": "independent_model_review",
                                           "reward_valid": False, "completion": report}
                    self._emit("verification", stage="final_evaluation", report=result.verification, policy_trainable=False)
                    return 0.0
            if getattr(self, '_report_required', False) and not model_report:
                report = {**report, 'status': 'unknown', 'reason': '文字交付物尚未通过独立核查；网页或文件条件通过不能代替报告验收'}
            result.success = report.get("status") == "passed"
            result.verification = copy.deepcopy(report)
            if result.success and not was_success:
                result.termination_reason = "verified"
                result.final_answer = "框架已核实全部任务条件；任务状态满足要求。"
            if was_success and not result.success:
                result.termination_reason = "verification_changed"
                result.final_answer = "最终验收未通过，任务完成证据已失效。"
            result.evaluation_available = bool(self.completion.spec.get("checks")) and report.get("status") in {"passed", "failed"}
            result.reward_source = "completion_contract" if result.evaluation_available else "unavailable"
            self._emit("verification", stage="final_evaluation", report=result.verification)
            return 1.0 if result.success and report.get("status") == "passed" else 0.0
        try:
            score = float(self.env.evaluate())
            import math
            if not math.isfinite(score):
                raise ValueError("non-finite evaluator score")
            refresh_final_pages()
            contract = self.completion.evaluate()
            has_contract = bool(self.completion.spec.get("checks"))
            environment_passed = score >= 1.0
            status = ("passed" if environment_passed else "failed")
            if has_contract:
                if contract.get("status") == "failed":
                    status, score = "failed", 0.0
                elif environment_passed and contract.get("status") != "passed":
                    status, score = "unknown", 0.0
            result.evaluation_available = status != "unknown"
            result.reward_source = ("combined_evaluation" if has_contract else "environment_evaluator") if result.evaluation_available else "unavailable"
            result.success = status == "passed"
            result.verification = {"status": status, "source": result.reward_source, "score": score,
                "environment_passed": environment_passed,
                **({"completion": contract, "checks": contract.get("checks", [])} if has_contract else {})}
            if result.success:
                result.termination_reason = "verified_by_environment"
            elif status == "unknown":
                result.termination_reason = "completion_unverifiable"
                result.final_answer = "环境评测通过，但附加任务条件尚无足够证据，无法确认全部完成。"
            elif result.termination_reason in {"evaluation_requested", "verified"}:
                result.termination_reason = "evaluation_failed"
                result.final_answer = "独立评测未通过，任务尚未完成。"
            self._emit("verification", stage="final_evaluation", report=result.verification)
            return score
        except NotImplementedError:
            result.success = False
            result.evaluation_available = False
            result.reward_source = "unavailable"
            result.termination_reason = "evaluation_error"
            result.verification = {"status": "unknown", "source": "environment_evaluator", "reason": "not implemented"}
            result.final_answer = "独立评测不可用，无法确认任务完成。"
            self._emit("verification", stage="final_evaluation", report=result.verification)
            return 0.0
        except Exception as exc:  # noqa: BLE001
            # 评测器自身失败（如 postconfig 要下载的评测脚本被网络/代理拦掉）不应毁掉整次运行：
            # agent 的实际操作已经完成，把评测异常记入轨迹、按 0 分返回，保证 result/轨迹正常落盘。
            result.trajectory.append({
                "step": result.steps,
                "eval_error": f"评测阶段异常（非 agent 操作失败）：{str(exc)[:500]}",
            })
            result.success = False
            result.evaluation_available = False
            result.reward_source = "unavailable"
            result.termination_reason = "evaluation_error"
            result.verification = {"status": "unknown", "source": "environment_evaluator", "reason": type(exc).__name__}
            result.final_answer = "独立评测失败，无法确认任务完成。"
            self._emit("verification", stage="final_evaluation", report=result.verification)
            return 0.0

    # ---- 纯静态小工具 ----






    _action_desc = staticmethod(pipeline_format._action_desc)

    _compact_action = staticmethod(pipeline_format._compact_action)

    _coordinate_of = staticmethod(pipeline_format._coordinate_of)

    _describe_outcome = staticmethod(pipeline_format._describe_outcome)

    _operation_mode = staticmethod(pipeline_format._operation_mode)

    _target_of = staticmethod(pipeline_format._target_of)

    _stable_action_key = staticmethod(progress_controller._stable_action_key)

    _normalize_target = staticmethod(progress_controller._normalize_target)

    _clear_stall = staticmethod(progress_controller._clear_stall)

    _semantic_observation = staticmethod(progress_controller._semantic_observation)
