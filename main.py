"""入口：跑单个任务。"""
from __future__ import annotations

import argparse

from .config import Config
from .model import build_decision_model, build_grounding_model
from .env import build_environment
from .agent import Agent


def main() -> None:
    parser = argparse.ArgumentParser(description="osworld_agent 单任务运行")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--task-id", required=True, help="OSWorld 任务 ID")
    parser.add_argument("--task-instruction", default="", help="任务说明（缺省用 task-id）")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    model = build_decision_model(cfg.model)
    grounding = build_grounding_model(cfg.grounding_model)
    env = build_environment(cfg)

    agent = Agent(
        decision_model=model,
        grounding=grounding,
        env=env,
        max_steps=cfg.agent.max_steps,
        max_retries=cfg.agent.max_retries,
        enable_reflection=cfg.agent.enable_reflection,
        early_stop_repeat=cfg.agent.early_stop_repeat,
    )
    task = args.task_instruction or args.task_id
    try:
        result = agent.run(task)
        print(f"任务 {args.task_id}: score={result.score} steps={result.steps} "
              f"answer={result.final_answer!r}")
    finally:
        env.close()


if __name__ == "__main__":
    main()
