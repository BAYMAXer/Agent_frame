"""入口：在本机 VMware 上跑一个真实 OSWorld 任务。

用法示例：
    python -m osworld_agent.adapters.run_vm \
        --task-id 0d8b7de3 \
        --examples-dir D:/400-project/SCALE-CUA/osworld_eval/evaluation_examples/examples \
        --desktop-env-path D:/400-project/AgentS \
        --vm-path "你的 .vmx 路径" \
        --config config.yaml

说明：
- 不改框架任何文件，只在这里组装真实环境 + grounding + agent。
- 依赖 OSWorld 的 desktop_env（--desktop-env-path 指向包含 desktop_env/ 的目录）。
"""
from __future__ import annotations

import argparse
from typing import Any, Dict

from ..config import Config
from ..model import build_decision_model, GROUNDING_REGISTRY
from ..agent import Agent
from .vmware_env import VMwareEnvironment
from .uitars_grounding import RealUItarsGrounding
from .uivenus2_grounding import UIVenus2Grounding
from .task_loader import load_task_config


def main() -> None:
    parser = argparse.ArgumentParser(description="在本机 VMware 上跑一个 OSWorld 任务")
    parser.add_argument("--task-id", required=True, help="任务 ID（对应 evaluation_examples 里的 json）")
    parser.add_argument("--examples-dir", required=True, help="evaluation_examples/examples 目录")
    parser.add_argument("--config", default="config.yaml", help="模型/grounding 配置文件")
    parser.add_argument("--desktop-env-path", default=None, help="包含 desktop_env/ 的目录（如 D:/400-project/AgentS）")
    parser.add_argument("--vm-path", default=None, help="VMware 的 .vmx 路径（缺省用 desktop_env 的注册表默认）")
    parser.add_argument("--headless", action="store_true", help="headless 模式")
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    args = parser.parse_args()

    cfg = Config.load(args.config)

    # 1) 任务配置
    task_config: Dict[str, Any] = load_task_config(args.task_id, args.examples_dir)
    instruction = str(task_config.get("instruction") or args.task_id)

    # 2) 决策模型
    model = build_decision_model(cfg.model)

    # 3) 真实 grounding
    entry = GROUNDING_REGISTRY.get(cfg.grounding_model.name)
    grounding_model_name = entry.name if entry else cfg.grounding_model.name
    grounding = UIVenus2Grounding(
        url=cfg.grounding_model.url,
        api_key=cfg.grounding_model.api_key,
        model=grounding_model_name,
        width=args.screen_width,
        height=args.screen_height,
    )

    # 4) VMware 环境
    env = VMwareEnvironment(
        task_config=task_config,
        vm_path=args.vm_path,
        headless=args.headless,
        screen_width=args.screen_width,
        screen_height=args.screen_height,
        desktop_env_path=args.desktop_env_path,
    )

    # 5) 组装 agent 跑
    agent = Agent(
        decision_model=model,
        grounding=grounding,
        env=env,
        max_steps=cfg.agent.max_steps,
        max_retries=cfg.agent.max_retries,
        enable_reflection=cfg.agent.enable_reflection,
        early_stop_repeat=cfg.agent.early_stop_repeat,
        max_replans=cfg.agent.max_replans,
        wait_config=cfg.agent.wait_config,
        progress_config=cfg.agent.progress_config,
        decision_judge=cfg.agent.decision_judge,
    )
    try:
        result = agent.run(instruction)
        print(f"任务 {args.task_id}: score={result.score} steps={result.steps} "
              f"success={result.success} answer={result.final_answer!r}")
    finally:
        env.close()


if __name__ == "__main__":
    main()
