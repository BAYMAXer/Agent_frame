"""训练轨迹导出：把一次任务运行整理成「可直接喂 SFT / RL」的标准轨迹。

借鉴 osworld_agent_v2：轨迹记录 policy_input / policy_output / decision_calls 等
训练直接可用的字段，并提供 SFT / RL 导出器 + 外部数据集导入。

轨迹结构（一个任务一个 trajectory.json）：
{
  "version": "2.0",
  "task": {"id", "domain", "instruction"},
  "outcome": {"success", "score", "num_steps", "final_answer", "evaluation_available", ...},
  "steps": [
    {
      "step", "screenshot", "page_info",        # 决策前的观察（截图文件名 + 页面信息）
      "reasoning", "action", "grounding", "execution", "assessment",
      "policy_input", "policy_output", "decision_calls", "trainable",
      "reward", "done", "terminated", "truncated", "valid_action", ...
    }
  ]
}
"""
from __future__ import annotations

import json
import base64
import copy
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

TRAJECTORY_VERSION = "3.0"


def _parse_action(action_repr) -> Dict[str, Any]:
    if isinstance(action_repr, dict):
        return action_repr
    if isinstance(action_repr, str):
        try:
            return json.loads(action_repr)
        except (json.JSONDecodeError, TypeError):
            return {"raw": action_repr}
    return {"raw": str(action_repr)}


def build_trajectory(
    task_id: str,
    domain: str,
    instruction: str,
    result,
    decisions: Optional[List[Dict[str, Any]]] = None,
    groundings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """把一次 AgentResult 组装成标准训练轨迹 dict。"""
    decision_map = {}
    if decisions:
        for d in decisions:
            decision_map[d.get("step")] = d
    grounding_map: Dict[int, List[Dict[str, Any]]] = {}
    if groundings:
        for g in groundings:
            grounding_map.setdefault(g.get("step"), []).append(g)

    steps: List[Dict[str, Any]] = []
    for s in (result.trajectory or []):
        step_no = s.get("step")
        d = decision_map.get(step_no, {})

        if not s.get("action") and "policy_input" not in s:
            continue

        action = _parse_action(s.get("action"))

        assessment = s.get("assessment") or {}

        grounding = s.get("grounding")
        gs = grounding_map.get(step_no, [])
        if gs and not grounding:
            g = gs[0]
            grounding = {
                "target": g.get("query", ""),
                "coord": g.get("coord"),
                "elapsed": g.get("elapsed"),
            }

        steps.append({
            "step": step_no,
            "action_id": s.get("action_id") or f"a_{step_no}",
            "observation_id": s.get("observation_id") or f"o_{step_no}",
            "screenshot": d.get("screenshot_file") or f"step_{step_no}.png",
            "page_info": s.get("page_info", ""),
            "prompt_text": s.get("prompt_text", ""),
            "reasoning": {
                "summary": s.get("summary", ""),
                "screen_analysis": s.get("screen_analysis", ""),
                "expected_effect": s.get("expected_effect", ""),
                "state_update": s.get("state_update") or {},
            },
            "action": action,
            "grounding": grounding,
            "execution": {
                "command": s.get("executed", ""),
                "outcome": s.get("outcome", ""),
            },
            "assessment": assessment,
            "assessment_source": s.get("assessment_source") or assessment.get("source", "unspecified"),
            "reward": s.get("reward"),
            "done": bool(s.get("done", False)),
            "terminated": bool(s.get("terminated", False)),
            "truncated": bool(s.get("truncated", False)),
            "valid_action": s.get("valid_action", bool(s.get("action"))),
            "observation": s.get("observation") or {"id": f"o_{step_no}", "context": [], "screenshot": None},
            "next_observation": s.get("next_observation"),
            "policy_input": s.get("policy_input"),
            "policy_output": s.get("policy_output"),
            "decision_calls": s.get("decision_calls", []),
            "prompt_metadata": s.get("prompt_metadata", {}),
            "routing": s.get("routing", {}),
            "operation_mode": s.get("operation_mode"),
            "resolved_action": s.get("resolved_action"),
            "execution_info": s.get("execution_info", {}),
            "framework_operations": copy.deepcopy(s.get("framework_operations", [])),
            "verification": s.get("verification"),
            "step_verification": copy.deepcopy(s.get("step_verification", {})),
            "step_progress": copy.deepcopy(s.get("step_progress", {})),
            "step_reviews": copy.deepcopy(s.get("step_reviews", [])),
            "execution_queue": copy.deepcopy(s.get("execution_queue", {})),
            "plan_revision": s.get("plan_revision"),
            "plan_step_id": s.get("plan_step_id"),
            "plan_step": copy.deepcopy(s.get("plan_step", {})),
            "timing_ms": s.get("timing_ms", {}),
            "trainable": bool(s.get("policy_input") and s.get("policy_output") and s.get("valid_action", False)),
            "events": copy.deepcopy(s.get("events", [])),
        })

    evaluation_available = bool(getattr(result, "evaluation_available", False))
    raw_score = getattr(result, "score", None)
    reward_valid = (evaluation_available and isinstance(raw_score, (int, float))
                    and not isinstance(raw_score, bool) and math.isfinite(raw_score))
    return {
        "version": TRAJECTORY_VERSION,
        "task": {"id": task_id, "domain": domain, "instruction": instruction},
        "outcome": {
            "success": bool(getattr(result, "success", False)),
            "score": float(raw_score) if isinstance(raw_score, (int, float)) and math.isfinite(raw_score) else 0.0,
            "reward": float(raw_score) if reward_valid else None,
            "reward_valid": reward_valid,
            "num_steps": int(getattr(result, "steps", 0)),
            "final_answer": getattr(result, "final_answer", ""),
            "evaluation_available": bool(getattr(result, "evaluation_available", False)),
            "reward_source": getattr(result, "reward_source", "unavailable"),
            "termination_reason": getattr(result, "termination_reason", "unknown"),
            "verification": getattr(result, "verification", {}),
            "run_metrics": copy.deepcopy(getattr(result, 'run_manifest', {}).get('run_metrics', {})),
        },
        "steps": steps,
        "plan": getattr(result, "plan", None),
        "planning_calls": getattr(result, "planning_calls", []),
        "review_calls": copy.deepcopy(getattr(result, "review_calls", [])),
        "run_manifest": copy.deepcopy(getattr(result, "run_manifest", {})),
        "events": copy.deepcopy(getattr(result, "events", [])),
        # Full evidence is archival data; it is never injected into training prompts.
        "private_evaluation": copy.deepcopy(getattr(result, "private_evaluation", {})),
    }


def dump_trajectory(path: str, trajectory: Dict[str, Any]) -> None:
    """Content-address images，把内嵌 base64 图片外置成 assets/ 下的去重文件。"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    from .training_events import externalize_images
    packed = externalize_images(trajectory, output.parent)
    for step in packed["steps"]:
        if step.get("observation", {}).get("screenshot"):
            step["screenshot"] = step["observation"]["screenshot"]
    output.write_text(json.dumps(packed, ensure_ascii=False, indent=2), encoding="utf-8")


def _reward_is_valid(outcome):
    value = outcome.get("reward", outcome.get("score"))
    return (outcome.get("evaluation_available") is True
            and outcome.get("reward_valid", True) is True
            and outcome.get("reward_source") not in (None, "", "unavailable")
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _positive_step(step):
    """A syntactically valid action is not necessarily a positive demonstration."""
    if not step.get("trainable"):
        return False
    info = step.get("execution_info") or {}
    assessment = step.get("assessment") or {}
    if info.get("framework_replaced_action") or step.get("framework_replaced_action"):
        return False
    if info.get("status") in {"rejected", "failed", "failure", "uncertain", "error"}:
        return False
    if assessment.get("action_status") in {"failure", "failed", "uncertain"}:
        return False
    if step.get("error") or info.get("error"):
        return False
    action = _parse_action(step.get("action"))
    if action.get("action") in {"fail"}:
        return False
    if action.get("action") == "request_finish":
        return (step.get("verification") or {}).get("status") == "passed"
    return True


def _model_call_events(trajectory):
    """Index causal call events by append order, independent of wall-clock drift."""
    indexed = {}
    for index, event in enumerate(trajectory.get("events", [])):
        if event.get("event_type") != "model_call":
            continue
        call_id = (event.get("call") or {}).get("call_id") or event.get("call_id")
        if not call_id:
            continue  # Older events did not have stable call identities.
        if call_id in indexed:
            raise ValueError("Duplicate model call_id in trajectory events")
        indexed[call_id] = (index, event)
    return indexed


def _call_association(trajectory, call, events, step=None):
    """Expose identifiers only; never copy private event payloads into samples."""
    call_id = call.get("call_id")
    indexed = events.get(call_id)
    index, event = indexed if indexed is not None else (None, {})
    parent = event.get("parent_decision_id") or call.get("parent_decision_id")
    linked_step = step
    if linked_step is None and parent:
        linked_step = next((item for item in trajectory.get("steps", [])
            if (item.get("action_id") or f"a_{item.get('step')}") == parent), None)
    if parent is None and linked_step is not None:
        parent = linked_step.get("action_id") or f"a_{linked_step.get('step')}"
    return {"call_id": call_id, "event_id": event.get("event_id") or call.get("event_id"),
            "parent_decision_id": parent,
            "observation_id": (event.get("observation_id") or call.get("observation_id")
                or (step.get("observation_id") or f"o_{step.get('step')}"
                    if step is not None else None)),
            "step": linked_step.get("step") if linked_step is not None else call.get("step"),
            "plan_stage": call.get("plan_stage"), "event_index": index}


def iter_sft_records(trajectory, require_success=True, *, actor_role="decision"):
    """SFT 记录导出：所有域统一，prompt 不做重建、不注入 hindsight。

    成功 = 独立评测器通过；模型自述不是标签。loss 只对 completion 算（completion_only）。
    """
    outcome = trajectory.get("outcome", {})
    if trajectory.get("provenance", {}).get("use_for_model_training") is False:
        return
    if trajectory.get("run_manifest", {}).get("private_evaluation_in_prompt"):
        raise ValueError("Private evaluator data must never enter policy prompts")
    if actor_role not in {"decision", "planner"}:
        raise ValueError("Only planner and decision policies are trainable; grounding is frozen")
    if require_success and not (_reward_is_valid(outcome) and outcome.get("score", 0) >= 1):
        return
    call_events = _model_call_events(trajectory)
    if actor_role == "planner":
        for index, call in enumerate(trajectory.get("planning_calls", [])):
            if (not call.get("messages") or not call.get("response")
                    or call.get("valid_plan") is not True or call.get("error_type")
                    or call.get("finish_reason") == "length"):
                continue
            yield {"prompt": copy.deepcopy(call["messages"]),
                   "completion": [{"role": "assistant", "content": call["response"]}],
                   "actor_role": "planner", "task_id": trajectory["task"]["id"],
                   **_call_association(trajectory, call, call_events),
                   "attempt": index, "loss_scope": "completion_only", "schema_version": TRAJECTORY_VERSION}
        return
    for step in trajectory.get("steps", []):
        if not _positive_step(step):
            continue
        calls = step.get("decision_calls") or []
        if calls and (calls[-1].get("finish_reason") == "length" or calls[-1].get('parser_normalization')):
            # Preserve the raw rollout, but do not teach malformed shorthand as
            # a positive SFT answer or rewrite it into unsampled model tokens.
            continue
        yield {"prompt": copy.deepcopy(step["policy_input"]),
               "completion": [{"role": "assistant", "content": step["policy_output"]}],
               "task_id": trajectory["task"]["id"], "step": step["step"],
               **_call_association(trajectory, calls[-1] if calls else {}, call_events, step),
               "actor_role": "decision", "loss_scope": "completion_only", "schema_version": TRAJECTORY_VERSION}


def restore_messages(messages, trajectory_path):
    """用保存的图片字节重放消息（恢复 base64 data URL）。"""
    root = Path(trajectory_path).resolve().parent
    restored = copy.deepcopy(messages)
    for message in restored:
        if not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if part.get("type") != "image_url":
                continue
            url = part["image_url"]["url"]
            if url.startswith("assets/"):
                path = (root / url).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Image path escapes trajectory directory")
                mime = "image/jpeg" if path.suffix == ".jpg" else "image/png"
                part["image_url"]["url"] = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
    return restored


def import_external_episode(task_id, domain, instruction, records, *, source, score=None):
    """导入外部数据集（自己采集 / 网上智能体轨迹），不编造缺失的 policy input。"""
    from .actions import parse_decision, action_from_decision, validate_action
    from .pipeline import AgentResult
    result = AgentResult(task=instruction, score=float(score or 0), steps=len(records),
                         evaluation_available=score is not None,
                         success=score is not None and score >= 1,
                         termination_reason="external_dataset")
    for i, record in enumerate(records, 1):
        response = record.get("response")
        action = action_from_decision(parse_decision(response) or {}) if isinstance(response, str) else None
        valid = bool(action and validate_action(action) is None)
        observation = copy.deepcopy(record.get("observation") or {})
        observation.setdefault("id", f"o_{i}")
        observation.setdefault("context", [])
        observation.setdefault("screenshot", record.get("screenshot"))
        result.trajectory.append({"step": i, "observation": observation,
            "next_observation": record.get("next_observation"),
            "action": {"action": action.action, "args": action.args} if action else record.get("action"),
            "policy_input": copy.deepcopy(record.get("messages")), "policy_output": response,
            "valid_action": valid, "reward": record.get("reward"),
            "terminated": bool(record.get("terminated", False)),
            "truncated": bool(record.get("truncated", False)),
            "done": bool(record.get("terminated") or record.get("truncated")),
            "prompt_metadata": {"origin": "external", "source": source,
                                "input_fidelity": "provided" if record.get("messages") else "missing"},
            "decision_calls": record.get("decision_calls", [])})
    trajectory = build_trajectory(task_id, domain, instruction, result)
    trajectory["outcome"]["reward_source"] = "external_dataset_label" if score is not None else "unavailable"
    trajectory["provenance"] = {"source": source, "policy": "external", "use_for_model_training": True}
    return trajectory


def iter_on_policy_records(trajectory, policy_revision):
    """Export sampled calls, never reconstruct tokenization or fabricate logprobs.

    This is a collector interchange format, not a claim that any trainer accepts
    it unchanged. A trainable planner participates in the same behavior policy;
    a separate teacher must explicitly mark its calls policy_trainable=False.
    """
    outcome = trajectory.get("outcome", {})
    if not _reward_is_valid(outcome):
        raise ValueError("Independent episode reward is required")
    if not policy_revision:
        raise ValueError("A fixed behavior policy revision is required")
    if trajectory.get("provenance", {}).get("use_for_model_training") is False:
        raise ValueError("Test fixtures are not policy rollouts")
    if trajectory.get("provenance", {}).get("policy") == "external":
        raise ValueError("External demonstrations are not on-policy rollouts")
    if trajectory.get("run_manifest", {}).get("private_evaluation_in_prompt"):
        raise ValueError("Private evaluator data must never enter policy prompts")
    for step in trajectory.get("steps", []):
        if ((step.get("action") or {}).get("action") == "call_code_agent"
                or (step.get("execution_info") or {}).get("action") == "call_code_agent"):
            raise ValueError("Code-agent sampling is not captured; this episode is not an on-policy rollout")
    samples = []
    call_events = _model_call_events(trajectory)

    def append_call(call, role, step, attempt, *, last=False):
        if call.get("policy_trainable") is False:
            return
        if call.get("policy_trainable") is not True:
            raise ValueError("Every model call must explicitly declare policy_trainable")
        if call.get("error_type") and not call.get("response"):
            return  # A transport error sampled no completion tokens.
        if (call.get("rl_ready") is not True or call.get("policy_revision_source") != "server"
                or call.get("rollout_validation_errors") or call.get("capture_validation_errors")):
            raise ValueError("Call is not RL-ready according to server metadata validation")
        if call.get("expected_policy_revision") and call["expected_policy_revision"] != policy_revision:
            raise ValueError("Requested revision differs from the configured behavior policy")
        ids, lp = call.get("token_ids"), call.get("token_logprobs")
        prompt_ids = call.get("prompt_token_ids")
        valid_ids = lambda value: (isinstance(value, list) and bool(value)
            and all(isinstance(token, int) and not isinstance(token, bool) and token >= 0 for token in value))
        if (call.get("policy_revision") != policy_revision or not valid_ids(ids)
                or not valid_ids(prompt_ids) or not isinstance(lp, list) or len(ids) != len(lp)
                or any(not isinstance(p, (int, float)) or isinstance(p, bool)
                       or not math.isfinite(p) or p > 1e-5 for p in lp)):
            raise ValueError("Need matching server policy_revision, prompt_token_ids, token_ids and finite token_logprobs")
        revisions = {key: call.get(key) for key in (
            "tokenizer_revision", "processor_revision", "chat_template_revision")}
        if not all(isinstance(value, str) and value for value in revisions.values()):
            raise ValueError("Tokenizer, processor and chat template revisions are required")
        if not call.get("messages") or not isinstance(call.get("response"), str):
            raise ValueError("Original policy messages and response are required")
        if call.get("private_evaluation_in_prompt"):
            raise ValueError("Private evaluator data must never enter policy prompts")
        samples.append({"prompt": copy.deepcopy(call["messages"]), "completion": call["response"],
            "actor_role": role, "policy_revision": policy_revision, **revisions,
            "prompt_token_ids": list(prompt_ids), "token_ids": list(ids),
            "behavior_logprobs": list(lp), "generation": copy.deepcopy(call.get("generation", {})),
            "loss_mask": [1] * len(ids), "finish_reason": call.get("finish_reason"),
            "step": step.get("step") if step else None, "attempt": attempt,
            "valid_action": bool(call.get("valid_action")) if role == "decision" else None,
            "parser_normalization": call.get('parser_normalization'),
            "parsed_action": copy.deepcopy(call.get('parsed_action')),
            "valid_plan": call.get("valid_plan") if role == "planner" else None,
            "episode_reward": outcome.get("reward", outcome.get("score")), "reward_valid": True,
            "reward_source": outcome.get("reward_source", "unspecified"),
            "termination_reason": outcome.get("termination_reason", "unknown"),
            "step_reward": step.get("reward") if step and last else None,
            "terminated": bool(step and last and step.get("terminated")),
            "truncated": bool(step and last and step.get("truncated")),
            "next_observation": copy.deepcopy(step.get("next_observation")) if step and last else None,
            "event_id": call.get("event_id"), "call_id": call.get("call_id"), "loss_scope": "completion_only",
            **_call_association(trajectory, call, call_events, step),
            "schema_version": TRAJECTORY_VERSION})

    for attempt, call in enumerate(trajectory.get("planning_calls", [])):
        append_call(call, "planner", None, attempt)
    for step in trajectory.get("steps", []):
        calls = step.get("decision_calls", [])
        if not calls:
            raise ValueError("Missing decision call records")
        for attempt, call in enumerate(calls):
            append_call(call, "decision", step, attempt, last=attempt == len(calls) - 1)
    if call_events:
        if any(sample["event_index"] is None for sample in samples):
            raise ValueError("Model-call event stream is incomplete; cannot establish on-policy call order")
        samples.sort(key=lambda sample: sample["event_index"])
    for sample in samples:
        # Legacy data remains exportable, without claiming reconstructed chronology.
        sample["call_order_source"] = "events" if call_events else "legacy_role_order"
    yield from samples


def adapt_sft_images(record, trajectory_path):
    """Convert image_url parts to image placeholders plus a portable images list.

    Paths are absolute and image order follows the exact original messages. A
    training-side dataset loader must decode these paths and apply the chosen
    model's processor/chat template; loss_scope is metadata, not a loss mask.
    """
    converted = copy.deepcopy(record)
    root = Path(trajectory_path).resolve().parent
    images = []
    for message in converted["prompt"]:
        if not isinstance(message.get("content"), list):
            continue
        parts = []
        for part in message["content"]:
            if part.get("type") != "image_url":
                parts.append(part)
                continue
            location = part["image_url"]["url"]
            if not location.startswith("assets/"):
                raise ValueError("Persist trajectory images before adapting SFT records")
            image_path = (root / location).resolve()
            if not image_path.is_relative_to(root) or not image_path.is_file():
                raise ValueError("Missing or unsafe trajectory image asset")
            images.append(str(image_path))
            parts.append({"type": "image"})
        message["content"] = parts
    converted["images"] = images
    converted["format"] = "messages_with_image_paths"
    return converted


def main(argv=None):
    """python -m osworld_agent.trajectory INPUT... --output FILE --mode ..."""
    import argparse
    parser = argparse.ArgumentParser(description="Export recorded planner/decision data; no model calls")
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("sft-decision", "sft-planner", "rl"), default="sft-decision")
    parser.add_argument("--policy-revision")
    parser.add_argument("--image-paths", action="store_true",
                        help="SFT: emit image placeholders and an images path list for a processor adapter")
    args = parser.parse_args(argv)
    records = []
    for filename in args.inputs:
        trajectory = json.loads(Path(filename).read_text(encoding="utf-8"))
        iterator = (iter_on_policy_records(trajectory, args.policy_revision) if args.mode == "rl"
                    else iter_sft_records(trajectory, actor_role=args.mode.removeprefix("sft-")))
        for record in iterator:
            record["source_trajectory"] = str(Path(filename).resolve())
            if args.image_paths:
                if args.mode == "rl":
                    parser.error("--image-paths is for SFT; RL preserves exact policy messages")
                record = adapt_sft_images(record, filename)
            else:
                record["prompt"] = restore_messages(record["prompt"], filename)
            records.append(record)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    print(f"Exported {len(records)} {args.mode} records to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
