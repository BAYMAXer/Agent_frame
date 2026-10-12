"""结构化记忆 + 持久控制状态（PersistentControlState）。

四部分结构化记忆（task_state / recent_events / known_facts / failure_memory）+
控制闭环字段（expected_effect / observed_effect / action_status / control_state / stall 等）。

核心原则：
- LLM 负责理解环境、判断状态变化，程序负责真正保存和更新记忆；
- 模型只能输出 state_update 补丁，不能重写整份 memory；
- 控制闭环：执行前 Planner 说明 expected_effect，执行后 Controller 判断 observed_effect，
  程序对比两者 + 确定性卡死检测，得出 control_state（CONTINUE/ADVANCE/RECOVER/STALL）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_RECENT_EVENTS = 3
MAX_FAILURES = 3
MAX_ACTION_HISTORY = 20      # 动作历史渲染给模型时保留最近 N 条（覆盖默认 max_steps=20）


@dataclass
class StructuredMemory:
    """四部分结构化记忆 + 控制状态。"""
    # ---- 四部分记忆 ----
    task_state: Dict[str, Any] = field(default_factory=lambda: {
        "goal": "",
        "current_subgoal": "",
        "completed": [],
        "pending": [],
    })
    # 稳定 ID、完成判据及证据维护的计划；task_state 由此派生。
    plan: Optional[Dict[str, Any]] = None
    recent_events: List[str] = field(default_factory=list)      # 最近 3 条
    known_facts: Dict[str, Any] = field(default_factory=dict)   # 关键事实（key -> value）
    failure_memory: List[str] = field(default_factory=list)     # 最近 1~3 个重要失败
    action_history: List[str] = field(default_factory=list)     # 完整动作历史：每步做了什么 + 结果

    # ---- 控制状态（程序维护）----
    expected_effect: str = ""          # 上一步动作的预期效果
    observed_effect: str = ""          # 上一步动作的实际观察
    action_status: str = ""            # 上一步动作的判定：success / failure / uncertain
    control_state: str = "CONTINUE"    # CONTINUE / ADVANCE / RECOVER / STALL / VERIFY_FINISH
    stall_count: int = 0
    failed_strategies: List[str] = field(default_factory=list)   # 失败过的策略描述
    forbidden_repeats: List[str] = field(default_factory=list)   # 禁止重复的 (action+target) 签名

    # ---- 程序维护：应用模型输出的 state_update 补丁 ----
    def apply_state_update(self, patch: Dict[str, Any]) -> None:
        if not isinstance(patch, dict) or not patch:
            return
        if patch.get("goal"):
            self.task_state["goal"] = patch["goal"]
        if patch.get("current_subgoal") is not None:
            self.task_state["current_subgoal"] = patch["current_subgoal"]
        completed = patch.get("completed_add")
        for item in completed if isinstance(completed, list) else []:
            if item and item not in self.task_state["completed"]:
                self.task_state["completed"].append(item)
        pending = patch.get("pending_add")
        for item in pending if isinstance(pending, list) else []:
            if item and item not in self.task_state["pending"]:
                self.task_state["pending"].append(item)
        facts = patch.get("known_fact_add")
        for k, v in (facts if isinstance(facts, dict) else {}).items():
            self.known_facts[k] = v
        self.sync_plan_state()

    def sync_plan_state(self) -> None:
        """有计划时由已确认的计划状态派生记忆，避免两份待办互相矛盾。"""
        if not isinstance(self.plan, dict) or not isinstance(self.plan.get("steps"), list):
            return
        steps = [step for step in self.plan["steps"] if isinstance(step, dict) and step.get("goal")]
        if not steps:
            return
        current = self.plan.get("current_step_id")
        active = next((step for step in steps if step.get("id") == current), None)
        self.task_state["current_subgoal"] = active.get("goal", "") if active else ""
        self.task_state["current_plan_step_id"] = current
        self.task_state["completed"] = [step["goal"] for step in steps if step.get("status") == "done"]
        self.task_state["pending"] = [step["goal"] for step in steps
                                      if step.get("status") not in ("done", "skipped")]

    # ---- 程序维护：事件 / 失败 ----
    def add_event(self, event: str) -> None:
        if not event:
            return
        self.recent_events.append(event)
        if len(self.recent_events) > MAX_RECENT_EVENTS:
            self.recent_events = self.recent_events[-MAX_RECENT_EVENTS:]

    def add_failure(self, failure: str) -> None:
        if not failure:
            return
        self.failure_memory.append(failure)
        if len(self.failure_memory) > MAX_FAILURES:
            self.failure_memory = self.failure_memory[-MAX_FAILURES:]

    # ---- 程序维护：控制状态 ----
    def record_failed_strategy(self, desc: str) -> None:
        if desc and desc not in self.failed_strategies:
            self.failed_strategies.append(desc)

    def add_forbidden_repeat(self, signature: str) -> None:
        if signature and signature not in self.forbidden_repeats:
            self.forbidden_repeats.append(signature)

    def add_action(self, step: int, desc: str, outcome: str) -> None:
        """记录一步动作到完整动作历史（程序侧维护，让模型能回顾自己做过了什么）。"""
        line = f"step{step}: {desc}"
        if outcome:
            line += f" → {outcome}"
        self.action_history.append(line)

    # ---- 渲染给模型 ----
    def to_text(self) -> str:
        def brief(value, limit):
            text = " ".join(str(value or "").split())
            return text if len(text) <= limit else text[:limit] + "…"

        ts = self.task_state
        lines = []
        # 编译器从开头截断：当前 ID、完整状态索引和当前判据优先于历史详情。
        if self.plan:
            steps = [s for s in self.plan.get("steps", []) if isinstance(s, dict)]
            current = self.plan.get("current_step_id")
            lines.append("## 任务计划(plan)")
            lines.append(f"- current_step_id: {current or '（无）'}")
            lines.append("- 步骤状态: " + ", ".join(
                f"{s.get('id', s.get('idx', '?'))}={s.get('status', 'pending')}" for s in steps))
            active = next((s for s in steps if s.get("id") == current), None)
            if active:
                lines.append(f"- 当前子目标: {brief(active.get('goal'), 120)}")
                lines.append(f"- 完成判据: {brief(active.get('completion_criteria') or active.get('goal'), 180)}")
                if active.get("blocked_reason"):
                    lines.append(f"- 受阻原因: {brief(active['blocked_reason'], 120)}")
            lines.append(f"- 上一步预期效果 expected_effect: {brief(self.expected_effect, 120) or '（无）'}")
            for s in steps:
                idx = s.get("idx", "?")
                status = s.get("status", "pending")
                mark = {"pending": "待办", "doing": "进行中", "in_progress": "进行中",
                        "done": "已完成", "blocked": "受阻", "skipped": "已跳过"}.get(status, status)
                lines.append(f"- [{mark}] {s.get('id', idx)} / {idx}. {brief(s.get('goal'), 72)}")
                if s.get("evidence"):
                    lines.append(f"  完成证据: {brief(s['evidence'], 96)}")
            if self.plan.get("keywords"):
                lines.append(f"- 搜索关键词 keywords: {self.plan.get('keywords')}")
            if self.plan.get("final_check"):
                lines.append(f"- 完成判据 final_check: {self.plan.get('final_check')}")
        lines += [
            "",
            "## 任务状态(task_state)",
            f"- 目标 goal: {ts['goal'] or '（未填）'}",
            f"- 当前子目标 current_subgoal: {ts['current_subgoal'] or '（未填）'}",
            f"- 已完成 completed: {ts['completed'] if ts['completed'] else '[]'}",
            f"- 待办 pending: {ts['pending'] if ts['pending'] else '[]'}",
        ]
        lines += [
            "",
            "## 动作历史(action_history，从头到尾做了什么)",
        ]
        hist = self.action_history[-MAX_ACTION_HISTORY:]
        lines += [f"- {a}" for a in hist] or ["- （无）"]
        lines += [
            "",
            "## 控制状态(control)",
            f"- 上一步预期效果 expected_effect: {self.expected_effect or '（无）'}",
            f"- 上一步实际观察 observed_effect: {self.observed_effect or '（无）'}",
            f"- 上一步判定 action_status: {self.action_status or '（无）'}",
            f"- control_state: {self.control_state}",
            f"- stall_count: {self.stall_count}",
            f"- 失败过的策略 failed_strategies: {self.failed_strategies if self.failed_strategies else '[]'}",
            f"- 禁止重复 forbidden_repeats: {self.forbidden_repeats if self.forbidden_repeats else '[]'}",
            "",
            "## 最近事件(recent_events)",
        ]
        lines += [f"- {e}" for e in self.recent_events] or ["- （无）"]
        lines.append("")
        lines.append("## 已知事实(known_facts)")
        lines += [f"- {k}: {v}" for k, v in self.known_facts.items()] or ["- （无）"]
        lines.append("")
        lines.append("## 失败记忆(failure_memory)")
        lines += [f"- {f}" for f in self.failure_memory] or ["- （无）"]
        return "\n".join(lines)
