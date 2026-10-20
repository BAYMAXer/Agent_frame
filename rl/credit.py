"""语义轨迹树 + Step-Level Credit Assignment（指导手册核心方法）。

对同一任务的 N 条 rollout（语义动作序列 + 最终 reward），构建语义轨迹树：
只有拥有相同 semantic history prefix 的轨迹才共享节点；一旦分叉，后续不再因
screenshot 相似而合并。

然后在树上估计：
- 动作价值  Q(h,a) = P(R=1|h,a)，用 Bayesian smoothing（Beta 先验 α=β=1）
- 状态价值  V(h)   = Σ_a π(a|h) Q(h,a)
- 步级 credit C(h,a) = Q(h,a) - V(h)
- 每条轨迹 signed L1 归一化：w_t = C_t / (Σ|C_j| + ε)

最终 policy objective（训练侧）：L = -Σ_t w_t log π(a_t|h_t)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math

from .semantic import semantic_action


# Bayesian smoothing 先验（指导手册第一版 α=β=1 均匀先验）
ALPHA = 1.0
BETA = 1.0
EPS = 1e-8


class TreeNode:
    """语义树节点 = 一个语义历史前缀 h。"""

    def __init__(self):
        # 每条出边（动作）的统计：action -> {"s": 成功次数, "n": 总次数}
        self.edge_stats: Dict[str, Dict[str, int]] = {}
        # 子节点：action -> TreeNode（历史 h + 动作 a）
        self.children: Dict[str, "TreeNode"] = {}

    def total_n(self) -> int:
        """该节点所有出边被走过的总次数 = Σ_a N(h,a)。"""
        return sum(st["n"] for st in self.edge_stats.values())


def _validate_group(rollouts):
    """Never infer a failed episode from unavailable evaluation or mix initial states."""
    groups = set()
    for rollout in rollouts:
        score = getattr(rollout, "score", None)
        if (not getattr(rollout, "evaluation_available", False)
                or not getattr(rollout, "reward_valid", False)
                or not isinstance(score, (int, float)) or isinstance(score, bool)
                or not math.isfinite(score)):
            raise ValueError("Credit assignment requires valid independent rewards; unknown is not failure")
        initial_state = getattr(rollout, "initial_state_id", "")
        revision = getattr(rollout, "policy_revision", "")
        instruction = getattr(rollout, "instruction", "")
        task_spec_hash = getattr(rollout, "task_spec_hash", "")
        if not initial_state or not revision or not instruction or not task_spec_hash:
            raise ValueError("Credit grouping needs instruction, task_spec_hash, initial_state_id and fixed policy_revision")
        groups.add((instruction, task_spec_hash, initial_state, revision,
                    getattr(rollout, "runtime_config_hash", ""),
                    getattr(rollout, "goal_binding_hash", "")))
    if len(groups) > 1:
        raise ValueError("Split rollouts by task, initial_state_id, policy_revision, runtime configuration and goal binding before assigning credit")
    return next(iter(groups), None)


def build_tree(rollouts: List) -> TreeNode:
    """用 N 条 rollout 构建语义轨迹树。

    rollouts: List[RolloutResult]（含 semantic_actions + score）。
    """
    root = TreeNode()
    root.group_key = _validate_group(rollouts)
    for r in rollouts:
        node = root
        success = 1.0 if r.score >= 1.0 else 0.0
        for a in r.semantic_actions:
            st = node.edge_stats.setdefault(a, {"s": 0, "n": 0})
            st["n"] += 1
            st["s"] += int(success)
            node = node.children.setdefault(a, TreeNode())
    return root


def action_value(node: TreeNode, action: str, alpha: float = ALPHA, beta: float = BETA) -> float:
    """Bayesian-smoothed Q(h,a) = (S+α) / (N+α+β)。"""
    st = node.edge_stats.get(action)
    if st is None:
        return 0.0
    s, n = st["s"], st["n"]
    return (s + alpha) / (n + alpha + beta)


def state_value(node: TreeNode, alpha: float = ALPHA, beta: float = BETA) -> float:
    """V(h) = Σ_a π(a|h) Q(h,a)，π(a|h) = N(h,a) / Σ_a' N(h,a')。"""
    total = node.total_n()
    if total == 0:
        return 0.0
    v = 0.0
    for a, st in node.edge_stats.items():
        pi = st["n"] / total
        q = (st["s"] + alpha) / (st["n"] + alpha + beta)
        v += pi * q
    return v


def step_credits(root: TreeNode, rollouts: List, alpha=ALPHA, beta=BETA) -> List[List[float]]:
    """对每条 rollout 计算每步的 credit C_t = Q(h_t, a_t) - V(h_t)。

    返回与 rollouts 对齐的 credit 列表（每条一个 list，长度 = 该条步数）。
    """
    if _validate_group(rollouts) != getattr(root, "group_key", None):
        raise ValueError("Credit tree belongs to a different rollout group")
    all_credits: List[List[float]] = []
    for r in rollouts:
        node = root
        credits: List[float] = []
        for a in r.semantic_actions:
            q = action_value(node, a, alpha, beta)
            v = state_value(node, alpha, beta)
            credits.append(q - v)
            node = node.children.get(a)
            if node is None:  # 理论上不会发生（树已构建）
                raise ValueError("Rollout prefix is not present in credit tree")
        all_credits.append(credits)
    return all_credits


def normalize_credits(credits: List[float], eps: float = EPS) -> List[float]:
    """signed L1 归一化：w_t = C_t / (Σ|C_j| + ε)。"""
    denom = sum(abs(c) for c in credits) + eps
    return [c / denom for c in credits]


def assign_step_level_credit(rollouts: List, alpha: float = ALPHA, beta: float = BETA,
                             normalize: bool = True) -> List[List[float]]:
    """一站式：建树 → 算 credit → 归一化，返回每条 rollout 的 w_t 序列。

    供训练侧：L = -Σ_t w_t log π(a_t|h_t)，w_t>0 提高、w_t<0 降低动作概率。
    """
    if alpha <= 0 or beta <= 0:
        raise ValueError("Beta prior parameters must be positive")
    root = build_tree(rollouts)
    credits = step_credits(root, rollouts, alpha, beta)
    if normalize:
        return [normalize_credits(c) for c in credits]
    return credits
