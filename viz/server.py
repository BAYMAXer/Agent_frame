"""viz 服务器：前端启动 / 实时看过程 / 看历史。

适配新框架（osworld_agent）：
- 核心端点 /log /history /ws /run/* /runs /tasks 保留，契约与原 Agent-S3 viz 一致；
- /experiments/* 和 /debug/* 是原框架的进阶功能，这里做最小 stub，避免前端报错；
- /run/start 启动的是新框架的 osworld_agent.viz.runner（跑 VMware 真实任务）。

运行：python -m osworld_agent.viz.server
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI(title="osworld_agent Viz")


@app.get('/runtime/info')
async def runtime_info():
    """Identify the actual executor host, including through an SSH tunnel."""
    return {'service': 'yaero-studio', 'platform': sys.platform,
            'local_execution': True}


from .jev_debug import router as jev_router
app.include_router(jev_router)
from .jev_demo import router as jev_demo_router
try:
    from .jev_compare import router as jev_compare_router
except ModuleNotFoundError as exc:
    if exc.name not in {'osworld_agent.comparison', 'osworld_agent.comparison.service'}:
        raise
else:
    app.include_router(jev_compare_router)
app.include_router(jev_demo_router)
from .paper_search_fixture import router as paper_search_router
app.include_router(paper_search_router)
from .workspace_picker import router as workspace_router
app.include_router(workspace_router)
from .knowledge import router as knowledge_router
app.include_router(knowledge_router)


@app.get('/browser/session')
async def get_browser_session_settings():
    from osworld_agent.persistent_chrome import browser_settings
    return {'ok': True, **browser_settings()}


@app.post('/browser/session')
async def set_browser_session_settings(request: Request):
    from fastapi import HTTPException
    from osworld_agent.persistent_chrome import save_browser_settings
    try:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError('浏览器设置必须为对象')
        from osworld_agent.persistent_chrome import browser_settings
        saved = browser_settings()
        mode = value.get('browser_session_mode', 'persistent')
        profile = value.get('browser_profile_dir', saved['browser_profile_dir'])
        if mode == 'isolated' and not profile:
            profile = saved['browser_profile_dir']
        settings = save_browser_settings(mode, profile)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {'ok': True, **settings}

# ---- 配置（环境变量覆盖）----
_PKG = Path(__file__).resolve().parent.parent          # osworld_agent 包目录
_PROJECT_ROOT = _PKG.parent                            # 项目根（含 osworld_agent 与 AgentS）
AGENT_ROOT = os.getenv("OSWORLD_AGENT_ROOT", str(_PROJECT_ROOT))  # 跑 runner 的 cwd
EXAMPLES_DIR = os.getenv("OSWORLD_EXAMPLES_DIR", "")
DESKTOP_ENV_PATH = os.getenv("OSWORLD_DESKTOP_ENV_PATH", "")
VM_PATH = os.getenv("OSWORLD_VM_PATH", "")
RESULTS_DIR = os.getenv("AGENTS_RESULTS_DIR", str(_PKG / "viz" / "runs"))
LOCAL_RUNS_DIR = os.getenv("OSWORLD_LOCAL_RUNS_DIR", str(_PKG / "local_runs"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _runner_python() -> str:
    """确定跑 runner 的 Python 解释器。

    desktop_env 的依赖（gymnasium 等）装在 AgentS 的 .venv 里，而 viz server 自身可能
    用系统 Python（只装了 fastapi/uvicorn）启动。若 runner 直接复用 sys.executable，会在
    import desktop_env 时崩（ModuleNotFoundError: gymnasium）。因此优先用 AgentS 的 .venv。
    """
    env_py = os.getenv("OSWORLD_RUNNER_PYTHON")
    if env_py:
        return env_py
    if DESKTOP_ENV_PATH:
        for rel in (os.path.join(".venv", "Scripts", "python.exe"),   # Windows
                    os.path.join(".venv", "bin", "python")):           # POSIX
            venv_py = os.path.join(DESKTOP_ENV_PATH, rel)
            if os.path.isfile(venv_py):
                return venv_py
    return sys.executable


def _detect_vm_path() -> str:
    """自动探测已解压的 VMware VM（.vmx）。

    OSWorld 的 vmware manager 用 registry（.vmware_vms）记录空闲 VM，registry 一旦为空
    就会走 _install_vm 从 HF 重新下载 Ubuntu-x86.zip（被内网代理 SSL 拦截）。而 VM 本体
    早已解压在 {DESKTOP_ENV_PATH}/vmware_vm_data/ 下，直接把 .vmx 路径传给 DesktopEnv
    （path_to_vm 非空即跳过 registry/下载），即可复用。
    """
    if not DESKTOP_ENV_PATH:
        return ""
    vms_dir = os.path.join(DESKTOP_ENV_PATH, "vmware_vm_data")
    if not os.path.isdir(vms_dir):
        return ""
    for name in sorted(os.listdir(vms_dir)):
        d = os.path.join(vms_dir, name)
        vmx = os.path.join(d, name + ".vmx")
        if os.path.isdir(d) and os.path.isfile(vmx):
            return vmx
    return ""


MAX_LOGS = 200
logs = []
clients = []
_workbench_cache = OrderedDict()
_workbench_sequence = 0
_workbench_run = None


def _cache_workbench(payload):
    """Latest reconstructable run state, independent of the 200 debug-log cap.

    Reconnects replay only changed records, not every screenshot on each poll.
    """
    global _workbench_sequence, _workbench_run
    event = payload.get('event')
    if event == 'workbench_start':
        _workbench_cache.clear()
        _workbench_run = payload.get('run_id')
    if not _workbench_run or payload.get('run_id') != _workbench_run:
        return
    relevant = {'workbench_start', 'workbench_record', 'execution_queue', 'step_verification',
                'report_start', 'report_delta', 'report_complete', 'report_error', 'report_verification',
                'run_metrics', 'live_frame', 'framework_wait', 'phase_timing'}
    if event not in relevant:
        return
    key = event
    if event == 'workbench_record':
        record = payload.get('record') or {}
        kind = record.get('event_type')
        ident = ((record.get('call') or {}).get('call_id') if kind == 'model_call' else
                 record.get('revision') if kind == 'plan_revision' else
                 record.get('parent_decision_id') or record.get('step'))
        key = (event, kind, ident or record.get('event_id'))
    elif event == 'step_verification':
        key = (event, payload.get('review_id'), payload.get('method'))
    elif event == 'execution_queue':
        key = (event, (payload.get('queue') or {}).get('revision'))
    elif event == 'framework_wait':
        key = (event, payload.get('step'))
    _workbench_sequence += 1
    payload['_wb_seq'] = _workbench_sequence
    _workbench_cache[key] = dict(payload)


# 内网模型端点直连，不走华为 HIS 代理
_no_proxy = os.environ.get("NO_PROXY", "")
for _seg in ("7.0.0.0/8", "192.168.0.0/16", "10.0.0.0/8", "localhost", "127.0.0.1"):
    if _seg not in _no_proxy:
        _no_proxy = _seg if not _no_proxy else _no_proxy + "," + _seg
os.environ["NO_PROXY"] = _no_proxy
os.environ["no_proxy"] = _no_proxy

_agent_state = {
    "status": "idle", "proc": None, "task_name": None, "config": None,
    "start_time": None, "output": [], "current_step": 0, "max_steps": 0, "task_total": 0,
    "last_error": "",
    "run_id": None,
}

# Human demonstrations use their own browser context and never invoke a model.
from .collection import CollectionController, collection_router
import atexit

collection = CollectionController(os.getenv("OSWORLD_SFT_DIR", str(_PKG / "human_runs")),
                                 agent_busy=lambda: _studio_agent_busy(),
                                 desktop_lease_factory=lambda owner: _desktop_lease(owner))
app.include_router(collection_router(collection))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
atexit.register(collection.close)


def _reader_thread(proc, timing_path=None, started=None, desktop_lease=None):
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                _agent_state["output"].append(line)
                if len(_agent_state["output"]) > 500:
                    _agent_state["output"] = _agent_state["output"][-500:]
    except Exception:
        pass
    finally:
        rc = proc.wait()
        if desktop_lease is not None:
            desktop_lease.close()
        elapsed = round(time.perf_counter() - started, 3) if started is not None else None
        if timing_path and elapsed is not None:
            try:
                path = Path(timing_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'elapsed_s': elapsed, 'scope': 'process', 'exit_code': rc}))
                os.replace(temporary, path)
            except OSError:
                pass
        if _agent_state.get('proc') is proc:
            _agent_state['elapsed_final'] = elapsed
        if _agent_state.get('proc') is proc and _agent_state["status"] == "running":
            _agent_state["status"] = "done" if rc == 0 else "failed"
            if rc != 0:
                _agent_state["last_error"] = next(
                    (line for line in reversed(_agent_state["output"])
                     if re.match(r"^[\w.]+(?:Error|Exception):", line)),
                    f"任务进程退出，代码 {rc}。可查看运行输出。")


# ================= 实时日志 ================= #
def _safe_jev_ui_value(value):
    """Redact credentials, including JSON encoded inside SystemOne state text."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
            if (normalized in {'authorization', 'token', 'password', 'passwd', 'secret', 'cookie', 'setcookie'}
                    or normalized.endswith(('apikey', 'password', 'token', 'clientsecret'))):
                continue
            result[key] = _safe_jev_ui_value(item)
        return result
    if isinstance(value, list):
        return [_safe_jev_ui_value(item) for item in value]
    if isinstance(value, str) and not value.startswith('data:image/'):
        if value.lstrip().startswith(('{', '[')):
            try:
                return json.dumps(_safe_jev_ui_value(json.loads(value)), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9]{16,}', '[redacted]', value)
    return value


def _jev_call_for_ui(call):
    request = call.get('request') if isinstance(call, dict) else None
    if not isinstance(request, dict) or 'state' not in request or not isinstance(request.get('questions'), dict):
        return None
    return _safe_jev_ui_value(call)


def _paper_identity_call_for_ui(call):
    if (not isinstance(call, dict) or call.get('call_type') != 'paper_identity'
            or not isinstance(call.get('selection_result'), dict)):
        return None
    return _safe_jev_ui_value({key: call[key] for key in (
        'call_id', 'call_type', 'decision_step', 'model', 'messages', 'response',
        'selection_result', 'latency_ms', 'status', 'error') if key in call})


def _paper_identity_entry_for_ui(call):
    call = _paper_identity_call_for_ui(call)
    if call is None:
        return None
    query = ''
    for message in call.get('messages') or []:
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        try:
            payload = json.loads(message.get('content', ''))
            if isinstance(payload, dict):
                query = str(payload.get('original_query') or '')
        except (TypeError, ValueError):
            continue
    return {'query': query, 'report': call['selection_result'], 'call': call}


@app.post("/log")
async def receive_log(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    if payload.get('event') == 'jev_call':
        payload = {**payload, 'call': _jev_call_for_ui(payload.get('call'))}
    elif payload.get('event') == 'paper_identity_review':
        payload = _safe_jev_ui_value(payload)
        if payload.get('call') is not None:
            payload['call'] = _paper_identity_call_for_ui(payload['call'])
        if isinstance(payload.get('calls'), list):
            payload['calls'] = [call for item in payload['calls']
                                if (call := _paper_identity_call_for_ui(item)) is not None]
    payload.setdefault("ts", time.time())
    _cache_workbench(payload)
    stored = dict(payload)
    # 保留 screenshot：前端 poll() 从 /history 拿日志渲染，去掉截图会导致实时监控截图缺失
    # （poll 抢先消费事件、WebSocket 的有截图事件被 seenKeys 去重跳过）。
    if payload.get('event') == 'live_frame':
        # A preview is ephemeral; keep only the latest frame per active run.
        logs[:] = [e for e in logs if not (e.get('event') == 'live_frame' and e.get('run_id') == payload.get('run_id'))]
    logs.append(stored)
    if len(logs) > MAX_LOGS:
        del logs[0:len(logs) - MAX_LOGS]
    if payload.get("kind") == "status":
        ev = payload.get("event")
        if ev == "step" and payload.get("step") is not None:
            _agent_state["current_step"] = max(_agent_state["current_step"] or 0, int(payload["step"]))
        elif ev == "task_start":
            _agent_state["current_step"] = 0
    msg = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)
    return {"ok": True, "total": len(logs)}


@app.get("/history")
async def history(since: int = 0):
    if _workbench_cache:
        return {'logs': sorted((v for v in _workbench_cache.values() if v['_wb_seq'] > since), key=lambda v:v['_wb_seq']),
                'cursor': _workbench_sequence}
    return {"logs": logs, 'cursor': 0}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass
    finally:
        if ws in clients:
            clients.remove(ws)


@app.get("/")
async def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ================= 任务列表 & 历史 ================= #
@app.get("/tasks")
async def list_tasks(test_all_meta_path: str = "evaluation_examples/test_small.json"):
    """返回所选测试集内的任务，按域名分组。

    优先读测试集 meta（如 evaluation_examples/test_small.json，相对 DESKTOP_ENV_PATH 即 AgentS 根目录），
    只返回该测试集的任务子集；meta 读不到时回退到扫描全部 examples。
    """
    if DESKTOP_ENV_PATH:
        meta_path = os.path.abspath(os.path.join(DESKTOP_ENV_PATH, test_all_meta_path))
        try:
            if meta_path.endswith(".json"):
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                if isinstance(meta, dict):
                    return {"tasks": {
                        domain: ids for domain, ids in sorted(meta.items())
                        if isinstance(ids, list)
                    }}
        except Exception:
            pass

    # 回退：扫描全部 examples
    tasks = {}
    if EXAMPLES_DIR and os.path.isdir(EXAMPLES_DIR):
        for domain in sorted(os.listdir(EXAMPLES_DIR)):
            domain_dir = os.path.join(EXAMPLES_DIR, domain)
            if not os.path.isdir(domain_dir):
                continue
            ids = sorted(f[:-5] for f in os.listdir(domain_dir) if f.endswith(".json"))
            if ids:
                tasks[domain] = ids
    if not tasks and EXAMPLES_DIR and os.path.isdir(EXAMPLES_DIR):
        flat = sorted(f[:-5] for f in os.listdir(EXAMPLES_DIR) if f.endswith(".json"))
        if flat:
            tasks["all"] = flat
    return {"tasks": tasks}


@app.get("/runs")
async def list_runs():
    runs = []
    base = os.path.join(RESULTS_DIR, "pyautogui", "screenshot")
    if os.path.isdir(base):
        for model in os.listdir(base):
            model_dir = os.path.join(base, model)
            if not os.path.isdir(model_dir):
                continue
            for domain in os.listdir(model_dir):
                domain_dir = os.path.join(model_dir, domain)
                if not os.path.isdir(domain_dir):
                    continue
                for task_id in os.listdir(domain_dir):
                    task_dir = os.path.join(domain_dir, task_id)
                    if not os.path.isdir(task_dir):
                        continue
                    instruction = result = None
                    try:
                        with open(os.path.join(task_dir, "instruction.txt"), encoding="utf-8") as f:
                            instruction = f.read().strip()
                    except Exception:
                        pass
                    try:
                        with open(os.path.join(task_dir, "result.txt")) as f:
                            result = float(f.read().strip())
                    except Exception:
                        pass
                    shots = [f for f in os.listdir(task_dir)
                             if f.startswith("step_") and f.endswith(".png")]
                    example_id, run_ts = task_id, ""
                    if "__" in task_id:
                        example_id, run_ts = task_id.rsplit("__", 1)
                    runs.append({
                        "model": model, "domain": domain, "task_id": task_id,
                        "example_id": example_id, "run_ts": run_ts,
                        "instruction": instruction or "", "result": result,
                        "steps": len(shots), "mtime": os.path.getmtime(task_dir),
                        "error": None, "experiment": {},
                    })
    runs.sort(key=lambda r: r.get("mtime", 0), reverse=True)
    return {"runs": runs}


# ================= 本地运行（独立 local_runs；兼容旧工程日期目录）================= #
_LOCAL_SUMMARY_CACHE_LIMIT = 512
_local_summary_cache = OrderedDict()
_local_summary_cache_lock = threading.Lock()
_TOKEN_CACHE_LIMIT = 512
_token_cache = OrderedDict()
_token_cache_lock = threading.Lock()


def _local_run_summary_from_events(events_path):
    """从运行中实时写入的 events.jsonl 提取摘要（失败/中断的运行没有 trajectory.json）。"""
    empty = {"instruction": "", "score": None, "success": None, "steps": 0,
             "termination_reason": "failed_no_log", "failure_reason": ""}
    try:
        task = ""
        reason = ""
        success = None
        steps = 0
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("event_type")
                if t == "episode_start":
                    task = (ev.get("manifest") or {}).get("task", "")
                elif t == "execution":
                    steps += 1
                elif t == "episode_end":
                    o = ev.get("outcome") or {}
                    reason = o.get("termination_reason") or ""
                    success = o.get("success")
                elif t == "episode_error":
                    reason = ev.get('error_type') or 'episode_error'
                    success = False
        if not task and not reason and not steps:
            return empty
        return {"instruction": task, "score": None, "success": success,
                "steps": steps, "termination_reason": reason or "interrupted",
                "failure_reason": ""}
    except Exception:  # noqa: BLE001
        return empty


def _events_path_for(traj_path, directory, prefix):
    """给定轨迹路径，推导同一次运行的 events.jsonl 路径（平铺/子目录两种布局）。"""
    if traj_path is None:
        return None
    if traj_path.name.endswith(".jsonl"):
        return traj_path  # 已经是 events.jsonl（无 trajectory 的兜底记录）
    return directory / (prefix + "events.jsonl")


def _run_token_usage(events_path):
    """One canonical event stream; missing usage remains explicitly partial."""
    from ..run_metrics import summarize_events
    if not events_path or not os.path.isfile(events_path):
        return summarize_events([])
    stat = os.stat(events_path)
    key, version = str(os.path.abspath(events_path)), (stat.st_mtime_ns, stat.st_size)
    with _token_cache_lock:
        cached = _token_cache.get(key)
        if cached and cached[0] == version:
            return dict(cached[1])
    def events():
        with open(events_path, encoding='utf-8') as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except ValueError:
                    continue
    result = summarize_events(events())
    with _token_cache_lock:
        _token_cache[key] = (version, dict(result))
        _token_cache.move_to_end(key)
        while len(_token_cache) > _TOKEN_CACHE_LIMIT:
            _token_cache.popitem(last=False)
    return result


def _local_metrics(events_path, directory, prefix='', fallback=None):
    metrics = _run_token_usage(events_path)
    if (not events_path or not Path(events_path).exists()) and fallback:
        metrics = dict(fallback)
    # The launcher records the whole process span, including browser boot,
    # export and cleanup. Pipeline time is retained as a separate scope.
    try:
        timing = json.loads((directory / (prefix + 'run_timing.json')).read_text())
        elapsed = timing.get('elapsed_s')
        if isinstance(elapsed, (int, float)) and 0 <= elapsed < 1e9:
            metrics = {**metrics, 'pipeline_elapsed_s': metrics.get('elapsed_s'),
                       'elapsed_s': elapsed, 'timing_source': 'process', 'complete': True}
    except (OSError, ValueError, TypeError):
        pass
    return metrics



_SUMMARY_HEAD_LIMIT = 4 * 1024 * 1024  # task/outcome 都很小且位于 steps 之前


def _summary_head_parse(traj_path):
    """只解析 trajectory.json 头部（version/task/outcome），跳过巨型 steps 数组。

    trajectory.json 顶层顺序为 version, task, outcome, steps, ...；task/outcome 在
    steps 之前且都很小。定位到顶层 "steps" 键即截断、补闭合并解析，避免对大文件做
    整包 json.load。解析失败返回 None，调用方回退到完整解析。
    """
    try:
        with open(traj_path, encoding="utf-8") as f:
            head = f.read(_SUMMARY_HEAD_LIMIT)
    except OSError:
        return None
    match = re.search(r'"steps"\s*:', head)
    if not match or not head.lstrip().startswith('{'):
        return None
    prefix = head[:match.start()].rstrip().rstrip(',') + '}'
    try:
        return json.loads(prefix)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _local_run_summary(traj_path):
    """Cache only small list metadata; a changed trajectory is read again."""
    empty = {"instruction": "", "score": None, "success": None, "steps": 0,
             "termination_reason": "", "failure_reason": ""}
    if traj_path is None:
        return {"instruction": "", "score": None, "success": False, "steps": 0,
                "termination_reason": "failed_no_log", "failure_reason": ""}
    try:
        before = traj_path.stat()
        path_key = str(traj_path.resolve())
        version = (before.st_mtime_ns, before.st_size)
        with _local_summary_cache_lock:
            cached = _local_summary_cache.get(path_key)
            if cached is not None and cached[0] == version:
                _local_summary_cache.move_to_end(path_key)
                return dict(cached[1])
        if str(traj_path).endswith("events.jsonl"):
            summary = _local_run_summary_from_events(traj_path)
        else:
            # 快路径：只读头部（version/task/outcome），避免整包解析巨型 steps
            traj = _summary_head_parse(traj_path)
            if traj is not None and (traj.get("outcome") or {}).get("num_steps") is None:
                traj = None  # num_steps 缺失，需完整解析数步数
            if traj is None:
                with open(traj_path, encoding="utf-8") as f:
                    traj = json.load(f)
            outcome = traj.get("outcome") or {}
            reason = outcome.get("termination_reason") or ""
            num_steps = outcome.get("num_steps")
            if num_steps is None:
                num_steps = len(traj.get("steps") or [])
            failure_reason = ""
            if reason == "planning_failed":
                if "planning_calls" not in traj:
                    with open(traj_path, encoding="utf-8") as f:
                        traj = json.load(f)
                failure_reason = next((str(c.get("validation_error")) for c in reversed(
                    traj.get("planning_calls") or []) if c.get("validation_error")), "")
            summary = {"instruction": (traj.get("task") or {}).get("instruction", ""),
                       "score": outcome.get("score"), "success": outcome.get("success"),
                       "steps": num_steps,
                       "termination_reason": reason,
                       "failure_reason": failure_reason, "run_metrics": outcome.get("run_metrics", {})}
        after = traj_path.stat()
        if version == (after.st_mtime_ns, after.st_size):
            with _local_summary_cache_lock:
                _local_summary_cache[path_key] = (version, dict(summary))
                _local_summary_cache.move_to_end(path_key)
                while len(_local_summary_cache) > _LOCAL_SUMMARY_CACHE_LIMIT:
                    _local_summary_cache.popitem(last=False)
        return summary
    except Exception:  # noqa: BLE001
        # An in-progress or invalid JSON file must not poison the cache.
        return empty


def _read_local_runs():
    runs, shots_by_directory = [], {}
    for run_id, traj_path, directory, prefix in _local_run_files():
        summary = _local_run_summary(traj_path)
        summary["tokens"] = _local_metrics(_events_path_for(traj_path, directory, prefix), directory, prefix, summary.get("run_metrics"))
        try:
            mtime = (traj_path.stat().st_mtime if traj_path is not None
                     else directory.stat().st_mtime)
            if directory not in shots_by_directory:
                shots_by_directory[directory] = [name for name in os.listdir(directory)
                                                 if name.endswith(".png")]
            shots = sum(name.startswith(prefix + "step_")
                        for name in shots_by_directory[directory])
        except OSError:
            continue  # A run may be removed while the directory is being scanned.
        runs.append({"id": run_id, **summary, "shots": shots, "mtime": mtime})
    runs.sort(key=lambda item: item['mtime'], reverse=True)
    return {"runs": runs}


@app.get("/local/runs")
async def local_runs():
    """列出独立运行日志，同时兼容工程日期目录中的旧轨迹。"""
    return await asyncio.to_thread(_read_local_runs)


def _local_run_files():
    """Read new per-run logs and legacy flat workspace logs without moving files."""
    from osworld_agent.workspace_config import default_workspace_root
    root = Path(default_workspace_root())
    if root.is_dir():
        for day in root.iterdir():
            if not day.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day.name):
                continue
            for path in day.glob('ui_*_trajectory.json'):
                run_id = path.name[:-len('_trajectory.json')]
                if re.fullmatch(r'ui_\d{8}_\d{6}_[0-9a-f]{8}', run_id):
                    yield run_id, path, day, run_id + '_'
    legacy = Path(LOCAL_RUNS_DIR)
    if legacy.is_dir():
        for directory in legacy.iterdir():
            if not directory.is_dir():
                continue
            standard = directory / 'trajectory.json'
            if standard.is_file():
                yield directory.name, standard, directory, ''
                continue
            candidates = sorted(directory.glob('*_trajectory.json'))
            for path in candidates:
                prefix = path.name[:-len('trajectory.json')]
                run_id = directory.name if len(candidates) == 1 else directory.name + '__' + path.stem
                yield run_id, path, directory, prefix
            # 兜底：没有 trajectory.json 但写了 events.jsonl（stalled/中断的运行也保留）
            events = directory / 'events.jsonl'
            if events.is_file():
                yield directory.name, events, directory, ''
            # 更早失败（连 events.jsonl 都没写）也保留一条"失败"记录
            elif not any(directory.iterdir()):
                yield directory.name, None, directory, ''


def _local_run_path(run):
    if not isinstance(run, str) or '/' in run or '\\' in run or run in {'', '.', '..'}:
        return None
    for run_id, path, directory, prefix in _local_run_files():
        if run_id == run:
            return path, directory, prefix
    return None


def _local_traj_to_frontend(traj, prefix=''):
    """把标准 trajectory.json 的 steps 转成前端 traj.jsonl 格式（复用历史视图渲染）。"""
    lines = []
    calls_by_step = {}
    identity_by_step = {}
    identity_diagnostics = {}
    for event in traj.get('events') or []:
        if not isinstance(event, dict):
            continue
        if event.get('event_type') == 'paper_identity_review' and isinstance(event.get('report'), dict):
            parent = re.fullmatch(r'a_(\d+)', str(event.get('parent_decision_id') or ''))
            step_id = parent.group(1) if parent else event.get('decision_step', event.get('step'))
            if re.fullmatch(r'\d+', str(step_id or '')):
                event_call = event.get('call') or next((call for call in reversed(event.get('calls') or [])
                    if _paper_identity_call_for_ui(call) is not None), None)
                identity_diagnostics.setdefault(str(int(step_id)), []).append(_safe_jev_ui_value({
                    'query': event.get('query', ''), 'report': event['report'],
                    'call': _paper_identity_call_for_ui(event_call)}))
        if event.get('event_type') not in {'model_call', 'jev_call'}:
            continue
        identity = _paper_identity_entry_for_ui(event.get('call'))
        if identity is not None:
            parent = re.fullmatch(r'a_(\d+)', str(event.get('parent_decision_id') or ''))
            step_id = parent.group(1) if parent else identity['call'].get('decision_step')
            if re.fullmatch(r'\d+', str(step_id or '')):
                identity_by_step.setdefault(str(int(step_id)), []).append(identity)
        call = _jev_call_for_ui(event.get('call'))
        if call is None:
            continue
        parent = re.fullmatch(r'a_(\d+)', str(event.get('parent_decision_id') or ''))
        step_id = parent.group(1) if parent else call.get('decision_step')
        if re.fullmatch(r'\d+', str(step_id or '')):
            calls_by_step.setdefault(str(int(step_id)), []).append(call)
    for call in traj.get('review_calls') or []:
        identity = _paper_identity_entry_for_ui(call)
        step_id = (identity or {}).get('call', {}).get('decision_step')
        if identity is not None and re.fullmatch(r'\d+', str(step_id or '')):
            identity_by_step.setdefault(str(int(step_id)), []).append(identity)
    for s in traj.get("steps") or []:
        action = s.get("action")
        action_str = json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else str(action or "")
        reasoning = s.get("reasoning") or {}
        assessment = s.get("assessment") or {}
        grounding = s.get("grounding") or {}
        resolved_action = s.get('resolved_action') or {}
        if isinstance(resolved_action, str):
            try:
                resolved_action = json.loads(resolved_action)
            except (TypeError, ValueError):
                resolved_action = {}
        timing = s.get("timing_ms") or {}
        gs = []
        if grounding and grounding.get("coord"):
            gs.append({"query": grounding.get("target", ""),
                       "coord": grounding.get("coord"),
                       "elapsed": timing.get("ground")})
        # 决策耗时：decision_calls 最后一条的 latency_ms（LoggingChatModel 转发后才有）
        decision_elapsed = None
        dc = s.get("decision_calls") or []
        if dc:
            decision_elapsed = dc[-1].get("latency_ms")
        # 子 Agent 调用：从 execution_info 里提取 call_code_agent
        ei = s.get("execution_info") or {}
        jev_calls, seen_calls = [], set()
        for raw_call in [*(s.get('jev_calls') or []), *calls_by_step.get(str(s.get('step')), [])]:
            call = _jev_call_for_ui(raw_call)
            if call is None:
                continue
            ident = call.get('call_id') or json.dumps(call, ensure_ascii=False, sort_keys=True)
            if ident not in seen_calls:
                seen_calls.add(ident)
                jev_calls.append(call)
        identity_reviews, seen_identity_calls = [], set()
        identity_entries = [*identity_by_step.get(str(s.get('step')), []),
                            *(_paper_identity_entry_for_ui(call) for call in s.get('review_calls') or [])]
        for identity in identity_entries:
            if identity is None:
                continue
            call = identity['call']
            ident = call.get('call_id') or json.dumps(call, ensure_ascii=False, sort_keys=True)
            if ident not in seen_identity_calls:
                seen_identity_calls.add(ident)
                identity_reviews.append(identity)
        for identity in identity_diagnostics.get(str(s.get('step')), []):
            call = identity.get('call') or {}
            if (call.get('call_id') in seen_identity_calls or any(
                    old.get('query') == identity.get('query') and old.get('report') == identity.get('report')
                    for old in identity_reviews)):
                continue
            if call.get('call_id'):
                seen_identity_calls.add(call['call_id'])
            identity_reviews.append(identity)
        subagent = None
        if ei.get("action") == "call_code_agent":
            subagent = {"task": ei.get("task", ""), "result": ei.get("result", "")}
        lines.append({
            "step_num": s.get("step"),
            "plan": reasoning.get("screen_analysis", ""),
            "expected_effect": reasoning.get("expected_effect", ""),
            "action": action_str,
            "reward": s.get("reward"),
            "done": bool(s.get("done", False)),
            "screenshot_file": f"{prefix}step_{s.get('step')}.png",
            "grounding": gs,
            "resolved_action": resolved_action,
            "routing": s.get("routing") or {},
            "operation_mode": s.get("operation_mode"),
            "prompt_metadata": s.get("prompt_metadata") or {},
            "execution_info": {key: ei[key] for key in
                               ("status", "dispatched", "channel", "resolved", "error_code", "reason", "coord", "coordinate_space")
                               if key in ei},
            "outcome": (s.get("execution") or {}).get("outcome", ""),
            "assessment": assessment,
            "step_verification": s.get("step_verification"),
            "step_reviews": s.get("step_reviews", []),
            "jev_calls": jev_calls,
            "paper_identity_reviews": identity_reviews,
            "execution_queue": s.get("execution_queue"),
            "prompt": s.get("prompt_text", ""),
            "timing_ms": timing,
            "decision_elapsed_ms": decision_elapsed,
            "subagent": subagent,
        })
    return lines


def _read_local_traj(run):
    found = _local_run_path(run)
    if found is None:
        return {"ok": False, "error": "轨迹不存在"}
    traj_path, directory, prefix = found
    if traj_path.name.endswith('.jsonl'):
        summary = _local_run_summary_from_events(traj_path)
        from .workbench import project_event_workbench
        from ..training_events import read_events
        metrics = _local_metrics(traj_path, directory, prefix)
        try:
            workbench = project_event_workbench(read_events(traj_path), summary['instruction'], metrics)
        except (OSError, ValueError):
            return {'ok': False, 'error': '事件记录损坏，无法可靠重建轨迹'}
        return {'ok': True, 'traj': [], 'instruction': summary['instruction'], 'workbench': workbench,
                'outcome': {**summary, 'final_answer': '运行未导出完整轨迹；展示已落盘事件。'},
                'tokens': metrics}
    try:
        with open(traj_path, encoding="utf-8") as f:
            traj = json.load(f)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"轨迹解析失败：{exc}"}
    from .workbench import project_workbench
    metrics = _local_metrics(_events_path_for(traj_path, directory, prefix), directory, prefix, (traj.get('outcome') or {}).get('run_metrics'))
    workbench = project_workbench(traj)
    workbench['metrics'] = metrics
    return {"ok": True, "workbench": workbench, "traj": _local_traj_to_frontend(traj, prefix),
            "instruction": (traj.get("task") or {}).get("instruction", ""),
            "plan": traj.get("plan"),
            "outcome": traj.get("outcome") or {},
            "planning_errors": [{"attempt": i + 1, "reason": c.get("validation_error")}
                                for i, c in enumerate(traj.get("planning_calls") or [])
                                if c.get("validation_error")],
            "tokens": metrics,
            "knowledge_retrieval": (traj.get("run_manifest") or {}).get("knowledge_retrieval"),
            "replans": [{"event": e.get("event_type"), **{k: e[k] for k in ("brief", "strategy_change", "revision") if k in e}}
                        for e in traj.get("events", []) if e.get("event_type") in {"replan_context", "replan_strategy"}]}


@app.get("/local/traj")
async def local_traj(run: str):
    """返回某个本地运行的轨迹；磁盘扫描与解析在后台线程执行。"""
    return await asyncio.to_thread(_read_local_traj, run)


class ResultFileAction(BaseModel):
    run: str
    artifact_id: str
    operation: Literal['open', 'reveal']
    scope: Literal['history', 'live'] = 'history'


@app.post('/local/result-file')
async def result_file_action(body: ResultFileAction):
    from .result_files import open_download
    if body.scope == 'live':
        if body.run != _workbench_run:
            return {'ok': False, 'error': '当前运行已切换，请从本机轨迹打开原任务。'}
        outcome = next((p['record'].get('outcome', {}) for p in reversed(list(_workbench_cache.values()))
                        if p.get('event') == 'workbench_record'
                        and p.get('record', {}).get('event_type') == 'episode_end'), {})
    else:
        data = await asyncio.to_thread(_read_local_traj, body.run)
        if not data.get('ok'):
            return {'ok': False, 'error': '轨迹不存在，无法核实下载文件。'}
        outcome = data.get('workbench', {}).get('outcome', {})
    return await asyncio.to_thread(open_download, outcome, body.artifact_id, body.operation)


@app.get("/local/shot")
async def local_shot(run: str, filename: str):
    """返回某个本地运行的截图（step_N.png）。"""
    filename = os.path.basename(filename)  # 防目录穿越
    found = _local_run_path(run)
    if found is None:
        return Response(status_code=404)
    _, directory, prefix = found
    if not re.fullmatch(re.escape(prefix) + r'step_\d+\.png', filename):
        return Response(status_code=404)
    shot = directory / filename
    if shot.is_file():
        return FileResponse(str(shot))
    return Response(status_code=404)


@app.get('/local/jev-image')
async def local_jev_image(run: str, filename: str):
    """Serve only content-addressed screenshot assets from the selected run."""
    if not re.fullmatch(r'assets/[0-9a-f]{64}\.(?:png|jpg|webp)', filename):
        return Response(status_code=404)
    found = _local_run_path(run)
    if found is None:
        return Response(status_code=404)
    _, directory, _ = found
    asset_root = (directory / 'assets').resolve()
    path = (directory / filename).resolve()
    if path.parent == asset_root and path.is_file():
        return FileResponse(str(path))
    return Response(status_code=404)


@app.post("/local/delete")
async def local_delete(request: Request):
    """删除某个本地运行。local_runs 独立子目录整目录删；工程日期目录只删该 run 的文件。"""
    import shutil
    try:
        cfg = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    run = cfg.get("run")
    if not isinstance(run, str) or not run:
        return {"ok": False, "error": "missing run id"}
    found = _local_run_path(run)
    if found is None:
        return {"ok": False, "error": "not found"}
    _, directory, prefix = found
    try:
        try:
            legacy_root = os.path.realpath(LOCAL_RUNS_DIR)
            real_dir = os.path.realpath(directory)
            in_legacy = real_dir != legacy_root and os.path.commonpath([legacy_root, real_dir]) == legacy_root
        except (ValueError, OSError):
            in_legacy = False
        if in_legacy:
            shutil.rmtree(directory)
        elif prefix:
            removed = 0
            for p in directory.glob(prefix + '*'):
                try:
                    if p.is_file():
                        p.unlink()
                        removed += 1
                except OSError:
                    continue
            if not removed:
                return {"ok": False, "error": "未找到可删除的文件"}
        else:
            return {"ok": False, "error": "无法识别的轨迹布局"}
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/run/traj")
async def get_traj(model: str, domain: str, task_id: str):
    base = os.path.join(RESULTS_DIR, "pyautogui", "screenshot", model, domain, task_id)
    lines = []
    traj_path = os.path.join(base, "traj.jsonl")
    if os.path.exists(traj_path):
        with open(traj_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except Exception:
                        pass
    instruction = ""
    ip = os.path.join(base, "instruction.txt")
    if os.path.exists(ip):
        with open(ip, encoding="utf-8") as f:
            instruction = f.read().strip()
    return {"traj": lines, "instruction": instruction, "config": {}, "system_prompt": ""}


@app.get("/run/trajectory")
async def get_trajectory(model: str, domain: str, task_id: str):
    """返回标准训练轨迹（trajectory.json，供前端展示 + 训练管线取用）。"""
    base = os.path.join(RESULTS_DIR, "pyautogui", "screenshot", model, domain, task_id)
    traj_path = os.path.join(base, "trajectory.json")
    if os.path.exists(traj_path):
        try:
            with open(traj_path, encoding="utf-8") as f:
                return {"ok": True, "trajectory": json.load(f)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"轨迹解析失败：{exc}"}
    return {"ok": False, "error": "该运行未导出标准轨迹（trajectory.json 不存在）"}


@app.get("/run/shot")
async def get_shot(model: str, domain: str, task_id: str, filename: str):
    shot_path = os.path.join(RESULTS_DIR, "pyautogui", "screenshot", model, domain, task_id, filename)
    if os.path.exists(shot_path):
        return FileResponse(shot_path)
    return Response(status_code=404)


@app.post("/run/delete")
async def delete_run(request: Request):
    import shutil
    try:
        cfg = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    model, domain, task_id = cfg.get("model"), cfg.get("domain"), cfg.get("task_id")
    if not (model and domain and task_id):
        return {"ok": False, "error": "missing params"}
    task_dir = os.path.join(RESULTS_DIR, "pyautogui", "screenshot", model, domain, task_id)
    if os.path.isdir(task_dir):
        shutil.rmtree(task_dir)
        return {"ok": True}
    return {"ok": False, "error": "not found"}


# ================= 进程管理 ================= #
@app.post("/run/test-connection")
async def test_connection(request: Request):
    import base64
    import io
    import secrets
    import requests as req
    from PIL import Image, ImageDraw
    from osworld_agent.config import resolve_secret
    try:
        cfg = await request.json()
    except Exception:
        cfg = {}
    model = cfg.get("model", "Qwen3.6-27B")
    url = cfg.get("model_url", "http://10.44.101.62:8094/v1")
    try:
        api_key = resolve_secret(cfg.get("model_api_key") or "EMPTY")
    except (RuntimeError, TypeError) as exc:
        return {"ok": False, "status": -1, "error": str(exc)}
    challenge = secrets.token_hex(3).upper()
    image = Image.new("RGB", (260, 90), "white")
    ImageDraw.Draw(image).text((35, 25), challenge, fill="black", stroke_width=1)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode()
    payload = {"model": model, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "读取图片里的六位字符，只输出 JSON 对象，字段名为 code。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
    ], "max_tokens": 32, "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False}}
    try:
        r = req.post(f"{url}/chat/completions", json=payload,
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
                     proxies={"http": None, "https": None})
        if r.status_code == 200:
            content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            try:
                recognized = str(json.loads(content).get("code") or "").strip().upper()
            except (TypeError, ValueError, json.JSONDecodeError):
                recognized = ""
            if recognized != challenge:
                return {"ok": False, "status": 200,
                        "error": "接口可调用，但未正确读取随机图片内容；不能作为截图决策模型"}
            return {"ok": True, "status": 200, "content": "视觉与 JSON 输出均通过",
                    "visual_support": "随机图片内容识别通过"}
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    except Exception as e:
        return {"ok": False, "status": -1, "error": str(e)[:300]}


_local_service = None
_local_service_lock = threading.Lock()


def _get_local_service():
    global _local_service
    with _local_service_lock:
        if _local_service is None:
            from osworld_agent.run_service import RunService
            _local_service = RunService(runs_root=LOCAL_RUNS_DIR)
    return _local_service


def _desktop_lease(owner):
    return _get_local_service().acquire_external(owner)


def _release_runner_lease(lease, proc=None):
    if lease is None:
        return
    if proc is not None and proc.poll() is None:
        # Preserve the child owner marker if terminating an external runner failed.
        lease.lock.close()
    else:
        lease.close()


def _studio_agent_busy():
    _sync_local_state()
    return _agent_state['status'] in {'starting', 'running', 'cancelling'}


def _sync_local_state():
    """Studio polls the service record; the service alone owns its subprocess."""
    run_id = _agent_state.get('run_id')
    if not run_id:
        return None
    snapshot = _get_local_service().get_task(run_id)
    status = snapshot['status']
    _agent_state['status'] = {'completed': 'done', 'cancelled': 'cancelled',
                              'timed_out': 'failed', 'interrupted': 'failed'}.get(status, status)
    lines = []
    record_dir = _get_local_service().runs_root / run_id
    for filename in ('stdout.log', 'stderr.log'):
        try:
            with (record_dir / filename).open('rb') as stream:
                stream.seek(max(0, stream.seek(0, os.SEEK_END) - 128 * 1024))
                lines.extend(stream.read().decode('utf-8', errors='replace').splitlines())
        except OSError:
            pass
    _agent_state['output'] = lines[-500:]
    _agent_state['last_error'] = snapshot.get('error') or ''
    _agent_state['current_step'] = max(_agent_state['current_step'] or 0,
                                     (snapshot.get('progress') or {}).get('current_step') or 0)
    try:
        metadata = json.loads((record_dir / 'run.json').read_text(encoding='utf-8'))
        snapshot['pid'] = metadata.get('pid')
    except (OSError, ValueError):
        pass
    if status not in {'starting', 'running', 'cancelling'}:
        _agent_state['elapsed_final'] = max(0, (snapshot.get('finished_at') or time.time()) -
                                           (snapshot.get('started_at') or snapshot.get('created_at') or time.time()))
        if status in {'failed', 'timed_out', 'interrupted'} and not _agent_state['last_error']:
            _agent_state['last_error'] = snapshot.get('termination_reason') or '任务未通过验收。'
    return snapshot


def _start_local(cfg: dict, viz_port: int | None = None):
    """Launch local tasks through the same service and desktop gate as MCP."""
    from osworld_agent.local_runtime import build_local_launch
    from osworld_agent.persistent_chrome import save_browser_settings
    from osworld_agent.workspace_config import allocate_local_result_dir
    task = str(cfg.get('task') or '').strip()
    if not task:
        return {'ok': False, 'error': '请输入任务描述'}
    try:
        max_steps = int(cfg.get('max_steps', 40))
        timeout_s = float(cfg.get('task_timeout_s', 1800))
        def build(run_id, result_dir):
            launch = build_local_launch(cfg, result_dir, python_executable=_runner_python(),
                viz_port=viz_port or int(os.getenv('AGENTS_VIZ_PORT', '8088')))
            settings = save_browser_settings(launch.metadata['browser_session_mode'],
                                            launch.metadata['browser_profile_dir'])
            cfg.update(settings)
            return launch
        snapshot = _get_local_service().start(task=task, request_id='studio-' + uuid.uuid4().hex,
            max_steps=max_steps, timeout_s=timeout_s,
            result_dir=cfg.get('result_dir') or allocate_local_result_dir(LOCAL_RUNS_DIR),
            launch_factory=build)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        return {'ok': False, 'error': str(exc)}
    _agent_state.update(task_name=task[:60], task_total=1, config=cfg, output=[], last_error='',
        current_step=0, max_steps=max_steps, start_time=time.time(),
        started_monotonic=time.perf_counter(), elapsed_final=None, status=snapshot['status'],
        proc=None, run_id=snapshot['run_id'])
    logs.clear()
    return {'ok': True, 'task': _agent_state['task_name'], 'task_total': 1,
            'run_id': snapshot['run_id']}


def _wait_cli_args(cfg):
    from osworld_agent.local_runtime import wait_cli_args
    return wait_cli_args(cfg)


def _judge_cli_args(cfg):
    from osworld_agent.local_runtime import judge_cli_args
    return judge_cli_args(cfg)


# ================= 批量运行（Excel 逐条任务） ================= #
_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_batch_state = {"running": False, "total": 0, "done": 0, "current": "",
                "results": [], "error": "", "stop": False, "report_path": "",
                "started_at": None}


def _cell_col(ref):
    m = re.match(r'([A-Z]+)', str(ref or ''))
    if not m:
        return None
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - ord('A') + 1)
    return col - 1


def _read_xlsx_rows(path):
    import zipfile
    from xml.etree import ElementTree
    rows = []
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ElementTree.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall(f'{{{_NS}}}si'):
                shared.append(''.join(t.text or '' for t in si.iter(f'{{{_NS}}}t')))
        sheet = 'xl/worksheets/sheet1.xml'
        if sheet not in z.namelist():
            names = [n for n in z.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml', n)]
            if not names:
                return rows
            sheet = names[0]
        root = ElementTree.fromstring(z.read(sheet))
        for row in root.iter(f'{{{_NS}}}row'):
            cells, max_col = {}, -1
            for c in row.findall(f'{{{_NS}}}c'):
                col = _cell_col(c.get('r'))
                if col is None:
                    continue
                t = c.get('t')
                v = c.find(f'{{{_NS}}}v')
                is_el = c.find(f'{{{_NS}}}is')
                if t == 's' and v is not None:
                    val = shared[int(v.text)]
                elif v is not None:
                    val = v.text
                elif is_el is not None:
                    val = ''.join(x.text or '' for x in is_el.iter(f'{{{_NS}}}t'))
                else:
                    val = ''
                cells[col] = val or ''
                max_col = max(max_col, col)
            if max_col >= 0:
                rows.append([cells.get(i, '') for i in range(max_col + 1)])
    return rows


def _read_csv_rows(path):
    import csv
    rows = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.reader(f):
            rows.append(row)
    return rows


def _parse_tasks_file(path):
    ext = os.path.splitext(path)[1].lower()
    rows = _read_csv_rows(path) if ext == '.csv' else _read_xlsx_rows(path)
    clean = []
    for row in rows:
        cells = [str(c).strip() if c is not None else '' for c in row]
        if any(cells):
            clean.append(cells)
    if not clean:
        return []

    def has_any(cell, keys):
        return any(k in cell.lower() for k in keys)

    header = clean[0]
    col_id = col_instr = None
    # 先识别 ID 列（更具体，避免「任务ID」被当成「任务」描述列）
    for ci, h in enumerate(header):
        hl = h.lower()
        if has_any(hl, ('编号', '序号', 'task_id')):
            col_id = ci
            break
    if col_id is None:
        for ci, h in enumerate(header):
            hl = h.lower()
            if 'id' in hl and not has_any(hl, ('描述', '内容', '指令', '需求')):
                col_id = ci
                break
    # 再识别任务描述列
    for ci, h in enumerate(header):
        if ci == col_id:
            continue
        if has_any(hl := h.lower(), ('描述', '内容', '指令', '需求', 'instruction', '任务', 'task')):
            col_instr = ci
            break
    has_header = col_id is not None or col_instr is not None
    data = clean[1:] if has_header else clean
    if not has_header:
        col_instr = 0
        col_id = 1 if len(clean[0]) > 1 else None
    tasks = []
    for cells in data:
        instr = cells[col_instr].strip() if col_instr is not None and col_instr < len(cells) else ''
        if not instr:
            continue
        task_id = cells[col_id].strip() if col_id is not None and col_id < len(cells) else ''
        tasks.append({'task_id': task_id, 'instruction': instr})
    return tasks


def _build_batch_cmd(cfg, task, result_dir):
    """Retained compatibility helper; batches share the local launch builder."""
    from osworld_agent.local_runtime import build_local_launch
    return build_local_launch({**cfg, 'task': task}, result_dir,
                              python_executable=_runner_python()).command


def _read_run_result(result_dir):
    tj = os.path.join(result_dir, 'trajectory.json')
    if not os.path.isfile(tj):
        return {'status': 'no_trajectory'}
    try:
        with open(tj, encoding='utf-8') as f:
            traj = json.load(f)
    except Exception:
        return {'status': 'parse_error'}
    o = traj.get('outcome') or {}
    m = traj.get('run_metrics') or {}
    return {'status': 'ok', 'success': o.get('success'), 'score': o.get('score'),
            'steps': o.get('num_steps'), 'termination_reason': o.get('termination_reason'),
            'final_answer': (o.get('final_answer') or '')[:500],
            'input_tokens': m.get('input_tokens'), 'output_tokens': m.get('output_tokens'),
            'elapsed_s': m.get('elapsed_s')}


def _run_batch_worker(cfg, tasks, desktop_lease=None):
    from osworld_agent.workspace_config import allocate_local_result_dir
    report_path = os.path.join(LOCAL_RUNS_DIR, 'batch_report_' + time.strftime('%Y%m%d_%H%M%S') + '.csv')
    results = []
    proc = None
    try:
        with open(report_path, 'w', encoding='utf-8-sig', newline='') as rf:
            import csv
            writer = csv.writer(rf)
            writer.writerow(['序号', '任务ID', '任务描述', '成功', '得分', '步数', '终止原因', '输入token', '输出token', '耗时秒', '最终答案'])
            for idx, t in enumerate(tasks):
                if _batch_state['stop']:
                    _batch_state['error'] = '已停止'
                    break
                _batch_state['current'] = t.get('task_id') or t['instruction'][:40]
                result_dir = allocate_local_result_dir(LOCAL_RUNS_DIR)
                proc = None
                try:
                    from osworld_agent.local_runtime import build_local_launch
                    launch = build_local_launch({**cfg, 'task': t['instruction']}, result_dir,
                                                python_executable=_runner_python())
                    proc = subprocess.Popen(launch.command, cwd=launch.cwd, env=launch.env,
                                            start_new_session=os.name != 'nt',
                                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                    if desktop_lease is not None:
                        desktop_lease.set_child(proc.pid)
                    while proc.poll() is None:
                        if _batch_state['stop']:
                            _stop_process_tree(proc)
                            break
                        time.sleep(1.0)
                    proc.wait()
                    r = _read_run_result(result_dir)
                    r['error'] = ''
                except Exception as exc:
                    if proc is not None and proc.poll() is None:
                        try:
                            _stop_process_tree(proc)
                        except Exception:
                            # The finally block preserves its owner marker.
                            _batch_state['stop'] = True
                    r = {'status': 'spawn_error', 'error': str(exc)[:300], 'success': None,
                         'score': None, 'steps': None, 'termination_reason': 'spawn_error',
                         'final_answer': '', 'input_tokens': None, 'output_tokens': None, 'elapsed_s': None}
                row = {'idx': idx + 1, 'task_id': t.get('task_id', ''), 'instruction': t['instruction'], **r}
                results.append(row)
                writer.writerow([row['idx'], row['task_id'], row['instruction'], row.get('success'),
                                 row.get('score'), row.get('steps'), row.get('termination_reason'),
                                 row.get('input_tokens'), row.get('output_tokens'), row.get('elapsed_s'),
                                 row.get('final_answer', '')])
                rf.flush()
                _batch_state['done'] = idx + 1
                _batch_state['results'] = results
    finally:
        _batch_state['running'] = False
        _batch_state['report_path'] = report_path
        _release_runner_lease(desktop_lease, proc)


@app.post("/run/batch")
async def start_batch(request: Request):
    import base64
    import tempfile
    if _batch_state['running']:
        return {"ok": False, "error": "批量运行进行中，请先停止"}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    filename = body.get('filename') or ''
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {'.xlsx', '.xls', '.csv'}:
        return {"ok": False, "error": "仅支持 .xlsx / .xls / .csv 文件"}
    content_b64 = body.get('content_b64') or ''
    if not content_b64:
        return {"ok": False, "error": "未收到文件内容"}
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        try:
            tmp.write(base64.b64decode(content_b64))
        except Exception:
            return {"ok": False, "error": "文件内容不是有效的 base64"}
        tmp.close()
        tasks = _parse_tasks_file(tmp.name)
    except Exception as exc:
        return {"ok": False, "error": "解析表格失败：" + str(exc)[:200]}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if not tasks:
        return {"ok": False, "error": "表格里没有读到任务（请确认第 1 列是任务描述）"}
    cfg = body.get('config') or {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        desktop_lease = _desktop_lease('studio-batch-' + uuid.uuid4().hex)
    except (RuntimeError, OSError) as exc:
        return {'ok': False, 'error': str(exc)}
    _batch_state.update(running=True, total=len(tasks), done=0, current='',
                        results=[], error='', stop=False, report_path='', started_at=time.time())
    try:
        threading.Thread(target=_run_batch_worker, args=(cfg, tasks, desktop_lease), daemon=True).start()
    except Exception:
        _batch_state['running'] = False
        desktop_lease.close()
        raise
    return {"ok": True, "total": len(tasks)}


@app.get("/run/batch/status")
async def batch_status():
    return {"ok": True, **_batch_state}


@app.post("/run/batch/stop")
async def stop_batch():
    _batch_state['stop'] = True
    return {"ok": True}


@app.post("/run/start")
async def start_run(request: Request):
    _sync_local_state()
    if collection.active:
        return {"ok": False, "error": "人工采集进行中，请先结束采集。"}
    if _agent_state["status"] in {"starting", "running", "cancelling"}:
        return {"ok": False, "error": "已有任务在运行中，请先停止"}
    try:
        cfg = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    from osworld_agent.prompt_settings import validate_mode_prompts
    try:
        cfg['mode_prompts'] = validate_mode_prompts(cfg.get('mode_prompts'))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if cfg.get("mode") == "local":
        return _start_local(cfg, request.url.port)

    domain = cfg.get("domain", "all")
    task_id = cfg.get("task_id")
    if not task_id:
        return {"ok": False, "error": "请选择一个具体任务 task_id"}
    if not EXAMPLES_DIR:
        return {"ok": False, "error": "未配置 OSWORLD_EXAMPLES_DIR（evaluation_examples/examples）"}
    if not DESKTOP_ENV_PATH:
        return {"ok": False, "error": "未配置 OSWORLD_DESKTOP_ENV_PATH（包含 desktop_env 的目录）"}

    model = cfg.get("model", "Qwen3.6-27B")
    model_url = cfg.get("model_url", "http://10.44.101.62:8094/v1")
    model_api_key = cfg.get("model_api_key") or "EMPTY"
    ground_model = cfg.get("ground_model", "UI-Venus-2-9B")
    ground_url = cfg.get("ground_url", "http://7.246.80.189:8001/v1")
    ground_api_key = cfg.get("ground_api_key", "EMPTY")
    ground_type = cfg.get("ground_type", "ui-venus2")
    max_steps = int(cfg.get("max_steps", 40))

    cmd = [
        _runner_python(), "-m", "osworld_agent.viz.runner",
        "--task-id", task_id,
        "--examples-dir", EXAMPLES_DIR,
        "--domain", domain,
        "--model", model,
        "--model-url", model_url,
        "--model-api-key", model_api_key,
        "--ground-model", ground_model,
        "--ground-url", ground_url,
        "--ground-api-key", ground_api_key,
        "--ground-type", ground_type,
        "--max-steps", str(max_steps),
        "--desktop-env-path", DESKTOP_ENV_PATH,
        "--cache-dir", os.path.join(DESKTOP_ENV_PATH, "cache"),
    ]
    try:
        cmd += _wait_cli_args(cfg)
        from osworld_agent.prompt_settings import prompt_cli_args
        cmd += prompt_cli_args(cfg.get('mode_prompts'), Path(LOCAL_RUNS_DIR) / '.prompt_configs')
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    vm_path = VM_PATH or _detect_vm_path()
    if vm_path:
        cmd += ["--vm-path", vm_path]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["AGENTS_VIZ_SERVER_URL"] = f"http://127.0.0.1:{request.url.port or os.getenv('AGENTS_VIZ_PORT', '8088')}"
    env["AGENTS_RESULTS_DIR"] = RESULTS_DIR
    env["NO_PROXY"] = "192.168.0.0/16,10.0.0.0/8,7.0.0.0/8,localhost,127.0.0.1"
    env["no_proxy"] = env["NO_PROXY"]
    # 本地 proxy 任务的上游代理：用 HIS 活 IP，否则 _proxy_setup 起不来 tinyproxy
    env["OSWORLD_UPSTREAM_PROXY"] = os.environ.get("OSWORLD_UPSTREAM_PROXY") or "http://172.18.94.109:8080"

    try:
        desktop_lease = _desktop_lease('studio-vm-' + uuid.uuid4().hex)
    except (RuntimeError, OSError) as exc:
        return {'ok': False, 'error': str(exc)}
    _agent_state['run_id'] = None
    _agent_state["task_name"] = f"{domain}/{task_id}"
    _agent_state["task_total"] = 1
    _agent_state["config"] = cfg
    _agent_state["output"] = []
    _agent_state["last_error"] = ""
    _agent_state["current_step"] = 0
    _agent_state["max_steps"] = max_steps
    _agent_state["start_time"] = time.time()
    _agent_state["started_monotonic"] = time.perf_counter()
    _agent_state["elapsed_final"] = None
    _agent_state["status"] = "starting"
    logs.clear()
    proc = None
    try:
        proc = subprocess.Popen(cmd, cwd=AGENT_ROOT, env=env, start_new_session=os.name != 'nt',
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        _agent_state["proc"] = proc
        desktop_lease.set_child(proc.pid)
        _agent_state["status"] = "running"
        threading.Thread(target=_reader_thread, args=(proc, None, _agent_state["started_monotonic"], desktop_lease), daemon=True).start()
        return {"ok": True, "task": _agent_state["task_name"], "task_total": 1}
    except Exception as e:
        if proc is not None and proc.poll() is None:
            try:
                _stop_process_tree(proc)
            except Exception:
                pass
        _release_runner_lease(desktop_lease, proc)
        _agent_state["status"] = "failed"
        return {"ok": False, "error": str(e)}


@app.get("/run/status")
async def run_status():
    snapshot = _sync_local_state()
    elapsed = _agent_state.get("elapsed_final")
    if elapsed is None:
        elapsed = time.perf_counter() - _agent_state["started_monotonic"] if _agent_state.get("started_monotonic") else 0
    return {
        "status": _agent_state["status"],
        "task": _agent_state["task_name"],
        "current_step": _agent_state["current_step"],
        "max_steps": _agent_state["max_steps"],
        "elapsed": round(elapsed, 1),
        "pid": (snapshot.get('pid') if snapshot else
                _agent_state["proc"].pid if _agent_state["proc"] else None),
        "error": _agent_state["last_error"],
        "run_id": _agent_state.get("run_id"),
        "success": snapshot.get("success") if snapshot else None,
        "termination_reason": snapshot.get("termination_reason") if snapshot else None,
    }


def _stop_process_tree(proc):
    """Stop a Studio-owned runner before its shared desktop lease is released."""
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=10)


@app.post("/run/stop")
async def stop_run():
    run_id = _agent_state.get('run_id')
    if run_id:
        snapshot = _get_local_service().cancel_task(run_id)
        _sync_local_state()
        return {'ok': True, 'run_id': run_id, 'status': snapshot['status']}
    proc = _agent_state["proc"]
    if proc and proc.poll() is None:
        _stop_process_tree(proc)
    _agent_state["status"] = "cancelled"
    _agent_state["proc"] = None
    _agent_state["task_name"] = None
    return {"ok": True}


@app.get("/run/output")
async def run_output():
    _sync_local_state()
    return {"output": _agent_state["output"][-200:]}


@app.get("/model-presets")
async def model_presets():
    """返回模型预设（决策模型 + grounding 模型），供前端启动界面下拉选择。"""
    import yaml
    presets_path = _PKG / "config" / "model_presets.yaml"
    try:
        with open(presets_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {
            "decision_models": data.get("decision_models") or [],
            "grounding_models": data.get("grounding_models") or [],
        }
    except Exception as exc:  # noqa: BLE001
        return {"decision_models": [], "grounding_models": [], "error": str(exc)}


@app.post("/run/manual-eval")
async def manual_eval():
    return {"ok": False, "error": "新框架暂不支持手动模式"}


# ================= 实验/调试 stub（防止前端报错）================= #
@app.get("/experiments/default-prompt")
async def get_default_prompt(backend: str = "native", browser_only: bool = False, os_name: str = ''):
    try:
        from osworld_agent.prompts import build_mode_system_prompts, build_editable_mode_prompts
        os_name = os_name or ('windows' if sys.platform == 'win32' else 'mac' if sys.platform == 'darwin' else 'linux')
        if os_name not in {'windows', 'mac', 'linux'}:
            return {'ok': False, 'error': 'Unsupported operating system'}
        templates = build_mode_system_prompts(os_name=os_name, browser_only=browser_only)
        return {"ok": True, "backend": "native",
                "os_name": os_name, "prompt": templates['dom'], "prompts": templates,
                "editable_prompts": build_editable_mode_prompts(os_name=os_name, browser_only=browser_only)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/experiments/memory-strategies")
async def get_memory_strategies():
    return {"strategies": []}


@app.get("/experiments/vision-strategies")
async def get_vision_strategies():
    return {"strategies": []}


@app.get("/experiments/prompts")
async def get_prompt_presets():
    return {"presets": []}


@app.get("/experiments/prompts/{name}")
async def get_prompt_preset(name: str):
    return Response(status_code=404)


@app.post("/experiments/prompts")
async def post_prompt_preset(request: Request):
    return {"ok": False, "error": "新框架未接入提示词预设"}


@app.get("/experiments/token-count")
async def get_token_count():
    return {"ok": False, "error": "新框架未接入 tokenizer 服务"}


@app.get("/debug/snapshots")
async def list_debug_snapshots(call_type: str = ""):
    return {"snapshots": []}


@app.get("/debug/snapshots/{snapshot_id}")
async def get_debug_snapshot(snapshot_id: str):
    return Response(status_code=404)


@app.get("/debug/images/{filename}")
async def get_debug_image(filename: str):
    return Response(status_code=404)


@app.post("/debug/replay")
async def replay_debug_snapshot(request: Request):
    return {"ok": False, "error": "新框架未接入模型调试重放"}


def _extract_grounding_coords(content):
    """解析 grounding 响应，返回 (coords, kind) 或 None。

    coords 为 [0,1000] 归一化坐标；kind 为 'point' 或 'bbox'。
    支持 [x,y]、[x1,y1,x2,y2]、{"bbox_2d":[...]}、{"point":[...]}、{"x":..,"y":..}。
    """
    if not content:
        return None
    text = str(content).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            if "bbox_2d" in obj and isinstance(obj["bbox_2d"], (list, tuple)) and len(obj["bbox_2d"]) >= 4:
                b = obj["bbox_2d"]
                return [float(b[0]), float(b[1]), float(b[2]), float(b[3])], "bbox"
            if "point" in obj and isinstance(obj["point"], (list, tuple)) and len(obj["point"]) >= 2:
                return [float(obj["point"][0]), float(obj["point"][1])], "point"
            if "x" in obj and "y" in obj:
                return [float(obj["x"]), float(obj["y"])], "point"
    except Exception:
        pass
    import re
    if re.search(r"[\[\(]\s*-1\s*,\s*-1\s*[\]\)]", text):
        return None
    m4 = re.search(r"[\[\(]\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*[\]\)]", text)
    if m4:
        x1, y1, x2, y2 = map(int, m4.groups())
        return [x1, y1, x2, y2], "bbox"
    m2 = re.search(r"[\[\(]\s*(-?\d+)\s*,\s*(-?\d+)\s*[\]\)]", text)
    if m2:
        x, y = map(int, m2.groups())
        if x < 0 or y < 0:
            return None
        return [x, y], "point"
    return None


def _first_image_url(messages):
    """从 messages 里找第一张图片的 data URL（base64）。"""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "image_url":
                    u = (c.get("image_url") or {}).get("url", "")
                    if isinstance(u, str) and u.startswith("data:image"):
                        return u
        elif isinstance(content, str) and content.startswith("data:image"):
            return content
    return None


def _make_annotation_image(messages, coords, kind, is_pixel=False):
    """在原图上画标注（point=绿圈，bbox=红框+中心点），返回 data URL。

    is_pixel: True 表示 coords 已是像素坐标（如 UI-TARS），False 表示 [0,1000] 归一化（如 UI-Venus-2）。
    """
    url = _first_image_url(messages)
    if not url:
        return None
    try:
        import base64
        from io import BytesIO
        from PIL import Image, ImageDraw
        b64 = url.split(",", 1)[1]
        img = Image.open(BytesIO(base64.b64decode(b64))).convert("RGB")
        w, h = img.size
        draw = ImageDraw.Draw(img)

        def to_px(cx, cy):
            if is_pixel:
                return cx, cy
            return cx / 1000 * w, cy / 1000 * h

        if kind == "bbox":
            x1, y1 = to_px(coords[0], coords[1])
            x2, y2 = to_px(coords[2], coords[3])
            draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=4)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            draw.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], outline=(0, 255, 0), width=4)
        else:
            cx, cy = to_px(coords[0], coords[1])
            r = max(6, min(w, h) // 60)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 255, 0), width=4)
            draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(0, 255, 0))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


@app.post("/debug/chat")
async def debug_chat(request: Request):
    """会话调试：代理前端请求到任意 OpenAI 兼容模型端点（决策 / grounding 都走这里）。

    前端 payload: {engine:{engine_type,model,base_url}, messages, generation, api_key}
    返回: {"ok": True, "response": ...} 或 {"ok": False, "error": ...}
    """
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "请求体不是合法 JSON"}

    started = time.monotonic()
    result = await asyncio.to_thread(_debug_chat_request, payload)
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def _debug_chat_request(payload):
    """Run blocking model transport outside the UI event loop."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "请求体必须是 JSON 对象"}
    engine = payload.get("engine") or {}
    engine_type = str(engine.get("engine_type") or "vllm").lower()
    model = str(engine.get("model") or "").strip()
    base_url = str(engine.get("base_url") or "").strip()
    messages = payload.get("messages") or []
    generation = payload.get("generation") or {}
    from osworld_agent.config import resolve_secret
    try:
        api_key = resolve_secret(payload.get("api_key") or "EMPTY")
    except (RuntimeError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}

    if not model:
        return {"ok": False, "error": "缺少模型名"}
    if not base_url:
        return {"ok": False, "error": "缺少模型 URL"}

    # 规范化 messages：content 保持原样（字符串或 OpenAI vision 多部分列表都兼容）
    normalized = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role not in ("system", "user", "assistant"):
            role = "user"
        normalized.append({"role": role, "content": m.get("content", "")})
    if not normalized:
        return {"ok": False, "error": "messages 为空"}

    temperature = float(generation.get("temperature", 0.0))
    max_tokens = int(generation.get("max_new_tokens", 4096))

    if engine_type in ("vllm", "openai", "open_router"):
        import requests as req
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "model": model,
            "messages": normalized,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # 关闭 Qwen 思考模式，让 grounding 直接输出 [x,y] 坐标而非冗长推理
            "chat_template_kwargs": {"enable_thinking": False},
        }
        last_err = None
        for attempt in range(2):
            try:
                # Empty strings explicitly disable inherited ALL_PROXY as well.
                # Public DashScope can reuse this deployment's configured egress;
                # private model endpoints remain direct unless explicitly configured.
                from urllib.parse import urlsplit
                proxy = os.getenv("AGENT_DEBUG_HTTP_PROXY", "")
                host = urlsplit(base_url).hostname or ""
                if not proxy and (host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com")):
                    proxy = os.getenv("AGENT_KNOWLEDGE_HTTP_PROXY", "")
                r = req.post(url, json=body, headers=headers, timeout=300,
                             proxies={"http": proxy, "https": proxy, "all": ""})
                if r.status_code != 200:
                    return {"ok": False, "error": f"模型返回 HTTP {r.status_code}: {r.text[:300]}"}
                data = r.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                # grounding 响应：解析坐标并在原图上画标注，返回可视化图
                parsed = _extract_grounding_coords(content)
                annotation = None
                if parsed and payload.get("call_type") in (None, "grounding"):
                    # UI-TARS 返回像素坐标，UI-Venus-2 返回 [0,1000] 归一化
                    is_pixel = "tars" in model.lower() or "uitars" in model.lower()
                    annotation = _make_annotation_image(normalized, parsed[0], parsed[1], is_pixel=is_pixel)
                return {"ok": True, "response": content, "annotation_image": annotation,
                        "usage": data.get("usage") or {}, "model": data.get("model") or model}
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                # 若端点不支持 chat_template_kwargs，去掉后重试一次
                if "chat_template_kwargs" in body:
                    body.pop("chat_template_kwargs", None)
                    continue
                break
        return {"ok": False, "error": f"调用失败: {last_err}"}

    return {"ok": False, "error": f"暂不支持的 provider: {engine_type}（当前支持 vllm / openai / open_router）"}


def _keep_model_warm():
    """可选的自托管模型保活；API 决策模型仅在显式给出 URL 时才保活。"""
    import requests as _req
    from osworld_agent.prompts import build_system_prompt
    sys_prompt = build_system_prompt(browser_only=True)
    dec_url = os.getenv("OSWORLD_DECISION_URL", "")
    dec_model = os.getenv("OSWORLD_DECISION_MODEL", "Qwen3.6-27B")
    grd_url = os.getenv("OSWORLD_GROUNDING_URL", "http://7.246.80.189:8001/v1/chat/completions")
    while True:
        if dec_url:
            try:
                _req.post(dec_url, json={"model": dec_model,
                                         "messages": [{"role": "system", "content": sys_prompt},
                                                      {"role": "user", "content": "keepalive"}],
                                         "max_tokens": 1}, timeout=120)
            except Exception:  # noqa: BLE001
                pass
        try:
            _req.post(grd_url, json={"model": "UI-Venus-2-9B",
                                     "messages": [{"role": "user", "content": "keepalive"}],
                                     "max_tokens": 1}, timeout=60)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


if __name__ == "__main__":
    port = int(os.getenv("AGENTS_VIZ_PORT", "8088"))
    if os.getenv("OSWORLD_KEEP_MODEL_WARM") == "1":
        threading.Thread(target=_keep_model_warm, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
