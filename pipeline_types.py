"""Data containers shared by pipeline stages, independent of orchestration."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .actions import Action
from .env import Observation
from .execution_queue import ExecutionQueue
from .memory import StructuredMemory
from .skills import SkillContext
from .step_progress import StepProgressMonitor


@dataclass(frozen=True)
class WaitConfig:
    """Framework waits are bounded wall time, never extra policy decisions."""
    enabled: bool = True
    download_visual_assist: bool = True  # Visual assistance only while download events are absent.
    poll_interval_s: float = 0.25
    download_start_timeout_s: float = 2.0
    render_timeout_s: float = 20.0
    download_max_bytes: int = 50 * 1024 * 1024
    operation_timeout_s: float = 300.0
    task_timeout_s: float = 1800.0

    def __post_init__(self):
        if type(self.download_max_bytes) is not int or self.download_max_bytes < 0:
            raise ValueError('wait_config.download_max_bytes must be a nonnegative integer')
        if type(self.download_visual_assist) is not bool:
            raise ValueError('download_visual_assist must be boolean')
        if type(self.enabled) is not bool:
            raise ValueError("wait_config.enabled must be boolean")
        for name in ("poll_interval_s", "download_start_timeout_s", "operation_timeout_s", "task_timeout_s", "render_timeout_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"wait_config.{name} must be a finite nonnegative number")
            if name != "download_start_timeout_s" and value == 0:
                raise ValueError(f"wait_config.{name} must be positive")


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
    evaluation_available: bool = False
    reward_source: str = "unavailable"
    termination_reason: str = "unknown"
    verification: Dict[str, Any] = field(default_factory=dict)
    planning_calls: List[dict] = field(default_factory=list)
    run_id: str = ""
    run_manifest: Dict[str, Any] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    private_evaluation: Dict[str, Any] = field(default_factory=dict)
    review_calls: List[dict] = field(default_factory=list)


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
    verification: Dict[str, Any] = field(default_factory=dict)
    task_profile: Dict[str, Any] = field(default_factory=dict)
    recovery: Dict[str, Any] = field(default_factory=dict)
    phase_events: List[dict] = field(default_factory=list)
    execution_feedback: Dict[str, Any] = field(default_factory=dict)
    failure_counts: Dict[str, int] = field(default_factory=dict)
    failure_class: str = ""
    failure_key: str = ""
    semantic_targets: Dict[str, Any] = field(default_factory=dict)
    waiting_download_ids: set = field(default_factory=set)
    last_browser_page: Dict[str, Any] = field(default_factory=dict)
    queue: Optional[ExecutionQueue] = None
    step_review: Dict[str, Any] = field(default_factory=dict)
    progress_monitor: Optional[StepProgressMonitor] = None
    step_progress: Dict[str, Any] = field(default_factory=dict)
    routing_hint: Dict[str, Any] = field(default_factory=dict)
    finish_feedback: str = ""
    render_waits: Dict[tuple, str] = field(default_factory=dict)
    control_mode: str = ''
    consumed_downloads: set = field(default_factory=set)
    download_facts: List[dict] = field(default_factory=list)
    final_review_state: Dict[str, Any] = field(default_factory=dict)
    operation_facts: List[str] = field(default_factory=list)
    paper_resolution: Dict[str, Any] = field(default_factory=dict)
    judge_rechecks: set = field(default_factory=set)
    judge_checkpoints: List[dict] = field(default_factory=list)
    unconfirmed_download_click: Dict[str, Any] = field(default_factory=dict)
    report_sources: List[dict] = field(default_factory=list)
    written_files: List[dict] = field(default_factory=list)
    report_delivery: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """③ 决策阶段的产出（先分析当前屏幕 → 证据先行 → 预期效果）。"""
    raw: str = ""
    action: Optional[Action] = None
    summary: str = ""                    # 一句话观察与行动计划；非完成证据
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
