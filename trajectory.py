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
import hashlib
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

TRAJECTORY_VERSION = "2.0"

_FAILURE_MESSAGES = {
    "planning_error": "任务规划失败",
    "step_budget": "已达到最大执行步数，任务尚未完成",
    "repeated_action": "连续重复动作已触发停止，任务尚未完成",
    "recovery_exhausted": "恢复尝试已耗尽，任务尚未完成",
    "download_configuration_error": "浏览器下载配置失败，任务尚未完成",
    "final_verification_failed": "最终任务核验未通过",
    "completion_rejected": "子任务完成证据反复未通过核验",
    "context_budget": "输入超出上下文预算",
    "model_timeout": "模型请求超过时间预算",
    "runtime_error": "执行过程中发生异常",
    "interrupted": "任务被手动中断",
    "failed": "任务未完成",
}


def _outcome_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_outcome(outcome: Dict[str, Any], plan=None) -> Dict[str, Any]:
    """Return a coherent display/export outcome without changing saved history."""
    normalized = dict(outcome)
    status = _outcome_text(normalized.get("status"))
    success = normalized.get("success")
    if status in {"failed", "interrupted"}:
        normalized["success"] = False
    elif status == "completed" and success is not True:
        status = "failed"
        normalized["success"] = False
    elif status != "running" and success is False:
        status = "failed"
    elif not status and isinstance(success, bool):
        status = "completed" if success else "failed"
    if status:
        normalized["status"] = status
    reason = _outcome_text(normalized.get("termination_reason"))
    if reason in {"", "unknown"}:
        reason = "success" if status == "completed" else status or "unknown"
    if status == "interrupted":
        reason = "interrupted"
    elif status == "failed" and reason == "success":
        reason = "runtime_error"
    normalized["termination_reason"] = reason
    if status == "completed":
        normalized["error"] = ""
    elif status in {"failed", "interrupted"}:
        plan_steps = plan if isinstance(plan, list) else plan.get("steps", []) if isinstance(plan, dict) else []
        blocked = next((_outcome_text(step.get("blocked_reason"))
                        for step in plan_steps
                        if isinstance(step, dict) and _outcome_text(step.get("blocked_reason"))), "")
        normalized["error"] = (_outcome_text(normalized.get("error"))
            or ("" if status == "interrupted" else _outcome_text(normalized.get("final_answer")))
            or ("" if status == "interrupted" else blocked)
            or _FAILURE_MESSAGES.get(reason, _FAILURE_MESSAGES[status]))
    return normalized


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
        if gs:
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
            "model_calls": s.get("model_calls", s.get("decision_calls", [])),
            "prompt_metadata": s.get("prompt_metadata", {}),
            "routing": s.get("routing", {}),
            "resolved_action": s.get("resolved_action"),
            "execution_info": s.get("execution_info", {}),
            "verification": s.get("verification"),
            "plan_step_id": s.get("plan_step_id"),
            "plan_snapshot": s.get("plan_snapshot"),
            "timing_ms": s.get("timing_ms", {}),
            "trainable": bool(s.get("policy_input") and s.get("policy_output") and s.get("valid_action", False)),
        })

    return {
        "version": TRAJECTORY_VERSION,
        "task": {"id": task_id, "domain": domain, "instruction": instruction},
        "outcome": normalize_outcome({
            "success": bool(getattr(result, "success", False)),
            "score": float(getattr(result, "score", 0.0)),
            "num_steps": int(getattr(result, "steps", 0)),
            "final_answer": getattr(result, "final_answer", ""),
            "evaluation_available": bool(getattr(result, "evaluation_available", False)),
            "reward_source": "environment_evaluator" if getattr(result, "evaluation_available", False) else "unavailable",
            "termination_reason": getattr(result, "termination_reason", "unknown"),
            "status": "completed" if getattr(result, "success", False) else "failed",
            "error": getattr(result, "error", ""),
        }, getattr(result, "plan", None)),
        "steps": steps,
        "plan": getattr(result, "plan", None),
    }


def dump_trajectory(path: str, trajectory: Dict[str, Any]) -> None:
    """Content-address images，把内嵌 base64 图片外置成 assets/ 下的去重文件。"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    assets = output.parent / "assets"

    def externalize(obj):
        if isinstance(obj, str) and obj.startswith("data:image/") and ";base64," in obj:
            header, encoded = obj.split(",", 1)
            data = base64.b64decode(encoded, validate=True)
            digest = hashlib.sha256(data).hexdigest()
            suffix = ".jpg" if "jpeg" in header else ".png"
            assets.mkdir(exist_ok=True)
            target = assets / (digest + suffix)
            if not target.exists():
                target.write_bytes(data)
            return f"assets/{target.name}"
        if isinstance(obj, dict):
            return {k: externalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [externalize(v) for v in obj]
        return obj

    packed = externalize(trajectory)
    for step in packed["steps"]:
        if step.get("observation", {}).get("screenshot"):
            step["screenshot"] = step["observation"]["screenshot"]
    output.write_text(json.dumps(packed, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_sft_records(trajectory, require_success=True):
    """SFT 记录导出：所有域统一，prompt 不做重建、不注入 hindsight。

    成功 = 独立评测器通过；模型自述不是标签。loss 只对 completion 算（completion_only）。
    """
    outcome = trajectory.get("outcome", {})
    if trajectory.get("provenance", {}).get("use_for_model_training") is False:
        return
    if require_success and not (outcome.get("evaluation_available") and outcome.get("score", 0) >= 1):
        return
    for step in trajectory.get("steps", []):
        if not step.get("trainable"):
            continue
        calls = step.get("decision_calls") or []
        if calls and calls[-1].get("finish_reason") == "length":
            continue
        yield {"prompt": copy.deepcopy(step["policy_input"]),
               "completion": [{"role": "assistant", "content": step["policy_output"]}],
               "task_id": trajectory["task"]["id"], "step": step["step"],
               "loss_scope": "completion_only", "schema_version": TRAJECTORY_VERSION}


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
    """RL on-policy 记录：严格校验 policy_revision / token_ids / token_logprobs。"""
    outcome = trajectory.get("outcome", {})
    if not outcome.get("evaluation_available"):
        raise ValueError("Independent episode reward is required")
    if not policy_revision:
        raise ValueError("A fixed behavior policy revision is required")
    if trajectory.get("provenance", {}).get("use_for_model_training") is False:
        raise ValueError("Test fixtures are not policy rollouts")
    samples = []
    for step in trajectory.get("steps", []):
        calls = step.get("decision_calls", [])
        if not calls:
            raise ValueError("Missing decision call records")
        for attempt, call in enumerate(calls):
            ids, lp = call.get("token_ids"), call.get("token_logprobs")
            if (call.get("policy_revision") != policy_revision or not ids or not lp or len(ids) != len(lp)):
                raise ValueError("Need matching policy_revision, completion token_ids and token_logprobs for every call")
            samples.append({"prompt": call["messages"], "completion": call["response"],
                "token_ids": ids, "behavior_logprobs": lp, "step": step["step"], "attempt": attempt,
                "valid_action": bool(call.get("valid_action")), "episode_reward": outcome["score"],
                "step_reward": step.get("reward") if attempt == len(calls) - 1 else None,
                "terminated": step.get("terminated", False), "truncated": step.get("truncated", False),
                "next_observation": step.get("next_observation")})
    yield from samples
