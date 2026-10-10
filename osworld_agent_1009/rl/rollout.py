"""RL rollout：用本框架的 Agent 跑一条任务，产出「语义轨迹 + 最终 reward」。

这是语义轨迹树（semantic trajectory tree）credit assignment 的输入：
对同一任务跑 N 次 rollout，得到 N 条 (semantic_action 序列, 最终 reward)，
供后续建树、估计动作价值、算 step-level credit（见 credit.py / 指导手册）。

注意：语义动作（semantic action）不含 grounding 坐标，只有决策意图，
这样才能跨 rollout 比较「相同语义动作是否分叉」（见 semantic.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .semantic import semantic_action


@dataclass
class RolloutResult:
    """一次 rollout 的产物（RL 训练最小可用）。"""
    instruction: str
    semantic_actions: List[str] = field(default_factory=list)  # 每步语义动作（不含坐标）
    score: float = 0.0                 # OSWorld 最终 reward（0/1）
    success: bool = False
    steps: int = 0
    final_answer: str = ""
    # 阶段 2 补：每步决策的 logprobs（policy gradient 用），先留空
    logprobs: List[List[float]] = field(default_factory=list)
    token_ids: List[List[int]] = field(default_factory=list)


def run_rollout(agent, instruction: str, max_steps: int = 20) -> RolloutResult:
    """跑一条 rollout（单任务）。

    agent：本框架的 Agent（含 decision_model / grounding / env）。
    每次调用都会 env.reset（Agent.run 内部 Pipeline 第一次 preprocess 时 reset），
    所以可复用同一个 agent 对同一任务跑 N 次。
    """
    result = agent.run(instruction)

    semantic_actions: List[str] = []
    for step in (result.trajectory or []):
        action = step.get("action")
        if action:
            semantic_actions.append(semantic_action(action))

    return RolloutResult(
        instruction=instruction,
        semantic_actions=semantic_actions,
        score=float(getattr(result, "score", 0.0)),
        success=bool(getattr(result, "success", False)),
        steps=int(getattr(result, "steps", 0)),
        final_answer=str(getattr(result, "final_answer", "")),
    )


def run_rollouts(agent, instruction: str, n: int, max_steps: int = 20) -> List[RolloutResult]:
    """对同一任务跑 N 次 rollout，返回 N 条语义轨迹（语义树的输入）。"""
    return [run_rollout(agent, instruction, max_steps) for _ in range(n)]
