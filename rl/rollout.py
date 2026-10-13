"""RL rollout：用本框架的 Agent 跑一条任务，产出「语义轨迹 + 最终 reward」。

这是语义轨迹树（semantic trajectory tree）credit assignment 的输入：
对同一任务跑 N 次 rollout，得到 N 条 (semantic_action 序列, 最终 reward)，
供后续建树、估计动作价值、算 step-level credit（见 credit.py / 指导手册）。

注意：语义动作（semantic action）不含 grounding 坐标，只有决策意图，
这样才能跨 rollout 比较「相同语义动作是否分叉」（见 semantic.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .semantic import semantic_action


@dataclass
class RolloutResult:
    """一次 rollout 的产物（RL 训练最小可用）。"""
    instruction: str
    semantic_actions: List[str] = field(default_factory=list)  # 每步语义动作（不含坐标）
    score: float = 0.0                 # 保留 AgentResult.score；不冒充独立环境 reward
    success: bool = False
    steps: int = 0
    final_answer: str = ""
    # 阶段 2 补：每步决策的 logprobs（policy gradient 用），先留空
    logprobs: List[List[float]] = field(default_factory=list)
    token_ids: List[List[int]] = field(default_factory=list)


def run_rollout(agent, instruction: str, max_steps: int = 20) -> RolloutResult:
    """跑一条 rollout（单任务）。

    agent：连接 Windows 本机环境的 Agent（含 model / grounding / env）。
    reset 仅刷新会话与观察，不还原用户文件或网页状态。重复实验前，调用方
    必须自行恢复本机任务初始状态。max_steps 只在本次运行期间生效。
    """
    env = getattr(agent, "env", None)
    if (getattr(env, "os_name", None) != "windows"
            or getattr(env, "local_environment", False) is not True):
        raise ValueError("rollout 仅支持 Windows 本机环境")
    if type(max_steps) is not int or max_steps <= 0:
        raise ValueError("max_steps 必须是正整数")
    previous_max_steps = agent.max_steps
    try:
        agent.max_steps = max_steps
        result = agent.run(instruction)
    finally:
        agent.max_steps = previous_max_steps

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
    """收集 N 条连续本机轨迹，不承诺自动恢复相同初始状态。

    需要独立样本时，调用方应逐次调用 run_rollout 并在两次之间恢复任务状态。
    """
    if type(n) is not int or n < 0:
        raise ValueError("n 必须是非负整数")
    return [run_rollout(agent, instruction, max_steps) for _ in range(n)]
