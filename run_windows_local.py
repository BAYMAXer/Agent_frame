"""Windows 本地手动任务运行器：自由文本任务 → Agent + WindowsEnvironment。

跑一个本地 GUI 任务（重点：浏览器任务），逐步回放 + 落盘轨迹/截图。

用法：
    python run_windows_local.py --task "用谷歌学术(scholar.google.com)搜索 'attention is all you need'，告诉我第一个搜索结果"

说明：
- 会自动启动一个带 remote-debugging 的无痕 Chrome（端口 9222），仅复用已确认的无痕实例；
- 模型默认走 189 的决策(9028) + grounding(8001)，可用 --model-url / --ground-url 覆盖；
- 屏幕分辨率自动检测（grounding 按它换算坐标），也可用 --width/--height 覆盖；
- 运行时会【真】控制你的鼠标键盘，跑的时候别动鼠标。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_here)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from osworld_agent.config import ModelConfig          # noqa: E402
from osworld_agent.model import ChatModel             # noqa: E402
from osworld_agent.adapters.uivenus2_grounding import UIVenus2Grounding  # noqa: E402
from osworld_agent.adapters.windows_env import WindowsEnvironment        # noqa: E402
from osworld_agent.agent import Agent                 # noqa: E402
from osworld_agent.viz.logging import (LoggingChatModel, LoggingGrounding,  # noqa: E402
                                       set_results_dir, log_status, reset_collected)

CHROME_DEFAULT = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def ensure_chrome(port: int, chrome_path: str) -> bool:
    """确保有带 remote-debugging 的无痕 Chrome；仅新建实例时返回 True。"""
    from osworld_agent.utils import cdp_pages_are_incognito, list_cdp_pages
    if list_cdp_pages("localhost", port):
        if not cdp_pages_are_incognito("localhost", port):
            raise RuntimeError(
                f"调试端口 {port} 已被普通或无法确认无痕模式的浏览器占用。"
                "请关闭该调试浏览器，或使用 --browser-port 指定空闲端口。")
        print(f"[chrome] 检测到 {port} 端口已有无痕 Chrome，直接复用")
        return False
    if not os.path.isfile(chrome_path):
        print(f"[chrome] 找不到 Chrome：{chrome_path}（可用 --chrome 指定路径）")
        return False
    print(f"[chrome] 启动带 remote-debugging 的无痕 Chrome（端口 {port}）...")
    user_data_dir = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".",
                                 f"osworld_agent_chrome_incognito_{port}")
    proc = subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--incognito", "--new-window",
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble", "--disable-infobars",  # 消除"恢复页面"弹窗干扰
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(0.5)
        if list_cdp_pages("localhost", port) and cdp_pages_are_incognito("localhost", port):
            print("[chrome] 无痕 Chrome 已就绪")
            return True
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)
    raise RuntimeError(f"端口 {port} 的 Chrome 未就绪或无法确认无痕模式，已停止本次启动。")


def close_test_chrome(port: int = 9222) -> None:
    """关闭 ensure_chrome 启动的测试 Chrome（按 user-data-dir 过滤，不碰用户自己的 Chrome）。"""
    profile_pattern = rf"osworld_agent_chrome_incognito_{int(port)}(?:\"|\s|$)"
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -match '{profile_pattern}' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001  关闭失败不影响主流程
        pass


def _live_step(step_no, decision, execution, state):
    """每步结束的实时回调：打印动作 / 路由 / 判定 / 结果。"""
    import json as _json
    a = decision.action
    action_str = ""
    if a is not None:
        action_str = _json.dumps({"action": a.action, "args": a.args}, ensure_ascii=False)
    route = state.route or {}
    channel = route.get("channel", "") if isinstance(route, dict) else ""
    parts = [f"[step {step_no}] {action_str}"]
    if channel:
        parts.append(f"路由:{channel}")
    if decision.action_status:
        parts.append(f"判定:{decision.action_status}")
    print("  " + "  |  ".join(parts), flush=True)
    if execution.outcome:
        print(f"         结果: {str(execution.outcome)[:200]}", flush=True)


def main() -> int:
    # 行缓冲：即使被后台 subprocess 捕获，也逐行 flush，避免"看似卡住"（上次就是这个坑）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="Windows 本地手动任务运行器")
    parser.add_argument("--task", required=True, help="自由文本任务描述")
    parser.add_argument("--model", default="Qwen3.5-9B")
    parser.add_argument("--model-url", default="http://7.246.80.189:9028/v1")
    parser.add_argument("--ground-model", default="UI-Venus-2-9B")
    parser.add_argument("--ground-url", default="http://7.246.80.189:8001/v1")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--width", type=int, default=None, help="屏幕宽（缺省自动检测）")
    parser.add_argument("--height", type=int, default=None, help="屏幕高（缺省自动检测）")
    parser.add_argument("--browser-port", type=int, default=9222)
    parser.add_argument("--chrome", default=CHROME_DEFAULT)
    parser.add_argument("--download-dir", default=None, help="下载目录（缺省为 osworld_agent/downloads）")
    parser.add_argument("--result-dir", default=None, help="轨迹/截图保存目录（缺省不落盘）")
    args = parser.parse_args()

    # 0) 屏幕分辨率：grounding 按它换算坐标，必须和真实屏幕一致
    import pyautogui
    sw, sh = pyautogui.size()
    width = args.width or sw
    height = args.height or sh
    print(f"[screen] 分辨率 {width}x{height}（检测到 {sw}x{sh}）")

    # 1) 确保 Chrome 带 remote debugging
    started_chrome = ensure_chrome(args.browser_port, args.chrome)
    time.sleep(2.0)  # 等 Chrome 窗口稳定

    # 2) 模型（用 Logging 包装，每次决策/定位调用实时推送到 viz server，前端边跑边显示）
    model = LoggingChatModel(ChatModel(ModelConfig(name=args.model, url=args.model_url, api_key="EMPTY")))
    grounding = LoggingGrounding(UIVenus2Grounding(url=args.ground_url, api_key="EMPTY",
                                                   model=args.ground_model, width=width, height=height))

    # 3) 环境
    env = WindowsEnvironment(browser_port=args.browser_port, width=width, height=height,
                             downloads_dir=args.download_dir)

    # 4) 组装 + 跑
    agent = Agent(decision_model=model, grounding=grounding, env=env,
                  max_steps=args.max_steps, enable_reflection=False,
                  step_callback=_live_step)

    if args.result_dir:
        set_results_dir(args.result_dir)
    reset_collected()
    log_status({"event": "task_start", "instruction": args.task, "max_steps": args.max_steps})

    print(f"\n===== 任务开始 =====\n{args.task}\n")
    try:
        result = agent.run(args.task)
    finally:
        env.close()
        # 任务结束（无论成功/失败/异常）都关掉本次新建的测试 Chrome，避免窗口残留
        if started_chrome:
            close_test_chrome(args.browser_port)

    # 通知前端任务结束（带结果），否则前端收不到 task_end 会把"已完成"误判成"异常终止"
    log_status({
        "event": "task_end",
        "result": result.score,
        "steps": result.steps,
        "success": bool(result.success),
        "answer": result.final_answer or "",
    })

    # 5) 逐步回放
    print(f"\n===== 结束 =====\nscore={result.score} steps={result.steps} answer={result.final_answer!r}\n")
    print("----- 逐步记录 -----")
    for s in result.trajectory:
        step = s.get("step")
        action = s.get("action") or "(无动作)"
        outcome = s.get("outcome") or ""
        routing = s.get("routing") or {}
        channel = routing.get("channel") if isinstance(routing, dict) else ""
        assess = s.get("assessment") or {}
        line = f"step{step}: {action}"
        if channel:
            line += f"  [路由:{channel}]"
        print(line)
        if outcome:
            print(f"      结果: {str(outcome)[:160]}")
        if assess.get("action_status"):
            print(f"      判定: {assess.get('action_status')}")

    # 6) 落盘轨迹 + 截图
    if args.result_dir:
        os.makedirs(args.result_dir, exist_ok=True)
        for step_no, shot in result.screenshots.items():
            try:
                if hasattr(shot, "save"):
                    shot.save(os.path.join(args.result_dir, f"step_{step_no}.png"))
            except Exception:  # noqa: BLE001
                pass
        # 原始每步记录（含解析失败的 raw 输出），供归因。剔除 base64 图避免膨胀（图已在 assets/ 去重）。
        import json as _json

        def _strip_images(obj):
            if isinstance(obj, dict):
                return {k: _strip_images(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_strip_images(v) for v in obj]
            if isinstance(obj, str) and obj.startswith("data:image/"):
                return "[image]"
            return obj

        raw_lines = [_json.dumps(_strip_images(s), ensure_ascii=False)
                     for s in result.trajectory if isinstance(s, dict)]
        with open(os.path.join(args.result_dir, "traj_raw.jsonl"), "w", encoding="utf-8") as _f:
            _f.write("\n".join(raw_lines))
        from osworld_agent.trajectory import build_trajectory, dump_trajectory
        traj = build_trajectory(task_id="local", domain="windows",
                                instruction=args.task, result=result)
        dump_trajectory(os.path.join(args.result_dir, "trajectory.json"), traj)
        print(f"\n轨迹与截图已保存到: {args.result_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
