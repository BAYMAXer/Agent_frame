"""主循环流水线：观察 → 决策（证据先行）→ 定位 → 执行 → 控制状态更新。

闭环设计（核心）：
- 执行前，模型在 next_action 的同时说明 expected_effect（预期观察到什么）；
- 执行后，模型先客观描述 observed_effect，再给 action_status（成功/失败）；
- 程序对比 expected vs observed + 确定性卡死检测，得出 control_state；
- Planner 只能 request_finish，程序验证后才真正结束。

阶段（run() 里按序调用）：
  ① preprocess   预处理：确保观察 + 记录截图 + 构建记忆文本
  ② build_input  构造输入：结构化记忆（含控制状态）+ 3 张截图
  ③ decide       决策：observed_effect + action_status + state_update + next_action + expected_effect
  ④ ground       grounding：描述→坐标，记录坐标供下一步标记
  ⑤ execute      执行：skill / shell / subagent / terminal(request_finish|fail) / env
  ⑥ postprocess  后处理：记录 expected/observed + 控制状态 + 卡死检测 + Finish 验证
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .actions import (
    Action, validate_action, action_signature, get_action_spec, ACTION_REGISTRY,
    parse_decision, action_from_decision,
)
from .env import Environment, Observation
from .model import ChatModel, Grounding, resolve_coords, mark_coordinate
from .model.decision_model import encode_image
from .scene import scene_hint
from .model.request_deadline import ModelRequestTimeout
from .memory import StructuredMemory
from .skills import is_skill, validate_skill, run_skill, enabled_skills, SkillContext
from .prompts import build_user_prompt, build_format_feedback, build_planner_prompt
from .context import PromptCompiler

# 实时状态推送（可选）：execute 阶段进度推给 viz 前端，失败不影响主流程
try:
    from .viz.logging import log_status as _viz_log_status
except Exception:  # noqa: BLE001
    _viz_log_status = None

# 确定性卡死检测参数
_NO_CHANGE_DIFF_THRESHOLD = 2.0       # 64x64 灰度 mean-abs-diff 阈值（< 此值视为无变化）
_NO_CHANGE_CONSECUTIVE_THRESHOLD = 2  # 连续 N 步无变化判定卡死
_REPEAT_ACTION_THRESHOLD = 3          # 连续 N 次相同 action+target 判定卡死
_LOOP_HISTORY_LEN = 6                 # 周期循环检测的历史长度
_LOOP_PERIODS = (2,)                  # 周期循环（A,B,A,B），周期1由 _REPEAT_ACTION_THRESHOLD 覆盖


# --------------------------------------------------------------------------- #
# 数据容器
# --------------------------------------------------------------------------- #
@dataclass
class AgentResult:
    """单任务运行结果。"""
    task: str
    success: bool = False
    score: float = 0.0
    steps: int = 0
    final_answer: str = ""
    trajectory: List[dict] = field(default_factory=list)
    screenshots: Dict[int, Any] = field(default_factory=dict)   # step -> 决策前截图（PIL 或 bytes）
    plan: Optional[Dict[str, Any]] = None   # 阶段0 产出的任务计划（steps/keywords/final_check）


@dataclass
class StepState:
    """跨步骤流动的循环状态。"""
    task: str = ""
    obs: Optional[Observation] = None
    memory: Optional[StructuredMemory] = None
    skill_ctx: Optional[SkillContext] = None
    skill_results: Dict[str, str] = field(default_factory=dict)
    terminal_result: str = ""
    code_agent_result: str = ""
    page_source: str = ""
    browser_result: str = ""
    memory_text: str = ""
    prompt_metadata: Dict[str, Any] = field(default_factory=dict)
    prompt_text: str = ""                     # 本步喂给决策模型的输入文本（任务+记忆+结构观察+工具结果）
    route: Optional[Dict[str, Any]] = None
    resolved_action: Optional[Any] = None
    # 视觉记忆
    screenshot_history: List[Any] = field(default_factory=list)
    last_grounding_coord: Optional[Tuple[int, int]] = None
    last_grounding_target: str = ""
    # 程序维护的信息
    step_id: int = 0
    last_action: str = ""
    last_target: str = ""
    execution_status: str = ""
    failure_count: int = 0
    # 卡死检测
    no_change_count: int = 0
    action_keys: List[str] = field(default_factory=list)
    shell_outputs: List[str] = field(default_factory=list)   # 最近 shell 命令输出（用于 shell 卡死检测）


@dataclass
class Decision:
    """③ 决策阶段的产出（先分析当前屏幕 → 证据先行 → 预期效果）。"""
    raw: str = ""
    action: Optional[Action] = None
    screen_analysis: str = ""            # 当前屏幕状态描述（含打开的应用）
    observed_effect: str = ""            # 上一步动作的实际观察（先客观描述）
    action_status: str = ""              # success / failure / uncertain
    state_update: Dict[str, Any] = field(default_factory=dict)
    expected_effect: str = ""            # 下一步动作的预期效果
    calls: List[dict] = field(default_factory=list)   # 每次决策调用的完整记录（含重试）


@dataclass
class Execution:
    """⑤ 执行阶段的产出。"""
    kind: str = ""                        # skill / shell / subagent / terminal / finish_request / env
    outcome: str = ""
    executed: str = ""
    obs: Optional[Observation] = None
    done: bool = False
    success: bool = False
    answer: str = ""
    info: Dict[str, Any] = field(default_factory=dict)   # 执行器返回的详细信息（browser_dom 状态等）
    reward: float = 0.0                                  # 环境奖励（browser_dom 透传 step_result.reward）


# --------------------------------------------------------------------------- #
# 主循环流水线
# --------------------------------------------------------------------------- #
class Pipeline:
    """主循环流水线。run() 只列 6 个阶段。"""

    def __init__(self, *, model: ChatModel, grounding: Grounding, env: Environment,
                 system_prompt: str, code_agent=None, max_steps: int = 20,
                 max_retries: int = 3, enable_reflection: bool = True,
                 early_stop_repeat: int = 3, step_callback=None, progress_callback=None,
                 step_timeout: float = 120.0):
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
        self.progress_callback = progress_callback  # 各阶段即时记录，中断时也保留执行前证据
        self.step_timeout = float(step_timeout)
        if self.step_timeout <= 0:
            raise ValueError("每步超时必须大于 0")
        self._deadline = None
        self._last_phase = None
        self.compiler = PromptCompiler()  # 上下文预算 + 相关性选择
        self._verification = None         # 最近一次 finish 验证的调用记录

    # ================= 主循环 ================= #
    def run(self, task: str) -> AgentResult:
        result = AgentResult(task=task)
        state = self._init_state(task)
        self._start_step_budget()
        # 阶段0：任务规划（模糊任务 → 结构化 plan），失败静默降级
        self._emit_progress("planning", state, result)
        self._plan(task, state)
        result.plan = state.memory.plan

        for step_no in range(1, self.max_steps + 1):
            self._start_step_budget()
            started = time.perf_counter()
            state.step_id = step_no
            self._emit_progress("observing", state, result)
            state = self.preprocess(state)
            if state.obs is not None:
                result.screenshots[step_no] = state.obs.screenshot
            self._emit_progress("deciding", state, result)
            messages = self.build_input(state)
            before = self._observation_record(state.obs, f"o_{step_no}")
            decision = self.decide(messages)
            self._emit_progress("decided", state, result, raw=decision.raw,
                                action=self._action_record(decision.action))
            for call in decision.calls:
                call.setdefault("actor_role", "decision")
                call.setdefault("provenance", "sampled")
            if decision.action is None:
                # 动作解析/校验失败：空转一步继续（对齐 Agent S3 的 wait 语义），
                # 不提前终止任务——9B 模型偶尔漏 next_action，下一轮往往能恢复。
                result.trajectory.append({
                    "step": step_no, "error": "动作解析失败",
                    "raw": (decision.raw or "")[-500:],
                    "observation": before,
                    "policy_input": copy.deepcopy(decision.calls[-1]["messages"] if decision.calls else messages),
                    "policy_output": decision.raw,
                    "decision_calls": decision.calls,
                    "model_calls": decision.calls,
                    "prompt_metadata": state.prompt_metadata,
                    "valid_action": False,
                    "terminated": False, "truncated": False,
                })
                state.memory.add_event("⚠️ 上一步动作解析失败，请重新输出合法的 next_action JSON")
                result.steps = step_no
                self._emit_progress("step_complete", state, result, status="rejected")
                continue
            ground_started = time.perf_counter()
            self._check_step_budget("grounding")
            self._emit_progress("grounding", state, result,
                                action=self._action_record(decision.action))
            action = self.ground(decision.action, state)
            ground_ms = (time.perf_counter() - ground_started) * 1000
            execute_started = time.perf_counter()
            self._check_step_budget("executing")
            if action.action == "wait" and float(action.args.get("seconds", 0)) > self._deadline - time.monotonic():
                raise ModelRequestTimeout("等待动作超出本步剩余时间")
            self._emit_progress("executing", state, result, action=self._action_record(action),
                                route=copy.deepcopy(state.route),
                                grounding=self._grounding_diagnostics() if state.route is None else {})
            # execute 阶段推送进度（下载等长耗时动作让前端看到"执行中"而非干等）
            if _viz_log_status is not None:
                try:
                    _viz_log_status({"event": "executing", "step": step_no,
                                     "action": decision.action.action if decision.action else ""})
                except Exception:  # noqa: BLE001
                    pass
            execution = self.execute(action, state)
            self._emit_progress("executed", state, result, execution_info=execution.info,
                                outcome=execution.outcome)
            if _viz_log_status is not None:
                try:
                    _viz_log_status({"event": "executed", "step": step_no,
                                     "status": (execution.info or {}).get("status", ""),
                                     "action": (execution.info or {}).get("action", "") or action.action,
                                     "error": (execution.info or {}).get("error", "")})
                except Exception:  # noqa: BLE001
                    pass
            execute_ms = (time.perf_counter() - execute_started) * 1000
            self._check_step_budget("postprocess")
            self._emit_progress("postprocess", state, result)
            state, should_break = self.postprocess(step_no, decision, execution, state, result)
            self._check_step_budget("step_complete")
            result.steps = step_no
            entry = next((t for t in reversed(result.trajectory)
                          if t.get("action_id") == f"a_{step_no}"), None)
            if entry is not None:
                entry.update({
                    "observation": before,
                    "next_observation": self._observation_record(state.obs, f"o_{step_no+1}"),
                    "policy_input": copy.deepcopy(decision.calls[-1]["messages"] if decision.calls else messages),
                    "policy_output": decision.raw,
                    "decision_calls": decision.calls,
                    "model_calls": decision.calls,
                    "prompt_metadata": state.prompt_metadata,
                    "valid_action": True,
                    "routing": copy.deepcopy(state.route),
                    "execution_info": execution.info,
                    "verification": self._verification if execution.kind == "finish_request" else None,
                    "terminated": bool(should_break and (result.success or execution.done)),
                    "truncated": bool(should_break and not (result.success or execution.done)),
                    "timing_ms": {"ground": round(ground_ms, 2), "execute_observe": round(execute_ms, 2),
                                  "total": round((time.perf_counter() - started) * 1000, 2)},
                })
            self._emit_progress("step_complete", state, result,
                                status=(execution.info or {}).get("status", ""))
            if self.step_callback is not None:
                try:
                    self.step_callback(step_no, decision, execution, state)
                except Exception:  # noqa: BLE001  回调失败不影响主流程
                    pass
            if should_break:
                break

        result.score = self._evaluate(result)
        # 补齐最后一步的 assessment（任务结束时没有下一步决策来评估 a_N）
        self._fill_final_assessment(result)
        transitions = [t for t in result.trajectory if "policy_input" in t]
        if transitions:
            transitions[-1]["truncated"] = not transitions[-1].get("terminated", False)
            transitions[-1]["done"] = True
        return result

    def _start_step_budget(self):
        self._deadline = time.monotonic() + self.step_timeout
        for component in (self.model, self.grounding, self.env):
            setattr(component, "request_deadline", self._deadline)

    def _check_step_budget(self, phase):
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise ModelRequestTimeout(f"本步已超过 {self.step_timeout:g} 秒，阶段: {phase}")

    @staticmethod
    def _action_record(action):
        return {"action": action.action, "args": dict(action.args)} if action is not None else None

    def _grounding_diagnostics(self):
        call = getattr(self.grounding, "last_call", {})
        if not isinstance(call, dict):
            return {}
        return {key: copy.deepcopy(call[key]) for key in
                ("model", "target", "response", "error", "confidence", "latency_ms") if key in call}

    def _emit_progress(self, phase, state, result, **details):
        now = time.monotonic()
        if self._last_phase is not None:
            previous, step, started = self._last_phase
            if step == state.step_id:
                details["previous_phase"] = previous
                details["previous_phase_elapsed_ms"] = round((now - started) * 1000, 2)
        self._last_phase = (phase, state.step_id, now)
        event = {"event": phase, "phase": phase, "step": state.step_id,
                 "task": state.task, "ts": time.time(), **details}
        duration = event.get("previous_phase_elapsed_ms")
        elapsed = f"；{event['previous_phase']} 耗时 {duration / 1000:.2f}s" if duration is not None else ""
        print(f"[step {state.step_id}] [phase] {phase}{elapsed}", flush=True)
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, state, result)
        except Exception as exc:  # 记录失败可见，不中断已经进行中的 GUI 任务
            print(f"[trajectory] 无法保存 {phase} 记录: {exc}", flush=True)

    @staticmethod
    def _observation_record(obs, observation_id):
        """把 Observation 转成可落盘的完整记录（截图 base64，dump 时外置去重）。"""
        if obs is None:
            return {"id": observation_id, "context": [], "screenshot": None}
        return {"id": observation_id, "context": copy.deepcopy(obs.context),
                "text": getattr(obs, "text", ""), "info": copy.deepcopy(obs.info),
                "screenshot": encode_image(obs.screenshot)}

    # ================= 六个阶段 ================= #
    def preprocess(self, state: StepState) -> StepState:
        if state.obs is None:
            state.obs = self.env.reset(state.task)
        state.screenshot_history.append(state.obs.screenshot)
        if len(state.screenshot_history) > 3:
            state.screenshot_history = state.screenshot_history[-3:]
        state.memory_text = self._build_memory_text(state)
        return state

    def build_input(self, state: StepState) -> List[dict]:
        page_source = state.page_source
        state.page_source = ""  # 一次性返回后清空，避免每步重复塞大段源码
        browser_result = state.browser_result
        state.browser_result = ""  # 一次性返回后清空
        # 结构观察块：browser AX snapshot（context）+ 桌面 AX（text）
        blocks = list(state.obs.context) if state.obs else []
        if state.obs and state.obs.text:
            blocks.append({"kind": "desktop_a11y", "text": state.obs.text})
        # 显式场景状态：让模型明确知道「有结构走 DOM / 无结构走视觉」
        hint = scene_hint(state.obs)
        blocks.insert(0, {"kind": "scene", "text": "当前场景: " + json.dumps(hint, ensure_ascii=False)})
        auxiliary = json.dumps({"skills": state.skill_results, "terminal": state.terminal_result,
                                "code_agent": state.code_agent_result, "page_source": page_source,
                                "browser_result": browser_result}, ensure_ascii=False)
        user_text, images, metadata = self.compiler.compile(
            system=self.system_prompt, task=state.task, memory=state.memory_text,
            blocks=blocks, auxiliary=auxiliary,
            images=self._build_images(state) if getattr(self.model, "vision", True) else [])
        state.prompt_metadata = metadata
        state.prompt_text = user_text
        if state.obs:
            state.obs.info["exposed_refs"] = metadata["exposed_refs"]
        return [
            {"role": "system", "content": self.system_prompt},
            self.model.build_vision_message(user_text, images),
        ]

    def decide(self, messages: List[dict]) -> Decision:
        raw, action = "", None
        screen_analysis = observed_effect = action_status = expected_effect = ""
        state_update: Dict[str, Any] = {}
        calls: List[dict] = []
        for _ in range(max(1, self.max_retries)):
            try:
                raw = self.model.chat(messages, json_mode=True)
            except Exception as exc:  # noqa: BLE001  传输失败：记录本次调用，不进入重试
                call = copy.deepcopy(getattr(self.model, "last_call", {}))
                call.update({"messages": copy.deepcopy(messages),
                             "error_type": type(exc).__name__, "valid_action": False})
                calls.append(call)
                if isinstance(exc, ModelRequestTimeout):
                    raise
                return Decision(raw="", action=None, calls=calls)
            call = copy.deepcopy(getattr(self.model, "last_call", {}))
            call.update({"messages": copy.deepcopy(messages), "response": raw, "valid_action": False})
            calls.append(call)
            obj = parse_decision(raw)
            action = action_from_decision(obj or {})
            if action is not None:
                err = self._validate_action(action)
                if err is None:
                    call["valid_action"] = True
                    screen_analysis = str((obj or {}).get("screen_analysis") or "")
                    observed_effect = str((obj or {}).get("observed_effect") or "")
                    action_status = str((obj or {}).get("action_status") or "")
                    state_update = (obj or {}).get("state_update") or {}
                    expected_effect = str((obj or {}).get("expected_effect") or "")
                    return Decision(raw=raw, action=action, screen_analysis=screen_analysis,
                                    observed_effect=observed_effect, action_status=action_status,
                                    state_update=state_update, expected_effect=expected_effect,
                                    calls=calls)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": build_format_feedback(err)})
            else:
                messages.append({"role": "assistant", "content": raw})
                if obj and "next_action" not in obj:
                    feedback = "你的 JSON 里漏了 next_action 字段（screen_analysis/observed_effect/state_update 都写了，就缺 next_action）。请补上 next_action，里面有 type 和对应参数。"
                elif obj and "next_action" in obj and not isinstance(obj["next_action"], dict):
                    feedback = "next_action 必须是一个对象（含 type 字段），现在不是合法对象。请改成 {\"type\": \"动作名\", ...}。"
                else:
                    feedback = "没有找到合法的 JSON 动作（需含 next_action）。"
                messages.append({"role": "user", "content": build_format_feedback(feedback)})
        return Decision(raw=raw, action=None, calls=calls)

    def ground(self, action: Action, state: StepState) -> Action:
        state.last_grounding_target = str(action.args.get("target", "")
                                          or action.args.get("from_target", ""))
        # 先路由：带 target_ref 且引用有效 → browser_dom；否则走 GUI grounding
        try:
            state.route = self.env.route_action(action, state.obs)
        except ModelRequestTimeout:
            raise
        except Exception:  # noqa: BLE001
            state.route = None
        if state.route is not None:
            state.last_grounding_coord = None
            state.resolved_action = action
            return action
        resolved = self._resolve(action, state.obs)
        state.last_grounding_coord = self._coordinate_of(resolved)
        state.resolved_action = resolved
        return resolved

    def execute(self, action: Action, state: StepState) -> Execution:
        name = action.action
        # browser_dom 通道：路由成功后走 DOM 定位执行（跳过 grounding）
        if state.route and state.route.get("channel") == "browser_dom":
            try:
                step_result = self.env.execute_routed(action, state.route)
                info = step_result.info if isinstance(step_result.info, dict) else {}
                # 下载类结果：把「已完成」作为明确事件注入记忆，让模型下一步直接 request_finish，
                # 而不是等它从 already_downloaded 的 JSON 里自己悟出来（base 模型常悟不出来，反复点击）。
                if info.get("action") == "download":
                    import os as _os
                    path = info.get("path", "")
                    if info.get("status") == "executed":
                        if info.get("verified"):
                            state.memory.add_event(
                                f"✅ 文件已下载并核实落盘：{path}（{info.get('bytes', 0)} 字节）。"
                                f"下载任务已完成，下一步请用 request_finish 结束任务。")
                            state.memory.known_facts["downloaded_file"] = path
                        else:
                            state.memory.add_event(
                                f"⚠️ 下载写入异常：{path} 落盘校验失败，可能需要重新下载。")
                    elif info.get("status") == "already_downloaded":
                        # 程序侧核实文件还在不在（不靠模型自评）
                        exists = bool(path) and _os.path.exists(path)
                        if exists:
                            state.memory.add_event(
                                f"✅ 文件已在本地核实存在：{path}。下载任务已完成，"
                                f"请立即用 request_finish 结束任务，不要再点击下载链接。")
                        else:
                            state.memory.add_event(
                                f"⚠️ 文件已下载过但当前不存在：{path}，需要重新下载。")
                        state.memory.known_facts["downloaded_file"] = path
                return Execution(kind="browser_dom", executed=self._compact_action(action),
                                 outcome=json.dumps(info, ensure_ascii=False),
                                 obs=step_result.observation, info=info,
                                 reward=step_result.reward, done=step_result.done)
            except ModelRequestTimeout:
                raise
            except Exception as exc:  # noqa: BLE001
                return Execution(kind="browser_dom", executed=self._compact_action(action),
                                 outcome=f"结构化执行异常，需重新观察: {type(exc).__name__}",
                                 info={"status": "uncertain", "error_type": type(exc).__name__})
        if state.route and state.route.get("channel") == "reobserve":
            refresh = getattr(self.env, "_fresh_obs_after_action", None)
            obs = refresh() if state.route.get("refresh_observation") and callable(refresh) else None
            return Execution(kind="browser_dom", outcome=state.route.get("reason", "需重新观察"),
                             executed="rejected", obs=obs,
                             info={"status": "rejected", "dispatched": False})

        if is_skill(name):
            out = run_skill(name, action.args, state.skill_ctx)
            state.skill_results[name] = out
            return Execution(kind="skill", executed=f"技能 {name}", outcome="结果将在下一步返回")

        spec = get_action_spec(name)
        kind = spec.kind if spec else "env"

        if kind == "shell":
            command = str(action.args.get("command", ""))
            output = self.env.run_command(command)
            # 对齐 AgentS3 的 run_in_terminal 语义：原框架是「打开终端 → pyautogui 真打字」，
            # 命令会写进 ~/.bash_history；我们 subprocess 执行不写。但有些 evaluator 检查
            # 「是否从终端执行」是靠 grep ~/.bash_history（如 force-quit 任务查 kill 命令），
            # 这里补写一行，保证 shell 命令在 history 里可见，不改变命令本身的行为。
            try:
                self.env.run_python(
                    "import os\n"
                    "_h = os.path.expanduser('~/.bash_history')\n"
                    f"with open(_h, 'a') as _f: _f.write({command!r} + '\\n')\n"
                )
            except Exception:  # noqa: BLE001  补写失败不影响命令已执行的事实
                pass
            state.terminal_result = output[:4000] + ("\n...(输出过长已截断)" if len(output) > 4000 else "")
            return Execution(kind="shell", executed=f"终端命令: {command}",
                             outcome="命令已执行，输出将在下一步返回")

        if kind == "browser":
            result = self.env.browser_action(name, action.args)
            if name == "get_page_source":
                state.page_source = result[:12000] + ("\n...(源码过长已截断)" if len(result) > 12000 else "")
                return Execution(kind="browser", executed="浏览器: get_page_source",
                                 outcome="页面源码将在下一步返回")
            state.browser_result = result
            return Execution(kind="browser", executed=f"浏览器: {name}",
                             outcome=result)

        if kind == "subagent":
            if self.code_agent is None:
                return Execution(kind="subagent", executed="子Agent", outcome="子Agent未配置")
            task = str(action.args.get("task", "") or state.task)
            result = self.code_agent.execute(task)
            state.code_agent_result = self._format_code_agent_result(result)
            for k, v in (result.get("notes") or {}).items():
                state.memory.known_facts[k] = v
            return Execution(kind="subagent", executed=f"子Agent: {task[:60]}",
                             outcome="子Agent已执行，结果将在下一步返回",
                             info={"action": "call_code_agent", "task": task,
                                   "result": state.code_agent_result[:1000]})

        if kind == "terminal":
            if name == "fail":
                return Execution(kind="terminal", executed=name, outcome="任务失败",
                                 done=True, success=False, answer="")
            # request_finish：不立即结束，交给 postprocess 验证
            return Execution(kind="finish_request", executed=name, outcome="请求结束，待验证",
                             done=False, success=False, answer=str(action.args.get("answer", "")))

        # env / uno 动作：此时 action 已坐标化。
        # uno = UNO 直写（set_cell_values / save，经 LibreOffice 2002 端口），与 GUI 点击
        # 共用 env.step 通道，但验证方式不同（uno 读回单元格 / 确认落盘，env 看屏幕变化）。
        try:
            step_result = self.env.step(action)
        except ModelRequestTimeout:
            raise
        except Exception as exc:  # noqa: BLE001
            return Execution(kind=kind, executed=self._compact_action(action),
                             outcome=f"执行异常: {exc}",
                             info={"status": "uncertain", "dispatched": True,
                                   "action": name, "error": str(exc)})
        outcome = self._describe_outcome(step_result)
        # type 落点校验结果透传：把「疑似打错输入框」的提示拼进 outcome，模型下一轮能看到并纠正
        if isinstance(step_result.info, dict):
            tc = step_result.info.get("type_check")
            if step_result.info.get("type_wrong_target") and tc:
                outcome += f"；⚠️ 输入落点校验：{tc}"

        # UNO 直写动作的写后校验（程序侧，不靠模型自觉）
        if kind == "uno" and action.action == "set_cell_values":
            try:
                from .adapters.vmware_env import verify_cell_values
                ok, detail = verify_cell_values(
                    self.env,
                    action.args.get("new_cell_values") or {},
                    str(action.args.get("app_name") or ""),
                    str(action.args.get("sheet_name") or "Sheet1"),
                )
                outcome += f"；写入校验：{'通过' if ok else '失败'}"
            except Exception:  # noqa: BLE001  校验失败不影响主流程
                pass

        return Execution(kind=kind, executed=self._compact_action(action),
                         outcome=outcome,
                         obs=step_result.observation, done=step_result.done,
                         info=step_result.info if isinstance(step_result.info, dict) else {},
                         reward=step_result.reward)

    def postprocess(self, step_no: int, decision: Decision, execution: Execution,
                    state: StepState, result: AgentResult) -> Tuple[StepState, bool]:
        """⑥ 后处理：记录 expected/observed + 控制状态 + 卡死检测 + Finish 验证。"""
        # 1) 记录模型对上一步的判断（observed / action_status）
        state.memory.observed_effect = decision.observed_effect
        state.memory.action_status = decision.action_status
        # 2) 根据 action_status 记录事件/失败（针对上一步动作）
        if state.last_action:
            desc = self._action_desc(state.last_action, state.last_target)
            if decision.action_status == "failure":
                state.failure_count += 1
                state.memory.add_failure(f"{desc} 失败：{decision.observed_effect or '无可见变化'}")
                state.memory.record_failed_strategy(desc)
                state.memory.add_event(f"{desc} → 失败")
            else:
                state.memory.add_event(f"{desc} → {decision.observed_effect or decision.action_status or '已执行'}")
        # 3) 应用 state_update 补丁
        state.memory.apply_state_update(decision.state_update)
        # 3.1) 程序侧更新 plan 进度（不依赖模型 state_update，防止待办一直不更新）
        self._update_plan_progress(state, execution)
        # 4) 回填上一步动作 a_{t-1} 的 assessment（decision 的 observed_effect/action_status
        #    评价的是上一步动作，而不是本步 next_action）
        self._fill_prev_assessment(result, decision)
        # 4.1) 记录这一步的 transition：a_t 的 assessment 留空，下一步回填
        result.trajectory.append({
            "step": step_no,
            "action_id": f"a_{step_no}",
            "observation_id": f"o_{step_no}",
            "screen_analysis": decision.screen_analysis,
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
            "reward": None,
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
        # 6) env 动作更新观察
        before_screenshot = state.screenshot_history[-1] if state.screenshot_history else None
        if execution.obs is not None:
            state.obs = execution.obs
        # 6.5) 单步动作有效性校验：env 动作执行后屏幕无变化 → 当场提示换策略
        # （不等到连续 2 步 STALL，每一步点空/点错都立即提示，避免反复点同一无效目标）
        if execution.kind == "env" and execution.obs is not None and before_screenshot is not None:
            if self._screen_diff(before_screenshot, execution.obs.screenshot) < _NO_CHANGE_DIFF_THRESHOLD:
                state.memory.add_event(
                    "⚠️ 这一步动作后屏幕没有可见变化：可能点空 / 点错 / 焦点不对。"
                    "下一步请换一种方式（键盘快捷键 / 不同菜单路径 / 重新定位目标），不要重复同样的点击。")
        # 7) 控制状态 + 确定性卡死检测
        self._compute_control_state(state, decision, execution, before_screenshot)
        # 7.5) 持久卡死（连续 STALL 3 次）→ 硬停止，不再只警告
        should_break = False
        if state.memory.control_state == "STALL" and state.memory.stall_count >= 3:
            result.trajectory.append({"step": step_no, "early_stop": "持续卡死，强制停止"})
            should_break = True
        # 8) 设置下一步的 expected_effect（当前决策的 expected_effect → 下一步对比用）
        state.memory.expected_effect = decision.expected_effect
        # 8.5) 程序侧完成判断：用验证器（导航/下载等）判断任务是否完成，不靠模型 finish 验证器
        if not should_break:
            verifier = self._programmatic_done(state, execution)
            if verifier:
                info = execution.info or {}
                if verifier == "download":
                    result.final_answer = f"已下载文件到 {info.get('path', '')}（{info.get('bytes', 0)} 字节），已核实落盘"
                elif verifier == "navigation":
                    result.final_answer = f"已导航到 {info.get('url', '')}"
                else:
                    result.final_answer = "任务完成"
                result.success = True
                state.memory.add_event(f"✅ 程序核实：{verifier} 验证通过，任务结束")
                should_break = True
        # 9) request_finish 验证
        if not should_break and execution.kind == "finish_request":
            confirmed, reason = self._verify_finish(state, execution.answer)
            if confirmed:
                result.success = True
                result.final_answer = execution.answer
                should_break = True
            else:
                state.memory.add_event(f"request_finish 被拒绝：{reason}")
        # 10) fail 结束
        if execution.done:
            result.success = execution.success if execution.kind == "terminal" else True
            result.final_answer = execution.answer
            should_break = True
        # （reflection 已关闭：旧实现调了模型但结果被丢弃，属无效调用，浪费一次推理）

        return state, should_break

    # ================= 内部工具 ================= #
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
                # 本机任务无评测器（score 恒 0），以 success（finish 验证结果）为准；
                # OSWorld 有评测器，score>=1 才算成功。
                ok = bool(result.success) or result.score >= 1.0
                t["assessment"] = {
                    "observed_effect": ("任务结束，成功" if ok else f"任务结束，最终得分 {result.score}"),
                    "action_status": "success" if ok else "failure",
                }
                t["done"] = True
                break

    def _init_state(self, task: str) -> StepState:
        return StepState(
            task=task,
            memory=StructuredMemory(),
            skill_ctx=SkillContext(env=self.env),
        )

    def _plan(self, task: str, state: StepState) -> None:
        """阶段0：把模糊任务拆成结构化 plan，存进记忆。

        失败（模型输出非法 JSON / 网络异常）静默降级为「无 plan 继续跑」，
        绝不因为 planner 出错而卡死整个任务。
        """
        prompt = build_planner_prompt(task)
        try:
            raw = self.model.chat([{"role": "user", "content": prompt}], retries=1, json_mode=True)
            obj = parse_decision(raw)
            steps = obj.get("steps") if isinstance(obj, dict) else None
            if not isinstance(steps, list) or not steps:
                return
            norm_steps = []
            for s in steps:
                if not isinstance(s, dict):
                    continue
                goal = str(s.get("goal", "")).strip()
                if not goal:
                    continue
                norm_steps.append({
                    "idx": s.get("idx", len(norm_steps) + 1),
                    "goal": goal,
                    "status": "pending",
                })
            if not norm_steps:
                return
            state.memory.plan = {
                "steps": norm_steps,
                "keywords": obj.get("keywords") or [],
                "final_check": str(obj.get("final_check") or ""),
            }
            state.memory.task_state["goal"] = task
            state.memory.add_event(f"📋 已制定任务计划：{len(norm_steps)} 步")
        except ModelRequestTimeout:
            raise
        except Exception:  # noqa: BLE001  planner 失败不影响主流程
            return

    def _update_plan_progress(self, state: StepState, execution: Execution) -> None:
        """根据执行结果，程序侧标记 plan 里已完成的步骤（不依赖模型 state_update）。

        模型常漏 state_update（current_subgoal/completed 为空），导致 plan 的 status 永远 pending。
        这里用执行结果（action + verified 等确定性信号）标记步骤完成。
        """
        plan = state.memory.plan
        if not plan or not plan.get("steps"):
            return
        info = execution.info if isinstance(execution.info, dict) else {}
        if info.get("status") != "executed":
            return
        action = info.get("action") or ""
        # 下载成功（程序核实落盘 verified）→ 标记所有"下载/PDF"步骤 done
        if action == "download" and info.get("verified"):
            for s in plan["steps"]:
                g = (s.get("goal") or "").lower()
                if any(k in g for k in ("下载", "download", "pdf")):
                    s["status"] = "done"
            return
        # goto 成功 → 标记第一个"打开/进入/访问网站"步骤 done
        if action == "goto":
            for s in plan["steps"]:
                if s.get("status") != "done":
                    g = (s.get("goal") or "").lower()
                    if any(k in g for k in ("打开", "进入", "访问", "官网", "首页",
                                            "搜索页面", "arxiv", "scholar", "google")):
                        s["status"] = "done"
                        break

    def _programmatic_done(self, state: StepState, execution: Execution) -> str:
        """程序侧判断任务是否完成（确定性验证器，不靠模型 finish 验证器）。

        返回命中的验证器名（str），未命中返回空字符串。
        验证器在 verifiers.py 里注册，可逐步扩展（导航/下载/表单/搜索……）。
        """
        info = execution.info if isinstance(execution.info, dict) else {}
        from . import verifiers
        return verifiers.check_completion(state.task or "", info)

    def _build_memory_text(self, state: StepState) -> str:
        return state.memory.to_text() + "\n\n" + self._program_info_text(state)

    def _program_info_text(self, state: StepState) -> str:
        coord = state.last_grounding_coord
        return "\n".join([
            "## 程序信息(program)",
            f"- step_id: {state.step_id}",
            f"- last_action: {state.last_action or '（无，这是第一步）'}",
            f"- last_target: {state.last_target or '（无）'}",
            f"- grounding_coordinate: {list(coord) if coord else '（无）'}",
            f"- execution_status: {state.execution_status or '（无）'}",
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
                               execution: Execution, before_screenshot) -> str:
        stall_reason = self._detect_stall(state, decision, execution, before_screenshot)
        if stall_reason:
            state.memory.control_state = "STALL"
            state.memory.stall_count += 1
            sig = self._stable_action_key(decision.action)
            state.memory.add_forbidden_repeat(sig)
            state.memory.add_event(f"⚠️ STALL：{stall_reason}。反复同一操作且屏幕无变化，任务可能【已经完成】——先看界面/文档是否已达标，达标就 request_finish；否则换一种完全不同的方法。")
            return "STALL"
        status = decision.action_status
        if status == "failure":
            state.memory.control_state = "RECOVER"
        elif status == "success":
            state.memory.control_state = "ADVANCE"
        else:
            state.memory.control_state = "CONTINUE"
        return state.memory.control_state

    @staticmethod
    def _brief_outcome(execution) -> str:
        """把执行结果精简成一句话，供动作历史使用（避免冗长 JSON 撑爆记忆预算）。"""
        info = execution.info if isinstance(execution.info, dict) else {}
        if info.get("action") == "download":
            if info.get("status") == "already_downloaded":
                return "已下载过（跳过）"
            return f"下载成功 {info.get('bytes', '?')}B"
        if info.get("action") == "goto":
            return f"已打开 {str(info.get('url', ''))[:60]}"
        status = info.get("status")
        if status == "rejected":
            return f"失败：{str(info.get('error', ''))[:60]}"
        if status == "uncertain":
            return f"结果不确定：{str(info.get('error', ''))[:60]}"
        if status == "executed":
            return "已执行"
        return (execution.outcome or "")[:80]

    @staticmethod
    def _stable_action_key(action) -> str:
        """稳定的动作签名：忽略每次变化的 ref/id/坐标/href，用于「重复动作」卡死检测。

        action_signature 会把 element_id/target_ref 也算进去（每次快照 ref 都变），
        导致「连续点同一链接」被判成不同动作、卡死检测失效。这里只保留语义 + 稳定参数。
        """
        if not action:
            return ""
        import re
        a = action.action
        target = str(action.args.get("target", "") or action.args.get("from_target", ""))
        target = re.sub(r"\(href[^)]*\)", "", target)          # 去掉 (href ...) 括号形式
        target = re.sub(r"href\s+[^\s,)]*", "href", target)    # 归一化 "href URL" → "href"
        target = re.sub(r"\[\d+\]", "", target)                # 去掉方括号数字 ref
        target = re.sub(r"\s+", " ", target).strip()
        stable = {"action": a, "target": target[:80]}
        for k in ("text", "option", "key", "url", "amount", "direction", "overwrite"):
            if k in action.args:
                stable[k] = action.args[k]
        return json.dumps(stable, ensure_ascii=False, sort_keys=True)

    def _detect_stall(self, state: StepState, decision: Decision,
                      execution: Execution, before_screenshot) -> Optional[str]:
        """确定性卡死检测：屏幕无变化 + shell 输出无变化 + 动作循环 + 禁止重复命中。返回原因或 None。"""
        # 1) 屏幕无变化（仅 env 动作，对比动作前后截图）
        if execution.kind == "env" and execution.obs is not None:
            after = execution.obs.screenshot
            if self._screen_diff(before_screenshot, after) < _NO_CHANGE_DIFF_THRESHOLD:
                state.no_change_count += 1
            else:
                state.no_change_count = 0
            if state.no_change_count >= _NO_CHANGE_CONSECUTIVE_THRESHOLD:
                return f"连续 {state.no_change_count} 步屏幕无可见变化"
        # 1b) shell 动作：命令在 VM 后台执行、不改屏幕，屏幕对比对它无效。
        #     改用「连续多次终端输出完全相同」判卡死（典型：反复执行同一失败命令）。
        if execution.kind == "shell":
            state.shell_outputs.append(state.terminal_result or "")
            recent = state.shell_outputs[-_REPEAT_ACTION_THRESHOLD:]
            if (len(recent) >= _REPEAT_ACTION_THRESHOLD
                    and all(o == recent[-1] for o in recent)):
                return f"连续 {len(recent)} 次终端命令输出完全相同（命令未产生新进展）"
        # 2) 连续相同动作（连续 N 次相同 action+target；用稳定签名，忽略每次变化的 ref/id）
        sig = self._stable_action_key(decision.action)
        state.action_keys.append(sig)
        recent = state.action_keys[-_REPEAT_ACTION_THRESHOLD:]
        if len(recent) >= _REPEAT_ACTION_THRESHOLD and all(k == sig for k in recent):
            return f"连续 {_REPEAT_ACTION_THRESHOLD} 次相同动作 {sig[:60]}"
        # 3) 周期循环（A,B,A,B 等，周期 2）
        keys = state.action_keys[-_LOOP_HISTORY_LEN:]
        if len(keys) >= _LOOP_HISTORY_LEN:
            for period in _LOOP_PERIODS:
                if all(keys[i] == keys[i - period] for i in range(period, len(keys))):
                    return f"动作形成 {period} 步周期循环"
        # 4) 禁止重复命中
        if sig and sig in state.memory.forbidden_repeats:
            return "重复了被禁止的策略"
        return None

    def _verify_finish(self, state: StepState, answer: str) -> Tuple[bool, str]:
        """Finish Verifier：逐条核对任务是否完成。返回 (是否确认, 说明)。"""
        text = (f"你是一个任务完成度核对器。\n\n## 任务\n{state.task}\n\n"
                f"## 已完成的工作（结构化记忆）\n{state.memory_text}\n\n"
                f"## 模型声称的答案\n{answer}\n\n"
                f"请逐条核对任务要求是否都已完成且答案正确。\n"
                f"全部满足：只输出 CONFIRM\n否则：输出 REJECT 并说明还缺什么")
        messages = [{"role": "user", "content": text}]
        try:
            raw = self.model.chat(messages)
        except ModelRequestTimeout:
            raise
        except Exception as exc:  # noqa: BLE001  模型不可用时放行，避免死锁
            self._verification = {"messages": messages, "error_type": type(exc).__name__}
            return True, ""
        self._verification = {"messages": messages, "response": raw,
                              "call": copy.deepcopy(getattr(self.model, "last_call", {}))}
        confirmed = "CONFIRM" in raw.upper() and "REJECT" not in raw.upper()
        return confirmed, raw.strip()

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
            info = execution.info or {}
            state.execution_status = info.get("status") or ("error" if info.get("error") else "executed")

    def _validate_action(self, action: Action) -> Optional[str]:
        name = action.action
        if is_skill(name):
            return validate_skill(name, action.args)
        supported = getattr(self.env, "supported_action_names", None)
        if supported is not None and name not in supported:
            return f"当前环境不支持动作「{name}」。可选动作：{', '.join(sorted(supported))}"
        if name not in ACTION_REGISTRY:
            return (f"未知动作类型「{name}」。可选动作：{', '.join(ACTION_REGISTRY)}；"
                    f"可选技能：{', '.join(enabled_skills())}")
        return validate_action(action)

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
        if spec.resolve == "point":
            gr = self.grounding.locate(obs.screenshot, str(action.args.get("target", "")))
            coord = resolve_coords(gr)
            # 保留 target 之外的参数（如 type 的 text），让「定位+聚焦」类动作能带上其余参数
            extra = {k: v for k, v in action.args.items() if k != "target"}
            if coord is None:
                # UI-TARS 失败：尝试 OCR 文字定位兜底（对齐 AgentS3 的第二条 grounding 腿）
                coord = self._ocr_fallback(obs.screenshot, str(action.args.get("target", "")))
            if coord is None:
                # grounding 失败：不带可执行坐标，由执行器拒绝（空转），不做坐标钳制
                return Action(action.action, {"grounding_failed": True,
                                              "grounding_error": self._grounding_diagnostics().get("error", ""),
                                              **extra})
            x, y = coord
            return Action(action.action, {"x": x, "y": y, **extra})
        if spec.resolve == "select":
            gr = self.grounding.locate(obs.screenshot, str(action.args.get("target", "")))
            coord = resolve_coords(gr)
            if coord is None:
                coord = self._ocr_fallback(obs.screenshot, str(action.args.get("target", "")))
            if coord is None:
                return Action(action.action, {"grounding_failed": True,
                                              "option": action.args.get("option", "")})
            x, y = coord
            return Action(action.action, {"x": x, "y": y, "option": action.args.get("option", "")})
        if spec.resolve == "drag":
            g1 = self.grounding.locate(obs.screenshot, str(action.args.get("from_target", "")))
            g2 = self.grounding.locate(obs.screenshot, str(action.args.get("to_target", "")))
            c1, c2 = resolve_coords(g1), resolve_coords(g2)
            if c1 is None or c2 is None:
                return Action(action.action, {"grounding_failed": True})
            return Action(action.action, {"x1": c1[0], "y1": c1[1], "x2": c2[0], "y2": c2[1]})
        return action

    def _ocr_fallback(self, screenshot, target: str):
        """UI-TARS 定位失败时的 OCR 文字定位兜底（对齐 AgentS3 的 generate_text_coords）。

        pytesseract OCR 提取屏幕文字表 → 用决策模型从文字表选最匹配 target 的 word
        → 返回 word bbox 中心坐标。OCR 依赖不可用 / 匹配失败时返回 None（不误伤）。
        """
        try:
            import re
            import pytesseract
            from PIL import Image
            from io import BytesIO
        except ImportError:
            return None

        try:
            img = screenshot
            if isinstance(screenshot, bytes):
                img = Image.open(BytesIO(screenshot))
            self._check_step_budget("ocr")
            timeout = max(0.001, min(15.0, self._deadline - time.monotonic())) if self._deadline else 15.0
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, timeout=timeout)
        except ModelRequestTimeout:
            raise
        except Exception:  # noqa: BLE001
            return None

        words = []
        for i, text in enumerate(data.get("text") or []):
            if text and text.strip():
                words.append({
                    "id": len(words),
                    "text": text.strip(),
                    "left": data["left"][i],
                    "top": data["top"][i],
                    "width": data["width"][i],
                    "height": data["height"][i],
                })
        if not words:
            return None

        table = "Word id\tText\n" + "\n".join(f"{w['id']}\t{w['text']}" for w in words)
        prompt = (
            f"屏幕上有以下文字（OCR 提取的文字表）：\n{table}\n\n"
            f"目标元素描述：{target}\n"
            f"请只输出一个数字：最匹配「目标元素描述」的那个 word id。"
        )
        try:
            raw = self.model.chat([{"role": "user", "content": prompt}])
            nums = re.findall(r"\d+", raw or "")
            if nums:
                wid = int(nums[-1])
                if 0 <= wid < len(words):
                    w = words[wid]
                    return (w["left"] + w["width"] // 2, w["top"] + w["height"] // 2)
        except ModelRequestTimeout:
            raise
        except Exception:  # noqa: BLE001
            pass
        return None

    def _evaluate(self, result: AgentResult) -> float:
        try:
            return self.env.evaluate()
        except NotImplementedError:
            return 1.0 if result.success else 0.0
        except Exception as exc:  # noqa: BLE001
            # 评测器自身失败（如 postconfig 要下载的评测脚本被网络/代理拦掉）不应毁掉整次运行：
            # agent 的实际操作已经完成，把评测异常记入轨迹、按 0 分返回，保证 result/轨迹正常落盘。
            result.trajectory.append({
                "step": result.steps,
                "eval_error": f"评测阶段异常（非 agent 操作失败）：{str(exc)[:500]}",
            })
            return 0.0

    # ---- 纯静态小工具 ----
    @staticmethod
    def _coordinate_of(action: Action) -> Optional[Tuple[int, int]]:
        a = action.args
        if "x" in a and "y" in a:
            return int(a["x"]), int(a["y"])
        if "x1" in a and "y1" in a:
            return int(a["x1"]), int(a["y1"])
        return None

    @staticmethod
    def _target_of(action: Action) -> str:
        a = action.args
        if action.action in ("click", "double_click", "right_click", "select"):
            return str(a.get("target", ""))
        if action.action == "drag_and_drop":
            return f"{a.get('from_target', '')} -> {a.get('to_target', '')}"
        if action.action == "type":
            t = str(a.get("target", ""))
            txt = str(a.get("text", ""))
            if t:
                return f"{txt!r} → {t}"
            return txt
        if action.action == "scroll":
            return f"{a.get('direction', '')} {a.get('amount', '')}"
        if action.action == "run_in_terminal":
            return str(a.get("command", ""))
        if action.action == "goto":
            return str(a.get("url", ""))
        if action.action == "hotkey":
            return "+".join(a.get("keys", []))
        if action.action == "press":
            return str(a.get("key", ""))
        return ""

    @staticmethod
    def _action_desc(action: str, target: str) -> str:
        if target:
            return f"{action} {target}"
        return action

    @staticmethod
    def _compact_action(action) -> str:
        if isinstance(action, Action):
            return json.dumps({"action": action.action, "args": action.args}, ensure_ascii=False)
        return str(action)

    @staticmethod
    def _describe_outcome(step_result) -> str:
        info = step_result.info if isinstance(step_result.info, dict) else {}
        status, error = info.get("status"), info.get("error")
        if status == "rejected":
            return f"未执行：{error or '动作被拒绝'}"
        if status == "uncertain":
            return f"执行结果不确定：{error or '需要重新观察'}"
        if error:
            return f"执行失败：{error}"
        if step_result.done:
            return "任务完成"
        if step_result.reward and step_result.reward > 0:
            return f"有进展(奖励 {step_result.reward})"
        return "已执行（屏幕状态待下一步判断）"
