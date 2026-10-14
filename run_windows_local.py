"""Windows 本地手动任务运行器：自由文本任务 → Agent + WindowsEnvironment。

跑一个本地 GUI 任务（重点：浏览器任务），逐步回放 + 落盘轨迹/截图。

用法：
    python launch.py --runner local --task "用谷歌学术搜索 'attention is all you need'，告诉我第一个搜索结果"

说明：
- 默认 browser_first：按计划子任务进入独立浏览器；--execution-mode desktop 保留桌面兼容流程；
- 浏览器输入截图最长边默认 1280 像素，精简 AX 默认最多 12000 字符；
- 进入网页子任务时，启动或核验带 remote-debugging 的无痕 Chrome；
- browser_first 绑定本次运行的私有网页，不依赖桌面焦点；
- 目标应用在任务期间置顶，切换应用或任务结束时恢复原置顶状态；
- 任务结束后释放本次运行的私有会话，浏览器进程由用户关闭；
- 模型连接可用 --model-url / --ground-url 覆盖；
- 屏幕分辨率自动检测（grounding 按它换算坐标），也可用 --width/--height 覆盖；
- 网页操作使用 CDP 虚拟输入；显式桌面子任务使用本机鼠标键盘。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

_here = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_here)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

if __package__:
    from .launch import bootstrap_package
else:
    from launch import bootstrap_package
bootstrap_package()

from osworld_agent.config import ContextConfig, DEFAULT_AX_MAX_CHARS, ModelConfig  # noqa: E402
from osworld_agent.model import ChatModel             # noqa: E402
from osworld_agent.adapters.uivenus2_grounding import UIVenus2Grounding  # noqa: E402
from osworld_agent.adapters.uitars_grounding import RealUItarsGrounding  # noqa: E402
from osworld_agent.adapters.windows_env import WindowsEnvironment        # noqa: E402
from osworld_agent.adapters.windows_display import (detect_primary_work_screen,
                                                     initialize_dpi_awareness)  # noqa: E402
from osworld_agent.agent import Agent                 # noqa: E402
from osworld_agent.run_recording import LocalRunRecorder  # noqa: E402
from osworld_agent.viz.logging import (LoggingChatModel, LoggingGrounding,  # noqa: E402
                                       set_results_dir, log_status, reset_collected)

CHROME_DEFAULT = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
_PROFILE_PREFIX = "osworld_agent_chrome_incognito_"


def _validated_profile(port: int, user_data_dir: str):
    """只接受临时根目录下、本端口的专用 profile，拒绝链接或越界路径。"""
    if not user_data_dir:
        return None
    try:
        original = os.path.abspath(user_data_dir)
        if os.path.islink(original) or getattr(os.path, "isjunction", lambda _: False)(original):
            return None
        profile = os.path.realpath(original)
        root = os.path.realpath(tempfile.gettempdir())
        prefix = f"{_PROFILE_PREFIX}{int(port)}_"
        name = os.path.basename(profile)
        if (os.path.normcase(os.path.dirname(profile)) != os.path.normcase(root)
                or not name.startswith(prefix) or len(name) == len(prefix)):
            return None
        return profile
    except (OSError, TypeError, ValueError):
        return None


def _profile_command_pattern(profile: str) -> str:
    """匹配完整 user-data-dir 参数，兼容含空格的 Windows 命令行。"""
    path = re.escape(profile)
    alternatives = [rf'"--user-data-dir={path}"', rf'--user-data-dir="{path}"']
    if not re.search(r"\s", profile):
        alternatives.append(rf'--user-data-dir={path}')
    return r'(?:^|\s)(?:' + "|".join(alternatives) + r')(?=\s|$)'


def _chrome_processes():
    """读取 Chrome 进程及其监听端口；无法确认时返回 None，禁止扩大清理范围。"""
    script = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
$taskRows = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | ForEach-Object {
    $taskPid = [int]$_.ProcessId
    $taskPorts = @($taskListeners | Where-Object { $_.OwningProcess -eq $taskPid } |
                  Select-Object -ExpandProperty LocalPort -Unique)
    [pscustomobject]@{
        pid = $taskPid
        command_line = $_.CommandLine
        ports = @($taskPorts | Where-Object {
            $taskPort = $_
            -not ($taskListeners | Where-Object {
                $_.LocalPort -eq $taskPort -and $_.OwningProcess -ne $taskPid
            })
        })
    }
})
ConvertTo-Json -InputObject $taskRows -Compress -Depth 3
"""
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20)
        if result.returncode:
            return None
        rows = json.loads(result.stdout)
        return rows if isinstance(rows, list) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _profile_processes(profile: str, processes):
    pattern = _profile_command_pattern(profile)
    return [proc for proc in (processes or [])
            if re.search(pattern, proc.get("command_line") or "", flags=re.IGNORECASE)]


def _stop_profile_processes(profile: str) -> bool:
    processes = _chrome_processes()
    if processes is None:
        return False
    if not _profile_processes(profile, processes):
        return True
    # 路径通过环境变量传给 PowerShell，避免把路径内容拼成 shell 代码。
    task_env = os.environ.copy()
    task_env["OSWORLD_AGENT_PROFILE_PATTERN"] = _profile_command_pattern(profile)
    script = """
$ErrorActionPreference = 'Stop'
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -match $env:OSWORLD_AGENT_PROFILE_PATTERN } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }
"""
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                env=task_env, capture_output=True, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _owned_endpoint_pids(port: int, profile: str):
    """仅在该端口的监听者全部属于指定 profile 时返回它们的 PID。"""
    processes = _chrome_processes()
    owners = {proc["pid"] for proc in _profile_processes(profile, processes)
              if port in (proc.get("ports") or [])}
    listeners = [proc for proc in (processes or []) if port in (proc.get("ports") or [])]
    if not owners or any(proc["pid"] not in owners for proc in listeners):
        return set()
    return owners


def _close_owned_cdp(port: int, profile: str) -> bool:
    owners = _owned_endpoint_pids(port, profile)
    if not owners:
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}", timeout=4000)
            try:
                # 连接期间端口可能已被重新占用；发送关闭命令前再次核对 PID 和 profile。
                current = _owned_endpoint_pids(port, profile)
                if not current or not current.issubset(owners):
                    return False
                browser.new_browser_cdp_session().send("Browser.close")
                return True
            finally:
                try:
                    browser.close()  # 仅断开临时连接；真正退出由 Browser.close 完成。
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        return False


def _remove_profile(port: int, profile: str) -> bool:
    verified = _validated_profile(port, profile)
    if verified is None:
        return False
    processes = _chrome_processes()
    if (processes is None or any(not proc.get("command_line") for proc in processes)
            or _profile_processes(verified, processes)):
        return False
    try:
        if os.path.exists(verified):
            # 递归删除前再次确认绝对路径和临时根目录范围。
            if _validated_profile(port, verified) != verified:
                return False
            shutil.rmtree(verified)
        return True
    except OSError:
        return False


def ensure_chrome(port: int, chrome_path: str):
    """返回 (是否新建, 本次专用 profile)。新建 profile 用于核验并清理空白启动页。

    复用端点始终返回 None profile，不把已有无痕页面认作当前任务拥有的页面。
    """
    from osworld_agent.utils import cdp_pages_are_incognito, list_cdp_pages
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("浏览器调试端口必须在 1 到 65535 之间")
    if list_cdp_pages("localhost", port):
        if not cdp_pages_are_incognito("localhost", port):
            raise RuntimeError(
                f"调试端口 {port} 已被普通或无法确认无痕模式的浏览器占用。"
                "请关闭该调试浏览器，或使用 --browser-port 指定空闲端口。")
        print(f"[chrome] 检测到 {port} 端口已有无痕 Chrome，直接复用")
        return False, None
    if not os.path.isfile(chrome_path):
        raise FileNotFoundError(f"找不到 Chrome：{chrome_path}（可用 --chrome 指定路径）")
    print(f"[chrome] 启动带 remote-debugging 的无痕 Chrome（端口 {port}）...")
    user_data_dir = tempfile.mkdtemp(prefix=f"{_PROFILE_PREFIX}{port}_",
                                    dir=os.path.realpath(tempfile.gettempdir()))
    proc = None
    try:
        proc = subprocess.Popen([
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--incognito", "--new-window",
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", "--disable-infobars",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.5)
            if (list_cdp_pages("localhost", port) and cdp_pages_are_incognito("localhost", port)
                    and _owned_endpoint_pids(port, user_data_dir)):
                print("[chrome] 无痕 Chrome 已就绪")
                return True, user_data_dir
        raise RuntimeError(f"端口 {port} 的 Chrome 未就绪或无法确认无痕模式，已停止本次启动。")
    except BaseException:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        close_test_chrome(port, user_data_dir)
        raise


def close_test_chrome(port: int, user_data_dir: str = None) -> None:
    """仅关闭本次 profile 的实例，再删除经过范围验证的临时目录。"""
    profile = _validated_profile(port, user_data_dir)
    if profile is None:  # 复用实例没有本次 profile，不承担它的关闭或清理。
        return
    if _close_owned_cdp(port, profile):
        time.sleep(0.5)
    processes = _chrome_processes()
    if processes is None or _profile_processes(profile, processes):
        _stop_profile_processes(profile)
        for _ in range(10):
            processes = _chrome_processes()
            if processes is not None and not _profile_processes(profile, processes):
                break
            time.sleep(0.2)
    if not _remove_profile(port, profile):
        print("[chrome] 未能确认专用实例已退出或 profile 无法删除，保留该临时目录")


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
    parser.add_argument("--model", default=os.getenv("OSWORLD_LOCAL_MODEL", "Qwen3.5-9B"))
    parser.add_argument("--model-url", default=os.getenv("OSWORLD_LOCAL_MODEL_URL", "http://7.246.80.237:9028/v1"))
    parser.add_argument("--ground-model", default=os.getenv("OSWORLD_LOCAL_GROUND_MODEL", "UI-Venus-2-9B"))
    parser.add_argument("--ground-url", default=os.getenv("OSWORLD_LOCAL_GROUND_URL", "http://7.246.80.189:8001/v1"))
    parser.add_argument("--ground-type", choices=("ui-venus2", "ui-tars"),
                        default=os.getenv("OSWORLD_LOCAL_GROUND_TYPE", "ui-venus2"))
    parser.add_argument("--model-timeout", type=float, default=float(os.getenv("OSWORLD_LOCAL_MODEL_TIMEOUT", "60")))
    parser.add_argument("--ground-timeout", type=float, default=float(os.getenv("OSWORLD_LOCAL_GROUND_TIMEOUT", "60")))
    parser.add_argument("--step-timeout", type=float, default=float(os.getenv("OSWORLD_LOCAL_STEP_TIMEOUT", "120")))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--execution-mode", choices=("browser_first", "desktop"),
                        default=os.getenv("OSWORLD_LOCAL_EXECUTION_MODE", "browser_first"),
                        help="browser_first 优先按计划进入独立浏览器；desktop 保留通用桌面流程")
    parser.add_argument("--ax-max-chars", type=int, default=DEFAULT_AX_MAX_CHARS, help="浏览器精简 AX 的字符上限")
    parser.add_argument("--screenshot-max-edge", type=int, default=1280,
                        help="浏览器模型输入截图最长边像素数")
    parser.add_argument("--width", type=int, default=None, help="屏幕宽（缺省自动检测）")
    parser.add_argument("--height", type=int, default=None, help="屏幕高（缺省自动检测）")
    parser.add_argument("--browser-port", type=int, default=9222)
    parser.add_argument("--chrome", default=CHROME_DEFAULT)
    parser.add_argument("--download-dir", default=None, help="下载目录（任务明确目录优先；否则缺省为版本 downloads）")
    parser.add_argument("--result-dir", default=None, help="轨迹/截图保存目录（缺省自动保存到 local_runs）")
    args = parser.parse_args()
    if any(value <= 0 or not math.isfinite(value)
           for value in (args.model_timeout, args.ground_timeout, args.step_timeout)):
        parser.error("模型、定位及每步超时必须为有限正数")
    if min(args.max_steps, args.ax_max_chars, args.screenshot_max_edge) <= 0:
        parser.error("最大步数、AX 字符预算及截图最长边必须为正整数")
    context_config = ContextConfig(ax_max_chars=args.ax_max_chars,
                                   screenshot_max_edge=args.screenshot_max_edge)

    # 0) 在 GUI 库初始化前统一 DPI；每次任务按 Windows 主屏标志选择默认工作屏幕。
    dpi_awareness = initialize_dpi_awareness()
    import pyautogui
    sw, sh = pyautogui.size()
    work_screen = detect_primary_work_screen(sw, sh)
    width = args.width or work_screen.width
    height = args.height or work_screen.height
    if work_screen.detected:
        print(f"[screen] 检测到 {work_screen.monitor_count} 块屏幕；默认工作屏幕：主屏 "
              f"{work_screen.device or '(primary)'} {work_screen.width}x{work_screen.height}")
    else:
        print(f"[screen] 显示布局检测不可用，沿用默认主屏截图；后备尺寸 "
              f"{work_screen.width}x{work_screen.height}")

    if not args.result_dir:
        base = os.getenv("OSWORLD_LOCAL_RUNS_DIR", os.path.join(_here, "local_runs"))
        args.result_dir = os.path.join(base, "local_" + time.strftime("%Y%m%d_%H%M%S")
                                      + "_" + str(time.time_ns() % 1_000_000_000))
    recorder = LocalRunRecorder(args.result_dir, args.task)
    print(f"[source] {os.path.abspath(__file__)}", flush=True)
    print(f"[model] {args.model} url={args.model_url} timeout={args.model_timeout:g}s attempts=1", flush=True)
    print(f"[grounding] {args.ground_model} type={args.ground_type} url={args.ground_url} "
          f"timeout={args.ground_timeout:g}s attempts=1; step_budget={args.step_timeout:g}s", flush=True)
    print(f"[trajectory] 执行前记录和每步轨迹实时保存到: {args.result_dir}", flush=True)
    print(f"[browser] mode={args.execution_mode} AX<={args.ax_max_chars} chars "
          f"screenshot_edge<={args.screenshot_max_edge}px", flush=True)
    env = None
    result = None
    try:
        set_results_dir(args.result_dir)
        # 2) 模型（Logging 实时推送到 viz server）
        model = LoggingChatModel(ChatModel(ModelConfig(
            name=args.model, url=args.model_url,
            api_key=os.getenv("OSWORLD_LOCAL_MODEL_API_KEY", "EMPTY"),
            timeout=args.model_timeout, request_retries=0)))
        grounding_class = UIVenus2Grounding if args.ground_type == "ui-venus2" else RealUItarsGrounding
        grounding = LoggingGrounding(grounding_class(
            url=args.ground_url, api_key=os.getenv("OSWORLD_LOCAL_GROUND_API_KEY", "EMPTY"),
            model=args.ground_model, width=width, height=height,
            timeout=args.ground_timeout, retries=0))

        # 3) 环境
        env = WindowsEnvironment(browser_port=args.browser_port, width=width, height=height,
                                 downloads_dir=args.download_dir,
                                 browser_launcher=lambda: ensure_chrome(args.browser_port, args.chrome),
                                 execution_mode=args.execution_mode,
                                 screenshot_max_edge=args.screenshot_max_edge,
                                 work_screen=work_screen,
                                 presentation_state_path=os.path.join(args.result_dir, "window_presentation.json"))

        # 4) 组装 + 跑，保留 1010 的规划流水线
        agent = Agent(decision_model=model, grounding=grounding, env=env,
                      max_steps=args.max_steps, enable_reflection=False,
                      step_callback=_live_step, progress_callback=recorder.on_progress,
                      step_timeout=args.step_timeout, context_config=context_config)
        reset_collected()
        log_status({"event": "task_start", "instruction": args.task, "max_steps": args.max_steps,
                    "work_screen": work_screen.to_dict(), "dpi_awareness": dpi_awareness,
                    "execution_mode": args.execution_mode,
                    "configured_download_dir": args.download_dir,
                    "ax_max_chars": args.ax_max_chars, "screenshot_max_edge": args.screenshot_max_edge})
        print(f"\n===== 任务开始 =====\n{args.task}\n")
        result = agent.run(args.task)
    except BaseException as exc:
        from osworld_agent.model.request_deadline import ModelRequestTimeout
        termination_reason = ("interrupted" if isinstance(exc, KeyboardInterrupt) else
                              "model_timeout" if isinstance(exc, ModelRequestTimeout) else "runtime_error")
        recorder.finish(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                        error=f"{type(exc).__name__}: {exc}", termination_reason=termination_reason)
        log_status({"event": "error", "step": (recorder.pending_step or {}).get("step", 0),
                    "error": f"{type(exc).__name__}: {exc}"})
        log_status({"event": "task_end", "success": False, "result": 0,
                    "answer": str(exc), "steps": (recorder.pending_step or {}).get("step", 0),
                    "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                    "error": f"{type(exc).__name__}: {exc}", "termination_reason": termination_reason})
        raise
    finally:
        if env is not None:
            # 仅释放环境会话；浏览器由用户保留和关闭。
            try:
                env.close()
            except BaseException as exc:
                prior_status = recorder.status
                prior_error = recorder.error if prior_status in {"failed", "interrupted"} else ""
                cleanup_error = f"{type(exc).__name__}: {exc}"
                recorder.finish(result, status="interrupted" if prior_status == "interrupted" else "failed",
                                error=(prior_error + "\n清理异常：" + cleanup_error) if prior_error else cleanup_error,
                                termination_reason=(recorder.termination_reason if prior_error else "runtime_error"))
                raise
    recorder.finish(result)

    # 通知前端任务结束（带结果），否则前端收不到 task_end 会把"已完成"误判成"异常终止"
    log_status({
        "event": "task_end",
        "result": result.score,
        "steps": result.steps,
        "success": bool(result.success),
        "answer": result.final_answer or "",
        "status": recorder.status,
        "termination_reason": recorder.termination_reason,
        "error": recorder.error,
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
        # trajectory.json 已由 recorder 在执行前/每步结束时原子更新，保留状态与中断证据。
        print(f"\n轨迹与截图已保存到: {args.result_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
