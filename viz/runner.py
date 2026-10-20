"""可视化运行器：跑一个真实 OSWorld 任务，边跑边把模型调用推给 viz 服务器，结束后落盘轨迹。

历史轨迹目录命名：{example_id}__{run_ts}，其中 run_ts = YYYYMMDD_NNN（日期+当日序号），
供前端历史列表区分多次运行、显示编号。traj.jsonl 写成前端 renderRunDetail 期望的格式。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

_here = os.path.dirname(os.path.abspath(__file__))
_pkg = os.path.dirname(_here)
if _pkg not in sys.path:
    sys.path.insert(0, _pkg)

from osworld_agent.config import ModelConfig  # noqa: E402
from osworld_agent.model import ChatModel  # noqa: E402
from osworld_agent.agent import Agent  # noqa: E402
from osworld_agent.adapters.vmware_env import VMwareEnvironment  # noqa: E402
from osworld_agent.adapters.uitars_grounding import RealUItarsGrounding  # noqa: E402
from osworld_agent.adapters.uivenus2_grounding import UIVenus2Grounding  # noqa: E402
from osworld_agent.adapters.task_loader import load_task_config  # noqa: E402
from osworld_agent.viz.logging import (  # noqa: E402
    LoggingChatModel, LoggingGrounding, log_status, set_results_dir,
    get_collected, reset_collected, flush_logs,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    from osworld_agent.model.decision_judge import add_judge_arguments, judge_config_from_args
    add_judge_arguments(parser)
    from osworld_agent.knowledge_selection import add_knowledge_arguments
    add_knowledge_arguments(parser)
    parser.add_argument('--judge-selection-policy', choices=['threshold', 'maximum_probability'],
                        default='maximum_probability', help='Jev 标签选择：最高概率或阈值与分差')
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--examples-dir", required=True)
    parser.add_argument("--domain", default="all")
    parser.add_argument("--desktop-env-path", default=None)
    parser.add_argument("--cache-dir", default=None, help="OSWorld 缓存目录（复用原框架的 cache，避免重复下载）")
    parser.add_argument("--vm-path", default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--model", default="Qwen3.6-27B")
    parser.add_argument("--model-url", default="http://10.44.101.62:8094/v1")
    parser.add_argument("--model-api-key", default="EMPTY")
    parser.add_argument("--ground-model", default="UI-Venus-2-9B")
    parser.add_argument("--ground-url", default="http://7.246.80.189:8001/v1")
    parser.add_argument("--ground-api-key", default="EMPTY")
    parser.add_argument("--ground-type", default="ui-venus2", choices=["ui-tars", "ui-venus2", "qwen-plus"],
                        help="grounding 模型类型：ui-tars / ui-venus2 / qwen-plus")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--operation-timeout", type=float, default=300)
    parser.add_argument("--task-timeout", type=float, default=1800)
    parser.add_argument("--download-max-mib", type=int, default=50,
                        help="单文件下载资源上限 MiB，0 表示不限")
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    parser.add_argument('--mode-prompts-file', help='Studio 保存的各模式决策提示词 JSON')
    args = parser.parse_args()
    from osworld_agent.prompt_settings import load_mode_prompts
    mode_prompts = load_mode_prompts(args.mode_prompts_file)

    results_root = os.getenv("AGENTS_RESULTS_DIR",
                             os.path.join(_pkg, "viz", "runs"))
    safe_model = args.model.replace("/", "_")
    model_dir = os.path.join(results_root, "pyautogui", "screenshot", safe_model)
    base_dir = os.path.join(model_dir, args.domain)

    # 目录命名 {example_id}__{run_ts}，run_ts = YYYYMMDD_NNN（当日全局序号，跨域名递增）
    example_id = args.task_id
    date_str = time.strftime("%Y%m%d")
    existing = []
    if os.path.isdir(model_dir):
        for domain in os.listdir(model_dir):
            domain_dir = os.path.join(model_dir, domain)
            if os.path.isdir(domain_dir):
                existing += [d for d in os.listdir(domain_dir) if f"__{date_str}_" in d]
    run_ts = f"{date_str}_{len(existing):03d}"
    results_dir = os.path.join(base_dir, f"{example_id}__{run_ts}")
    os.makedirs(results_dir, exist_ok=True)
    set_results_dir(results_dir)
    reset_collected()

    # 1) 任务配置
    task_config = load_task_config(args.task_id, args.examples_dir)
    instruction = str(task_config.get("instruction") or args.task_id)

    # 2) 决策模型（包装，记录调用）
    model = LoggingChatModel(ChatModel(ModelConfig(
        name=args.model, url=args.model_url, api_key=args.model_api_key)))

    # 3) grounding（包装，记录定位）
    if args.ground_type == "qwen-plus":
        from osworld_agent.adapters.qwen_grounding import QwenPlusGrounding
        _ground = QwenPlusGrounding(
            url=args.ground_url, api_key=args.ground_api_key, model=args.ground_model,
            width=args.screen_width, height=args.screen_height)
    elif args.ground_type == "ui-venus2":
        _ground = UIVenus2Grounding(
            url=args.ground_url, api_key=args.ground_api_key, model=args.ground_model,
            width=args.screen_width, height=args.screen_height)
    else:
        _ground = RealUItarsGrounding(
            url=args.ground_url, api_key=args.ground_api_key, model=args.ground_model,
            width=args.screen_width, height=args.screen_height)
    grounding = LoggingGrounding(_ground)

    # 4) 环境（传入决策模型用于 type 落点校验）
    env = VMwareEnvironment(
        task_config=task_config, vm_path=args.vm_path, headless=args.headless,
        screen_width=args.screen_width, screen_height=args.screen_height,
        desktop_env_path=args.desktop_env_path, cache_dir=args.cache_dir,
        decision_model=model)

    # 5) 组装 + 跑
    agent = Agent(decision_model=model, grounding=grounding, env=env, mode_prompts=mode_prompts,
                      knowledge_method=args.knowledge_method, knowledge_jev_url=args.knowledge_jev_url,
                  max_steps=args.max_steps, enable_reflection=False,
                  wait_config={"operation_timeout_s": args.operation_timeout, "task_timeout_s": args.task_timeout,
                               "download_max_bytes": args.download_max_mib * 1024 * 1024},
                  decision_judge={**judge_config_from_args(args), 'selection_policy': args.judge_selection_policy},
                  completion_spec=task_config.get("completion"),
                  private_completion_spec=task_config.get("private_completion"),
                  event_path=os.path.join(results_dir, "events.jsonl"))

    log_status({
        "event": "task_start", "instruction": instruction, "example_id": example_id,
        "max_steps": args.max_steps, "model": args.model, "ground_model": args.ground_model,
    })

    try:
        result = agent.run(instruction)
    finally:
        env.close()

    # 6) 落盘轨迹
    with open(os.path.join(results_dir, "instruction.txt"), "w", encoding="utf-8") as f:
        f.write(instruction)
    with open(os.path.join(results_dir, "result.txt"), "w", encoding="utf-8") as f:
        f.write(str(result.score))

    # 把 pipeline 轨迹 + logging 采集到的决策/grounding 归并成前端格式
    collected = get_collected()
    decisions = {d["step"]: d for d in collected["decisions"]}
    groundings = defaultdict(list)
    for g in collected["groundings"]:
        groundings[g["step"]].append({"query": g["query"], "coord": g["coord"],
                                      "elapsed": g["elapsed"]})
    with open(os.path.join(results_dir, "traj.jsonl"), "w", encoding="utf-8") as f:
        for step in result.trajectory:
            step_no = step.get("step")
            d = decisions.get(step_no, {})
            line = {
                "step_num": step_no,
                "plan": d.get("response", ""),
                "plan_elapsed": d.get("elapsed"),
                "action": step.get("action", ""),
                "reward": None,
                "done": None,
                "screenshot_file": d.get("screenshot_file", ""),
                "grounding": groundings.get(step_no, []),
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    # 导出标准训练轨迹（供 SFT / RL，reward 留空待回填）
    from osworld_agent.trajectory import build_trajectory, dump_trajectory
    trajectory = build_trajectory(
        task_id=example_id, domain=args.domain, instruction=instruction,
        result=result,
        decisions=collected["decisions"], groundings=collected["groundings"],
    )
    dump_trajectory(os.path.join(results_dir, "trajectory.json"), trajectory)

    log_status({
        "event": "task_end", "example_id": example_id,
        "result": result.score, "steps": result.steps, "success": result.success,
    })
    print(f"done: score={result.score} steps={result.steps} answer={result.final_answer!r}")
    flush_logs(timeout=1.0)


if __name__ == "__main__":
    main()
