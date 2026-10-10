"""Docker 批量评测：在服务器上用 Docker provider 跑 OSWorld test_small。

用法（在 runner 容器内，PYTHONPATH 含 /osworld_agent、/osworld）：
    python3.11 run_docker.py \
        --osworld-root /osworld \
        --vm-path /data/osworld-agent-s/vm/uploaded/Ubuntu.qcow2 \
        --test-meta /osworld/evaluation_examples/test_small.json \
        --result-dir /data/osworld-agent-s/results/<RUN_ID> \
        --model Qwen3.5-9B --model-url http://7.246.80.237:9028/v1 \
        --ground-model UI-TARS-1.5-7B --ground-url http://7.246.80.237:49999/v1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
_pkg = os.path.dirname(_here)
if _pkg not in sys.path:
    sys.path.insert(0, _pkg)

from osworld_agent.config import ModelConfig  # noqa: E402
from osworld_agent.model import ChatModel  # noqa: E402
from osworld_agent.agent import Agent  # noqa: E402
from osworld_agent.adapters.docker_env import DockerEnvironment  # noqa: E402
from osworld_agent.adapters.uitars_grounding import RealUItarsGrounding  # noqa: E402
from osworld_agent.adapters.uivenus2_grounding import UIVenus2Grounding  # noqa: E402


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {str(d): [str(t) for t in ids] for d, ids in data.items()}


def load_task_config(examples_dir: Path, domain: str, task_id: str) -> dict:
    p = examples_dir / domain / f"{task_id}.json"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--osworld-root", required=True)
    parser.add_argument("--vm-path", required=True)
    parser.add_argument("--test-meta", required=True)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--model", default="Qwen3.5-9B")
    parser.add_argument("--model-url", default="http://7.246.80.237:9028/v1")
    parser.add_argument("--ground-model", default="UI-TARS-1.5-7B")
    parser.add_argument("--ground-url", default="http://7.246.80.237:49999/v1")
    parser.add_argument("--ground-type", default="ui-tars", choices=["ui-tars", "ui-venus2"],
                        help="grounding 模型类型：ui-tars / ui-venus2")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    args = parser.parse_args()

    osworld_root = Path(args.osworld_root)
    examples_dir = osworld_root / "evaluation_examples" / "examples"
    manifest = load_manifest(args.test_meta)
    result_root = Path(args.result_dir) / "pyautogui" / "screenshot" / args.model

    model_cfg = ModelConfig(name=args.model, url=args.model_url, api_key="EMPTY")

    total = sum(len(v) for v in manifest.values())
    done = 0
    for domain, task_ids in manifest.items():
        for task_id in task_ids:
            task_dir = result_root / domain / task_id
            if (task_dir / "result.txt").exists():
                done += 1
                print(f"[跳过] {domain}/{task_id} 已有 result.txt", flush=True)
                continue

            task_config = load_task_config(examples_dir, domain, task_id)
            instruction = str(task_config.get("instruction") or task_id)

            model = ChatModel(model_cfg)
            if args.ground_type == "ui-venus2":
                grounding = UIVenus2Grounding(
                    url=args.ground_url, api_key="EMPTY", model=args.ground_model,
                    width=args.screen_width, height=args.screen_height)
            else:
                grounding = RealUItarsGrounding(
                    url=args.ground_url, api_key="EMPTY", model=args.ground_model,
                    width=args.screen_width, height=args.screen_height)
            env = DockerEnvironment(
                task_config=task_config, vm_path=args.vm_path, headless=True,
                screen_width=args.screen_width, screen_height=args.screen_height)

            agent = Agent(decision_model=model, grounding=grounding, env=env,
                          max_steps=args.max_steps, enable_reflection=False)

            print(f"[{done + 1}/{total}] 开始 {domain}/{task_id} ...", flush=True)
            result = None
            try:
                result = agent.run(instruction)
                score = result.score
            except Exception as exc:  # noqa: BLE001
                score = 0.0
                print(f"[{domain}/{task_id}] 异常: {exc}", flush=True)
            finally:
                try:
                    env.close()
                except Exception:  # noqa: BLE001
                    pass

            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "result.txt").write_text(str(score), encoding="utf-8")
            (task_dir / "instruction.txt").write_text(instruction, encoding="utf-8")
            # 落截图（决策前观察 o_t，供训练轨迹引用）
            if result is not None and getattr(result, "screenshots", None):
                for step_no, shot in result.screenshots.items():
                    try:
                        if hasattr(shot, "save"):
                            shot.save(str(task_dir / f"step_{step_no}.png"))
                        elif isinstance(shot, (bytes, bytearray)):
                            (task_dir / f"step_{step_no}.png").write_bytes(bytes(shot))
                    except Exception:  # noqa: BLE001  截图落盘失败不影响主流程
                        pass
            # 落轨迹（每步 action/outcome/assessment/screen_analysis，供失败归因）
            if result is not None and getattr(result, "trajectory", None):
                traj_lines = []
                for s in result.trajectory:
                    if isinstance(s, dict):
                        traj_lines.append(json.dumps(s, ensure_ascii=False))
                if traj_lines:
                    (task_dir / "traj.jsonl").write_text("\n".join(traj_lines), encoding="utf-8")
                # 导出标准训练轨迹（供 SFT / RL，reward 留空待回填）
                from osworld_agent.trajectory import build_trajectory, dump_trajectory
                trajectory = build_trajectory(
                    task_id=task_id, domain=domain, instruction=instruction, result=result)
                dump_trajectory(str(task_dir / "trajectory.json"), trajectory)
            done += 1
            print(f"[{done}/{total}] {domain}/{task_id} -> score={score}", flush=True)

    print("=== 全部任务完成 ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
