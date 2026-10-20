"""Isolated episode collection; unknown evaluator outcomes are masked, never zero labels."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import inspect
import hashlib
import json
import math
from pathlib import Path
import uuid
from typing import Optional

from .semantic import semantic_action


@dataclass
class RolloutResult:
    instruction: str
    semantic_actions: list[str] = field(default_factory=list)
    score: Optional[float] = None
    success: bool = False
    steps: int = 0
    final_answer: str = ""
    evaluation_available: bool = False
    reward_valid: bool = False
    reward_source: str = "unavailable"
    termination_reason: str = "unknown"
    initial_state_id: str = ""
    episode_id: str = ""
    policy_revision: str = ""
    task_spec_hash: str = ""
    runtime_config_hash: str = ""
    goal_binding_hash: str = ""
    trajectory: dict = field(default_factory=dict)
    initial_state: dict = field(default_factory=dict)


def run_rollout(agent, instruction: str, max_steps: int = 40, *, env_factory=None,
                completion_spec=None, private_completion_spec=None, output_dir=None, policy_revision=None):
    """A fresh factory environment is always closed, including setup failures."""
    original_env = agent.env
    episode_env = env_factory() if env_factory else original_env
    code_agent = getattr(agent, "code_agent", None)
    original_code_env = getattr(code_agent, "env", None)
    try:
        agent.env = episode_env
        if code_agent is not None:
            code_agent.env = episode_env
        return _collect_episode(agent, instruction, max_steps, completion_spec=completion_spec,
            private_completion_spec=private_completion_spec, output_dir=output_dir,
            policy_revision=policy_revision)
    finally:
        agent.env = original_env
        if code_agent is not None:
            code_agent.env = original_code_env
        if env_factory and episode_env is not None:
            episode_env.close()


def _collect_episode(agent, instruction, max_steps, *, completion_spec,
                     private_completion_spec, output_dir, policy_revision):
    """Require begin_episode isolation; reset-as-observe is insufficient.

    env_factory may supply a fresh environment, which must still expose the
    isolation manifest. max_steps is passed per run, never silently ignored.
    """
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        raise ValueError("max_steps must be a positive integer")
    episode_id = uuid.uuid4().hex
    episode_env = agent.env
    if episode_env is None:
        raise ValueError("Environment factory returned no environment")
    begin = getattr(episode_env, "begin_episode", None)
    if not callable(begin):
        raise ValueError("RL collection requires begin_episode with explicit isolation; reset is insufficient")
    initial_state = begin(task_id=episode_id, isolated=True)
    if (not isinstance(initial_state, dict) or initial_state.get("isolated") is not True
            or not isinstance(initial_state.get("initial_state_id"), str)
            or not initial_state["initial_state_id"]):
        raise ValueError("Environment did not prove isolation and a stable initial_state_id")
    signature = inspect.signature(agent.run)
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
    supports = lambda name: accepts_kwargs or name in signature.parameters
    if not supports("max_steps"):
        raise ValueError("Agent.run must accept max_steps; collector never mutates shared agent configuration")
    kwargs = {"max_steps": max_steps}
    if completion_spec is not None:
        if not supports("completion_spec"):
            raise ValueError("Agent.run does not accept the supplied completion contract")
        kwargs["completion_spec"] = completion_spec
    if private_completion_spec is not None:
        if not supports("private_completion_spec"):
            raise ValueError("Agent.run does not accept private evaluator configuration")
        kwargs["private_completion_spec"] = private_completion_spec
    episode_dir = None
    if output_dir is not None:
        episode_dir = Path(output_dir) / episode_id
        episode_dir.mkdir(parents=True, exist_ok=False)
        if not supports("event_path"):
            raise ValueError("Durable collection requires Agent.run(event_path=...)")
        kwargs["event_path"] = str(episode_dir / "events.jsonl")
    if supports("run_id"):
        kwargs["run_id"] = episode_id
    result = agent.run(instruction, **kwargs)
    available = bool(getattr(result, "evaluation_available", False))
    score = getattr(result, "score", None)
    valid = (available and isinstance(score, (int, float)) and not isinstance(score, bool)
             and math.isfinite(score))
    from ..trajectory import build_trajectory, dump_trajectory, iter_on_policy_records
    trajectory = build_trajectory(episode_id, "browser", instruction, result)
    task_spec_hash = trajectory["run_manifest"].get("completion_hash") or hashlib.sha256(
        json.dumps({"instruction": instruction, "public": completion_spec,
                    "private": private_completion_spec}, sort_keys=True).encode()).hexdigest()
    trajectory["run_manifest"].update({"episode_id": episode_id,
        "initial_state": copy.deepcopy(initial_state), "initial_state_id": initial_state["initial_state_id"],
        "max_steps": max_steps, "task_spec_hash": task_spec_hash})
    if episode_dir is not None:
        dump_trajectory(str(episode_dir / "trajectory.json"), trajectory)
    if policy_revision is not None and valid:
        list(iter_on_policy_records(trajectory, policy_revision))
    return RolloutResult(
        instruction=instruction,
        semantic_actions=[_step_semantic(step) for step in result.trajectory
                          if step.get("action") or step.get("decision_calls")],
        score=float(score) if valid else None, success=bool(result.success) and valid,
        steps=int(result.steps), final_answer=result.final_answer,
        evaluation_available=available, reward_valid=valid,
        reward_source=getattr(result, "reward_source", "unavailable") if valid else "unavailable",
        termination_reason=getattr(result, "termination_reason", "unknown"),
        initial_state_id=initial_state["initial_state_id"], episode_id=episode_id,
        policy_revision=policy_revision or "", trajectory=trajectory,
        task_spec_hash=task_spec_hash,
        runtime_config_hash=trajectory["run_manifest"].get("runtime_config_hash", ""),
        goal_binding_hash=trajectory["run_manifest"].get("goal_binding_hash", ""),
        initial_state=copy.deepcopy(initial_state))


def _step_semantic(step):
    """Credit uses sampled intent, including invalid calls, not framework repairs."""
    if not step.get("action"):
        return "invalid_decision()"
    from .semantic import parse_action
    action = copy.deepcopy(parse_action(step["action"]))
    if action.get("action") == "rename_artifact":
        args = action.setdefault("args", {})
        artifact_id = args.get("artifact_id")
        artifacts = ((step.get("observation") or {}).get("info") or {}).get("artifacts", [])
        artifact = next((a for a in artifacts if isinstance(a, dict) and
                         (a.get("artifact_id") or a.get("download_id")) == artifact_id), None)
        args["artifact_source"] = ((artifact.get("source_url") or artifact.get("url") or
                                    artifact.get("initial_sha256") or artifact.get("sha256"))
                                   if artifact else "unresolved_artifact")
    return semantic_action(action)


def run_rollouts(agent, instruction, n, max_steps=40, **kwargs):
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive integer")
    return [run_rollout(agent, instruction, max_steps, **kwargs) for _ in range(n)]


def main(argv=None):
    """Collect with a configured agent factory, without a trainer dependency."""
    import argparse
    import importlib
    import json
    parser = argparse.ArgumentParser(description="Collect isolated episodes with durable per-call events")
    parser.add_argument("--factory", required=True, help="Python module:function returning a configured Agent")
    parser.add_argument("--tasks", required=True, help="JSONL objects: instruction and optional completion_spec")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--policy-revision", help="Validate on-policy metadata when reward is available")
    args = parser.parse_args(argv)
    module_name, separator, function_name = args.factory.partition(":")
    if not separator:
        parser.error("--factory must have module:function form")
    factory = getattr(importlib.import_module(module_name), function_name)
    tasks = [json.loads(line) for line in Path(args.tasks).read_text(encoding="utf-8").splitlines() if line.strip()]
    for task in tasks:
        agent = factory()
        try:
            episodes = run_rollouts(agent, task["instruction"], args.n, args.max_steps,
                completion_spec=task.get("completion_spec"),
                private_completion_spec=task.get("private_completion_spec"), output_dir=args.output_dir,
                policy_revision=args.policy_revision)
            for episode in episodes:
                print(json.dumps({"episode_id": episode.episode_id, "reward": episode.score,
                                  "reward_valid": episode.reward_valid, "steps": episode.steps}))
        finally:
            agent.env.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
