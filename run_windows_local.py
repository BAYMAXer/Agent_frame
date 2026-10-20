"""Windows 本地手动任务运行器：自由文本任务 → Agent + WindowsEnvironment。

跑一个本地 GUI 任务（重点：浏览器任务），逐步回放 + 落盘轨迹/截图。

用法：
    python run_windows_local.py --task "用谷歌学术(scholar.google.com)搜索 'attention is all you need'，告诉我第一个搜索结果"

说明：
- 自动启动本任务专用的 remote-debugging Chrome，结束时只清理该实例；
- 决策默认使用 DashScope 多模态 API，Windows grounding 默认使用 UI-Venus；可用命令行或 Studio 模型预设覆盖；
- 屏幕分辨率自动检测，也可用 --width/--height 覆盖；
- 浏览器页面优先走 CDP/DOM；桌面接管时才使用本机鼠标键盘。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

_here = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_here)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from osworld_agent.config import ModelConfig          # noqa: E402
from osworld_agent.model import ChatModel             # noqa: E402
from osworld_agent.adapters.uivenus2_grounding import UIVenus2Grounding  # noqa: E402
from osworld_agent.adapters.uitars_grounding import RealUItarsGrounding  # noqa: E402
from osworld_agent.adapters.windows_env import WindowsEnvironment        # noqa: E402
from osworld_agent.agent import Agent                 # noqa: E402
from osworld_agent.viz.logging import (LoggingChatModel, LoggingGrounding,  # noqa: E402
                                       set_results_dir, log_status, reset_collected, flush_logs)

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


def _cdp_ready(port: int) -> bool:
    """Chrome CDP can be ready before any tab exists."""
    try:
        import httpx
        with httpx.Client(trust_env=False, timeout=0.6) as client:
            response = client.get(f'http://127.0.0.1:{int(port)}/json/version')
            response.raise_for_status()
            return bool(response.json().get('webSocketDebuggerUrl'))
    except (OSError, ValueError, httpx.HTTPError):
        return False


def _close_owned_cdp(port: int, profile: str) -> bool:
    owners = _owned_endpoint_pids(port, profile)
    if not owners:
        return False
    try:
        from playwright.sync_api import sync_playwright
        from osworld_agent.utils import cdp_websocket_endpoint
        endpoint = cdp_websocket_endpoint("localhost", port, timeout=4)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(endpoint, timeout=4000)
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


def _lifecycle_status(payload):
    """Telemetry must never prevent browser cleanup, including on failures."""
    try:
        log_status(payload)
    except Exception:
        pass


def ensure_chrome(port: int, chrome_path: str, *, pdf_downloads=False):
    """确保可连接无痕 Chrome，返回 (是否新建, 本次专用 profile 目录)。"""
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
    print(f"[chrome] 启动专用 remote-debugging Chrome（端口 {port}）...")
    user_data_dir = tempfile.mkdtemp(prefix=f"{_PROFILE_PREFIX}{port}_",
                                    dir=os.path.realpath(tempfile.gettempdir()))
    proc = None
    try:
        if pdf_downloads:
            from osworld_agent.browser_profile import prepare_pdf_download_profile
            prepare_pdf_download_profile(user_data_dir)
        proc = subprocess.Popen([
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--incognito", "--no-startup-window", "--lang=en-US",
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", "--disable-infobars",
            "--lang=en-US", "--accept-lang=en-US,en",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(0.5)
            if _cdp_ready(port) and _owned_endpoint_pids(port, user_data_dir):
                print("[chrome] 专用 Chrome 已就绪；任务窗口由隔离上下文创建")
                return True, user_data_dir
        raise RuntimeError(f"端口 {port} 的专用 Chrome 未就绪或无法确认进程归属，已停止本次启动。")
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


def main(platform: str = 'windows') -> int:
    # 行缓冲：即使被后台 subprocess 捕获，也逐行 flush，避免"看似卡住"（上次就是这个坑）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    if platform not in {'windows', 'mac'} and not (platform == 'linux' and '--cli-only' in sys.argv):
        raise ValueError(f'不支持的本机平台: {platform}')
    previous_sigterm = None
    if platform == 'mac' or '--cli-only' in sys.argv:
        # Studio 的“停止”向子进程发送 SIGTERM；转成 Python 退出才能执行下方 finally。
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(SystemExit(143)))
    parser = argparse.ArgumentParser(description=f"{platform} 本地手动任务运行器")
    from osworld_agent.model.decision_judge import add_judge_arguments, judge_config_from_args
    add_judge_arguments(parser)
    from osworld_agent.knowledge_selection import add_knowledge_arguments
    add_knowledge_arguments(parser)
    parser.add_argument('--judge-selection-policy', choices=['threshold', 'maximum_probability'],
                        default='maximum_probability', help='Jev 标签选择：默认直接采用最高概率；threshold 使用阈值与分差')
    parser.add_argument("--cli-only", action="store_true", help="只使用后台文件和终端，不启动浏览器或桌面输入")
    parser.add_argument("--task", required=True, help="自由文本任务描述")
    parser.add_argument("--model", default="Qwen3.6-27B")
    parser.add_argument("--model-url", default="http://10.44.101.62:8094/v1")
    parser.add_argument("--model-api-key", default="EMPTY",
                        help="决策模型密钥或 env:环境变量名（默认从 DASHSCOPE_API_KEY/.env 读取）")
    parser.add_argument("--planner-model", help="可选独立规划模型；默认使用决策模型")
    parser.add_argument("--planner-url", help="独立规划端点；默认沿用 --model-url")
    parser.add_argument("--verifier-model", help="独立核查模型；默认使用决策模型同端点的独立会话")
    parser.add_argument("--verifier-url", help="独立核查端点；默认沿用 --model-url")
    parser.add_argument("--capture-rollout", action="store_true", help="请求服务端实际token与logprobs，缺元数据不允许导出RL")
    parser.add_argument("--policy-revision", default="")
    parser.add_argument("--tokenizer-revision", default="")
    parser.add_argument("--processor-revision", default="")
    parser.add_argument("--chat-template-revision", default="")
    parser.add_argument("--ground-model", default="UI-Venus-2-9B")
    parser.add_argument("--ground-type", choices=["qwen-plus", "ui-venus2", "ui-tars"],
                        default="qwen-plus" if platform == 'mac' else "ui-venus2")
    parser.add_argument("--ground-url", default="http://7.246.80.189:8001/v1")
    parser.add_argument("--ground-api-key", default="EMPTY" if platform == 'mac' else "EMPTY")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="决策/规划采样温度，默认0以减少重复演示中的随机变化")
    parser.add_argument("--operation-timeout", type=float, default=300, help="下载等操作等待上限（秒），自动等待不消耗决策步数")
    parser.add_argument("--download-max-mib", type=int, default=50, help="单个下载的资源上限（MiB），0 表示不限大小；不用于论文身份判断")
    parser.add_argument("--task-timeout", type=float, default=1800, help="任务总时间预算（秒）")
    parser.add_argument("--window-timeout", type=float, default=15, help="桌面目标窗口就绪等待上限（秒），轮询不消耗决策步数")
    parser.add_argument("--width", type=int, default=None, help="屏幕宽（缺省自动检测）")
    parser.add_argument("--height", type=int, default=None, help="屏幕高（缺省自动检测）")
    parser.add_argument("--browser-port", type=int, default=9222)
    parser.add_argument("--browser-session-mode", choices=['isolated', 'persistent'], default='persistent',
                        help="persistent 保留固定任务配置的持久 Cookie 和站点存储；isolated 每次使用独立临时会话")
    parser.add_argument("--browser-profile-dir", default=None,
                        help="固定任务配置的绝对目录；缺省使用 AgentFrame 专用目录，不能接管日常 Chrome 配置")
    parser.add_argument("--browser-start-url", default=None,
                        help="可选初始浏览器页面；仅设置环境起点，不作为任务或论文身份信息")
    if platform == 'mac':
        from osworld_agent.mac_chrome import CHROME_DEFAULT as MAC_CHROME_DEFAULT
        chrome_default = MAC_CHROME_DEFAULT
    else:
        chrome_default = CHROME_DEFAULT
    parser.add_argument("--chrome", default=chrome_default)
    parser.add_argument("--workspace-dir", "--download-dir", dest="download_dir", default=None,
                        help="默认工作目录根路径（兼容 --download-dir）；当天任务共用日期子目录")
    parser.add_argument("--result-dir", default=None, help="轨迹/截图保存目录；默认使用程序 local_runs 下的独立运行目录")
    parser.add_argument("--run-prefix", default=None, help="同日平铺日志的唯一文件名前缀")
    parser.add_argument("--completion-spec", help="可信任务验收条件 JSON 文件；模型不能修改")
    parser.add_argument("--private-completion-spec", help="私有评测条件 JSON；不会发送给模型")
    parser.add_argument("--task-id", help="数据集任务标识；缺省按任务文本生成")
    parser.add_argument('--mode-prompts-file', help='Studio 保存的各模式决策提示词 JSON')
    args = parser.parse_args()
    from osworld_agent.prompt_settings import load_mode_prompts
    mode_prompts = load_mode_prompts(args.mode_prompts_file)
    if args.result_dir is None:
        from osworld_agent.workspace_config import allocate_local_result_dir
        args.result_dir = allocate_local_result_dir()
    if args.run_prefix is None and os.path.basename(os.path.normpath(args.result_dir)) == time.strftime('%Y-%m-%d'):
        import uuid
        args.run_prefix = 'ui_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    if args.result_dir:
        os.makedirs(args.result_dir, exist_ok=True)
    log_name = lambda name: f"{args.run_prefix}_{name}" if args.run_prefix else name
    completion_spec = None
    private_completion_spec = None
    if args.completion_spec:
        with open(args.completion_spec, encoding="utf-8") as f:
            completion_spec = json.load(f)
    if args.private_completion_spec:
        with open(args.private_completion_spec, encoding="utf-8") as f:
            private_completion_spec = json.load(f)
    from osworld_agent.verifiers import CompletionVerifier
    task_completion = CompletionVerifier(args.task, completion_spec, private_completion_spec)
    from osworld_agent.paper_sources import default_paper_start_url
    from osworld_agent.task_specs import task_profile
    args.browser_start_url = default_paper_start_url(args.task,
        is_paper_download=task_profile(args.task, task_completion.public_spec)['kind'] == 'paper_download',
        browser_start_url=args.browser_start_url)

    # 0) 屏幕分辨率：grounding 按它换算坐标，必须和真实屏幕一致
    if args.cli_only:
        sw, sh = 1280, 800  # unused: no screenshots/grounding in CLI-only mode
    else:
        import pyautogui
        sw, sh = pyautogui.size()
    width = args.width or sw
    height = args.height or sh
    print(f"[screen] 分辨率 {width}x{height}（检测到 {sw}x{sh}）")

    startup_started = time.perf_counter()
    try:
        log_status({"event": "task_start", "mode": "local", "instruction": args.task, "max_steps": args.max_steps})
        log_status({"event": "phase_timing", "phase": "browser_startup", "phase_id": "startup", "step": 0,
                    "status": "running", "started_at": time.time()})
    except Exception:
        pass  # Early telemetry cannot prevent the browser lifecycle from starting.
    # 1) 启动专用 CDP Chrome；macOS 延后到隔离任务上下文创建可见窗口。
    if platform == 'mac':
        from osworld_agent.mac_chrome import ensure_chrome as start_chrome, close_test_chrome as stop_chrome
        from osworld_agent.adapters.mac_env import MacEnvironment as LocalEnvironment
    else:
        start_chrome, stop_chrome = ensure_chrome, close_test_chrome
        LocalEnvironment = WindowsEnvironment
    from concurrent.futures import ThreadPoolExecutor
    from osworld_agent.parallel_work import measured_call, prepare_client_dependencies
    # SDK import can take seconds on first use. It needs neither the browser
    # nor credentials; overlap it with Chrome startup, without any generation.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='agent-sdk') as pool:
        dependencies = pool.submit(measured_call, prepare_client_dependencies)
        if args.cli_only:
            from osworld_agent.adapters.cli_env import CLIEnvironment
            LocalEnvironment = CLIEnvironment
            started_chrome, chrome_profile = False, None
        elif args.browser_session_mode == 'persistent':
            from osworld_agent.persistent_chrome import ensure_persistent_chrome, close_persistent_chrome
            started_chrome, chrome_profile = ensure_persistent_chrome(args.browser_port, args.chrome,
                profile_dir=args.browser_profile_dir, platform=platform)
            stop_chrome = close_persistent_chrome
            print(f'[chrome] 固定任务配置（持久存储）：{chrome_profile}')
        elif args.browser_profile_dir:
            parser.error('--browser-profile-dir 仅用于 persistent 模式')
        # A temporary isolated profile can select native PDF download behavior.
        # Persistent profiles keep the operator's own PDF and other preferences.
        elif task_completion.paper_goals():
            started_chrome, chrome_profile = start_chrome(args.browser_port, args.chrome, pdf_downloads=True)
        else:
            started_chrome, chrome_profile = start_chrome(args.browser_port, args.chrome)
        dependency_report, dependency_ms = dependencies.result()
    _lifecycle_status({"event": "client_dependencies_ready", **dependency_report,
                "elapsed_ms": dependency_ms, "overlap": "browser_startup"})
    env = None
    try:
        # Chrome starters already wait for the CDP endpoint. The adapter owns
        # page/window readiness; an unconditional two-second sleep adds no evidence.

        # 2) 模型（Logging 实时推送到 viz server）
        model = LoggingChatModel(ChatModel(ModelConfig(name=args.model, url=args.model_url, api_key=args.model_api_key, temperature=args.temperature,
            capture_rollout=args.capture_rollout, policy_revision=args.policy_revision,
            tokenizer_revision=args.tokenizer_revision, processor_revision=args.processor_revision,
            chat_template_revision=args.chat_template_revision)))
        planner = (LoggingChatModel(ChatModel(ModelConfig(name=args.planner_model,
            url=args.planner_url or args.model_url, api_key=args.model_api_key, temperature=args.temperature))) if args.planner_model else None)
        verifier = (ChatModel(ModelConfig(name=args.verifier_model or args.model,
            url=args.verifier_url or args.model_url, api_key=args.model_api_key, temperature=0, max_tokens=768,
            timeout=45)) if args.verifier_model or args.verifier_url else None)
        if args.ground_type == "qwen-plus":
            from osworld_agent.adapters.qwen_grounding import QwenPlusGrounding as ground_cls
        else:
            ground_cls = RealUItarsGrounding if args.ground_type == "ui-tars" else UIVenus2Grounding
        grounding = LoggingGrounding(ground_cls(url=args.ground_url, api_key=args.ground_api_key,
                                                       model=args.ground_model, width=width, height=height))

        # 3) 环境
        env_kwargs = {'browser_port': args.browser_port, 'width': width, 'height': height,
                      'downloads_dir': args.download_dir, 'window_timeout_s': args.window_timeout,
                      'browser_start_url': args.browser_start_url,
                      'browser_session_mode': args.browser_session_mode}
        env = LocalEnvironment(**env_kwargs)

        log_status({"event": "phase_timing", "phase": "browser_startup", "phase_id": "startup", "step": 0,
                    "status": "completed", "elapsed_ms": round((time.perf_counter()-startup_started)*1000, 2)})
        # 4) 组装 + 跑，保留 1010 的规划流水线
        agent = Agent(decision_model=model, grounding=grounding, env=env, mode_prompts=mode_prompts,
                      knowledge_method=args.knowledge_method, knowledge_jev_url=args.knowledge_jev_url,
                      max_steps=args.max_steps, enable_reflection=False,
                      wait_config={"operation_timeout_s": args.operation_timeout, "task_timeout_s": args.task_timeout, "download_max_bytes": args.download_max_mib * 1024 * 1024},
                      step_callback=_live_step, completion_spec=completion_spec,
                      private_completion_spec=private_completion_spec, planner_model=planner,
                      verifier_model=verifier,
                      decision_judge={**judge_config_from_args(args), 'selection_policy': args.judge_selection_policy},
                      event_path=os.path.join(args.result_dir, log_name("events.jsonl")) if args.result_dir else None)
        if args.result_dir:
            set_results_dir(args.result_dir, args.run_prefix or '')
        reset_collected()
        print(f"\n===== 任务开始 =====\n{args.task}\n")
        result = agent.run(args.task, run_id=os.environ.get('YAERO_RUN_ID') or None)
        # Report the verdict immediately, while keeping the run busy until
        # cleanup finishes. Cleanup is not part of semantic verification.
        _lifecycle_status({"event": "verification_complete", "success": bool(result.success),
                    "answer": result.final_answer or "",
                    "termination_reason": getattr(result, 'termination_reason', '')})
    finally:
        cleanup_started = time.perf_counter()
        _lifecycle_status({"event": "phase_timing", "phase": "cleanup", "phase_id": "cleanup",
                    "status": "running", "started_at": time.time()})
        try:
            if env is not None:
                env.close()
        finally:
            # 初始化、运行或环境关闭失败时，仍清理本次创建的实例。
            try:
                if started_chrome:
                    stop_chrome(args.browser_port, chrome_profile)
            finally:
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)
                _lifecycle_status({"event": "phase_timing", "phase": "cleanup", "phase_id": "cleanup",
                    "status": "completed", "elapsed_ms": round((time.perf_counter()-cleanup_started)*1000, 2)})

    # 通知前端任务结束（带结果），否则前端收不到 task_end 会把"已完成"误判成"异常终止"
    termination_reason = getattr(result, 'termination_reason', '')
    planning_calls = getattr(result, 'planning_calls', []) or []
    log_status({
        "event": "task_end",
        "result": result.score,
        "steps": result.steps,
        "success": bool(result.success),
        "answer": result.final_answer or "",
        "termination_reason": termination_reason,
        "validation_error": next((str(c.get('validation_error')) for c in reversed(planning_calls)
                                  if c.get('validation_error')), '') if termination_reason == 'planning_failed' else '',
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
                    shot.save(os.path.join(args.result_dir, log_name(f"step_{step_no}.png")))
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
        with open(os.path.join(args.result_dir, log_name("traj_raw.jsonl")), "w", encoding="utf-8") as _f:
            _f.write("\n".join(raw_lines))
        from osworld_agent.trajectory import build_trajectory, dump_trajectory
        import hashlib
        task_id = args.task_id or "local-" + hashlib.sha256(args.task.encode()).hexdigest()[:16]
        traj = build_trajectory(task_id=task_id, domain=platform,
                                instruction=args.task, result=result)
        dump_trajectory(os.path.join(args.result_dir, log_name("trajectory.json")), traj)
        print(f"\n轨迹与截图已保存到: {args.result_dir}")
    flush_logs(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
