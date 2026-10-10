"""osworld_agent 的 RL 训练模块。

迁移自 AgentS/rl_training，核心是「语义轨迹树 + step-level credit assignment」：
- semantic.py：语义动作规范化（action JSON → 意图级规范串，去坐标）
- rollout.py：用本框架 Agent 跑任务，产出语义轨迹 + 最终 reward
- credit.py：语义轨迹树 + Bayesian smoothing 动作价值 + step-level credit（后续）
"""
from .semantic import semantic_action, semantic_history, parse_action
from .rollout import RolloutResult, run_rollout, run_rollouts
from .credit import (
    build_tree, action_value, state_value, step_credits,
    normalize_credits, assign_step_level_credit,
)

__all__ = [
    "semantic_action", "semantic_history", "parse_action",
    "RolloutResult", "run_rollout", "run_rollouts",
    "build_tree", "action_value", "state_value", "step_credits",
    "normalize_credits", "assign_step_level_credit",
]
