"""viz 服务器：前端启动 / 实时看过程 / 看历史。

适配新框架（osworld_agent）：
- 核心端点 /log /history /ws /run/* /runs /tasks 保留，契约与原 Agent-S3 viz 一致；
- /experiments/* 和 /debug/* 是原框架的进阶功能，这里做最小 stub，避免前端报错；
- /run/start 启动的是新框架的 osworld_agent.viz.runner（跑 VMware 真实任务）。

运行：python -m osworld_agent.viz.server
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
import uvicorn

app = FastAPI(title="osworld_agent Viz")

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
}


def _reader_thread(proc):
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
        if _agent_state["status"] == "running":
            _agent_state["status"] = "done" if rc == 0 else "failed"


# ================= 实时日志 ================= #
@app.post("/log")
async def receive_log(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    payload.setdefault("ts", time.time())
    stored = dict(payload)
    stored.pop("screenshot", None)  # 存日志去掉截图 base64，省内存
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
async def history():
    return {"logs": logs}


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


# ================= 本地运行（Windows 本机任务，local_runs/ 目录）================= #
@app.get("/local/runs")
async def local_runs():
    """列出 local_runs/ 下的本地运行（读 trajectory.json）。"""
    runs = []
    base = Path(LOCAL_RUNS_DIR)
    if base.is_dir():
        entries = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for d in entries:
            if not d.is_dir():
                continue
            traj_path = d / "trajectory.json"
            if not traj_path.exists():
                continue
            instruction, score, steps, success = "", None, 0, None
            status, phase, pending_step = "", "", None
            try:
                with open(traj_path, encoding="utf-8") as f:
                    traj = json.load(f)
                instruction = (traj.get("task") or {}).get("instruction", "")
                outcome = traj.get("outcome") or {}
                score = outcome.get("score")
                success = outcome.get("success")
                status = outcome.get("status", "")
                phase = outcome.get("phase", "")
                pending_step = traj.get("pending_step")
                steps = len(traj.get("steps") or [])
            except Exception:  # noqa: BLE001
                pass
            shots = len([f for f in os.listdir(d)
                         if f.startswith("step_") and f.endswith(".png")])
            runs.append({"id": d.name, "instruction": instruction, "score": score,
                         "success": success, "steps": steps, "shots": shots,
                         "status": status, "phase": phase, "pending_step": pending_step,
                         "mtime": d.stat().st_mtime})
    return {"runs": runs}


def _local_traj_to_frontend(traj):
    """把标准 trajectory.json 的 steps 转成前端 traj.jsonl 格式（复用历史视图渲染）。"""
    lines = []
    for s in traj.get("steps") or []:
        action = s.get("action")
        action_str = json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else str(action or "")
        reasoning = s.get("reasoning") or {}
        assessment = s.get("assessment") or {}
        grounding = s.get("grounding") or {}
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
            "screenshot_file": f"step_{s.get('step')}.png",
            "grounding": gs,
            "routing": s.get("routing") or {},
            "outcome": (s.get("execution") or {}).get("outcome", ""),
            "assessment": assessment,
            "prompt": s.get("prompt_text", ""),
            "timing_ms": timing,
            "decision_elapsed_ms": decision_elapsed,
            "subagent": subagent,
        })
    pending = traj.get("pending_step")
    if isinstance(pending, dict):
        step = pending.get("step", 0)
        action = pending.get("action")
        lines.append({"step_num": step, "pending": True,
                      "screenshot_file": f"step_{step}.png" if step else None,
                      "action": json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else "",
                      "plan": pending.get("raw", ""), "routing": pending.get("route") or {},
                      "outcome": "未完成阶段: " + str(pending.get("phase") or pending.get("event") or "")
                                 + " " + str((traj.get("outcome") or {}).get("error") or ""),
                      "done": False})
    return lines


@app.get("/local/traj")
async def local_traj(run: str):
    """返回某个本地运行的轨迹（前端格式 + 原始 trajectory）。"""
    base = Path(LOCAL_RUNS_DIR) / run
    traj_path = base / "trajectory.json"
    if not traj_path.exists():
        return {"ok": False, "error": "轨迹不存在"}
    try:
        with open(traj_path, encoding="utf-8") as f:
            traj = json.load(f)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"轨迹解析失败：{exc}"}
    return {"ok": True, "traj": _local_traj_to_frontend(traj),
            "instruction": (traj.get("task") or {}).get("instruction", ""),
            "plan": traj.get("plan"),
            "trajectory": traj}


@app.get("/local/shot")
async def local_shot(run: str, filename: str):
    """返回某个本地运行的截图（step_N.png）。"""
    filename = os.path.basename(filename)  # 防目录穿越
    if not filename.startswith("step_") or not filename.endswith(".png"):
        return Response(status_code=404)
    shot = (Path(LOCAL_RUNS_DIR) / run / filename).resolve()
    if not str(shot).startswith(str(Path(LOCAL_RUNS_DIR).resolve())):
        return Response(status_code=404)
    if shot.is_file():
        return FileResponse(str(shot))
    return Response(status_code=404)


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
    import requests as req
    try:
        cfg = await request.json()
    except Exception:
        cfg = {}
    model = cfg.get("model", "Qwen3.5-9B")
    url = cfg.get("model_url", "http://7.246.80.237:9028/v1")
    api_key = cfg.get("model_api_key") or "EMPTY"
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
    b64 = base64.b64encode(png).decode()
    payload = {"model": model, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "回复两个字：正常"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
    ], "max_tokens": 32, "temperature": 0.0}
    try:
        r = req.post(f"{url}/chat/completions", json=payload,
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
                     proxies={"http": None, "https": None})
        if r.status_code == 200:
            content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "status": 200, "content": content[:50], "visual_support": "带图请求成功"}
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    except Exception as e:
        return {"ok": False, "status": -1, "error": str(e)[:300]}


def _start_local(cfg: dict):
    """启动 Windows 本机任务（run_windows_local.py），复用 _reader_thread + /log + /ws 实时显示。"""
    task = str(cfg.get("task") or "").strip()
    if not task:
        return {"ok": False, "error": "请输入任务描述"}
    model = cfg.get("model") or os.getenv("OSWORLD_LOCAL_MODEL", "Qwen3.5-9B")
    model_url = cfg.get("model_url") or os.getenv("OSWORLD_LOCAL_MODEL_URL", "http://7.246.80.189:9028/v1")
    ground_model = cfg.get("ground_model") or os.getenv("OSWORLD_LOCAL_GROUND_MODEL", "UI-Venus-2-9B")
    ground_url = cfg.get("ground_url") or os.getenv("OSWORLD_LOCAL_GROUND_URL", "http://7.246.80.189:8001/v1")
    ground_type = cfg.get("ground_type") or os.getenv("OSWORLD_LOCAL_GROUND_TYPE", "ui-venus2")
    max_steps = int(cfg.get("max_steps", 15))
    result_dir = cfg.get("result_dir") or os.path.join(
        LOCAL_RUNS_DIR, "ui_" + time.strftime("%Y%m%d_%H%M%S"))
    cfg = {**cfg, "result_dir": result_dir}

    cmd = [
        _runner_python(), str(_PKG / "run_windows_local.py"),
        "--task", task,
        "--model", model,
        "--model-url", model_url,
        "--ground-model", ground_model,
        "--ground-url", ground_url,
        "--ground-type", ground_type,
        "--model-timeout", str(cfg.get("model_timeout") or os.getenv("OSWORLD_LOCAL_MODEL_TIMEOUT", "60")),
        "--ground-timeout", str(cfg.get("ground_timeout") or os.getenv("OSWORLD_LOCAL_GROUND_TIMEOUT", "60")),
        "--step-timeout", str(cfg.get("step_timeout") or os.getenv("OSWORLD_LOCAL_STEP_TIMEOUT", "120")),
        "--max-steps", str(max_steps),
        "--result-dir", result_dir,
    ]
    env = os.environ.copy()
    if cfg.get("model_api_key"):
        env["OSWORLD_LOCAL_MODEL_API_KEY"] = str(cfg["model_api_key"])
    if cfg.get("ground_api_key"):
        env["OSWORLD_LOCAL_GROUND_API_KEY"] = str(cfg["ground_api_key"])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["AGENTS_VIZ_SERVER_URL"] = f"http://127.0.0.1:{os.getenv('AGENTS_VIZ_PORT', '8088')}"
    env["AGENTS_RESULTS_DIR"] = RESULTS_DIR
    env["NO_PROXY"] = "192.168.0.0/16,10.0.0.0/8,7.0.0.0/8,localhost,127.0.0.1"
    env["no_proxy"] = env["NO_PROXY"]

    _agent_state["task_name"] = task[:60]
    _agent_state["task_total"] = 1
    _agent_state["config"] = cfg
    _agent_state["output"] = []
    _agent_state["current_step"] = 0
    _agent_state["max_steps"] = max_steps
    _agent_state["start_time"] = time.time()
    _agent_state["status"] = "starting"
    logs.clear()
    try:
        proc = subprocess.Popen(cmd, cwd=AGENT_ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        _agent_state["proc"] = proc
        _agent_state["status"] = "running"
        threading.Thread(target=_reader_thread, args=(proc,), daemon=True).start()
        return {"ok": True, "task": _agent_state["task_name"], "task_total": 1}
    except Exception as e:
        _agent_state["status"] = "failed"
        return {"ok": False, "error": str(e)}


@app.post("/run/start")
async def start_run(request: Request):
    if _agent_state["status"] == "running":
        return {"ok": False, "error": "已有任务在运行中，请先停止"}
    try:
        cfg = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    if cfg.get("mode") == "local":
        return _start_local(cfg)

    domain = cfg.get("domain", "all")
    task_id = cfg.get("task_id")
    if not task_id:
        return {"ok": False, "error": "请选择一个具体任务 task_id"}
    if not EXAMPLES_DIR:
        return {"ok": False, "error": "未配置 OSWORLD_EXAMPLES_DIR（evaluation_examples/examples）"}
    if not DESKTOP_ENV_PATH:
        return {"ok": False, "error": "未配置 OSWORLD_DESKTOP_ENV_PATH（包含 desktop_env 的目录）"}

    model = cfg.get("model", "Qwen3.5-9B")
    model_url = cfg.get("model_url", "http://7.246.80.237:9028/v1")
    model_api_key = cfg.get("model_api_key") or "EMPTY"
    ground_model = cfg.get("ground_model", "UI-TARS-1.5-7B")
    ground_url = cfg.get("ground_url", "http://7.246.80.237:49999/v1")
    ground_api_key = cfg.get("ground_api_key", "EMPTY")
    ground_type = cfg.get("ground_type", "ui-tars")
    max_steps = int(cfg.get("max_steps", 20))

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
    vm_path = VM_PATH or _detect_vm_path()
    if vm_path:
        cmd += ["--vm-path", vm_path]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["AGENTS_VIZ_SERVER_URL"] = f"http://127.0.0.1:{os.getenv('AGENTS_VIZ_PORT', '8088')}"
    env["AGENTS_RESULTS_DIR"] = RESULTS_DIR
    env["NO_PROXY"] = "192.168.0.0/16,10.0.0.0/8,7.0.0.0/8,localhost,127.0.0.1"
    env["no_proxy"] = env["NO_PROXY"]
    # 本地 proxy 任务的上游代理：用 HIS 活 IP，否则 _proxy_setup 起不来 tinyproxy
    env["OSWORLD_UPSTREAM_PROXY"] = os.environ.get("OSWORLD_UPSTREAM_PROXY") or "http://172.18.94.109:8080"

    _agent_state["task_name"] = f"{domain}/{task_id}"
    _agent_state["task_total"] = 1
    _agent_state["config"] = cfg
    _agent_state["output"] = []
    _agent_state["current_step"] = 0
    _agent_state["max_steps"] = max_steps
    _agent_state["start_time"] = time.time()
    _agent_state["status"] = "starting"
    logs.clear()
    try:
        proc = subprocess.Popen(cmd, cwd=AGENT_ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        _agent_state["proc"] = proc
        _agent_state["status"] = "running"
        threading.Thread(target=_reader_thread, args=(proc,), daemon=True).start()
        return {"ok": True, "task": _agent_state["task_name"], "task_total": 1}
    except Exception as e:
        _agent_state["status"] = "failed"
        return {"ok": False, "error": str(e)}


@app.get("/run/status")
async def run_status():
    elapsed = time.time() - _agent_state["start_time"] if _agent_state["start_time"] else 0
    return {
        "status": _agent_state["status"],
        "task": _agent_state["task_name"],
        "current_step": _agent_state["current_step"],
        "max_steps": _agent_state["max_steps"],
        "elapsed": round(elapsed, 1),
        "pid": _agent_state["proc"].pid if _agent_state["proc"] else None,
    }


@app.post("/run/stop")
async def stop_run():
    proc = _agent_state["proc"]
    if proc and proc.poll() is None:
        try:
            command = ["taskkill"]
            if (_agent_state.get("config") or {}).get("mode") != "local":
                command.append("/T")
            # 本机任务只停止 runner，保留它按需启动的 Chrome 子进程。
            command.extend(["/F", "/PID", str(proc.pid)])
            subprocess.run(command,
                           capture_output=True, timeout=15, check=True)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception as exc:
                return {"ok": False, "error": f"未能确认任务已停止: {exc}"}
        cfg = _agent_state.get("config") or {}
        if cfg.get("mode") == "local" and cfg.get("result_dir"):
            # /F 不会运行 runner 的 finally，保留已写入的未完成步骤并标记中断。
            base = Path(cfg["result_dir"])
            target = base / "trajectory.json"
            if target.is_file():
                try:
                    trajectory = json.loads(target.read_text(encoding="utf-8"))
                    outcome = trajectory.setdefault("outcome", {})
                    if outcome.get("status") == "running":
                        outcome.update(status="interrupted", error="任务被手动终止")
                        temporary = base / ".stopped_trajectory.json"
                        temporary.write_text(json.dumps(trajectory, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
                        os.replace(temporary, target)
                        with (base / "run_events.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps({"event": "task_end", "status": "interrupted",
                                                     "phase": outcome.get("phase"), "ts": time.time()},
                                                    ensure_ascii=False) + "\n")
                except (OSError, ValueError, TypeError):
                    pass
    _agent_state["status"] = "idle"
    _agent_state["proc"] = None
    _agent_state["task_name"] = None
    return {"ok": True}


@app.get("/run/output")
async def run_output():
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
async def get_default_prompt(backend: str = "native", browser_only: bool = False,
                             os_name: str = "linux"):
    try:
        from osworld_agent.prompts import build_system_prompt
        return {"ok": True, "backend": "native",
                "prompt": build_system_prompt(os_name=os_name, browser_only=browser_only)}
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

    engine = payload.get("engine") or {}
    engine_type = str(engine.get("engine_type") or "vllm").lower()
    model = str(engine.get("model") or "").strip()
    base_url = str(engine.get("base_url") or "").strip()
    messages = payload.get("messages") or []
    generation = payload.get("generation") or {}
    api_key = payload.get("api_key") or "EMPTY"

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
                # proxies=None：直连内网端点，绕过 HIS 代理
                r = req.post(url, json=body, headers=headers, timeout=300,
                             proxies={"http": None, "https": None})
                if r.status_code != 200:
                    return {"ok": False, "error": f"模型返回 HTTP {r.status_code}: {r.text[:300]}"}
                data = r.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                # grounding 响应：解析坐标并在原图上画标注，返回可视化图
                parsed = _extract_grounding_coords(content)
                annotation = None
                if parsed:
                    # UI-TARS 返回像素坐标，UI-Venus-2 返回 [0,1000] 归一化
                    is_pixel = "tars" in model.lower() or "uitars" in model.lower()
                    annotation = _make_annotation_image(normalized, parsed[0], parsed[1], is_pixel=is_pixel)
                return {"ok": True, "response": content, "annotation_image": annotation}
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
    """后台保活线程：用真实 system prompt warm 决策模型的 prefix cache，避免冷启动（78s）。

    决策变慢的根因不是「模型被卸载」，而是 vLLM 第一次处理长 system prompt（+3 张截图）时
    要计算全部 KV cache（prefix cache 冷启动）；纯文本 "ping" 无法 warm 这个 cache。
    所以这里用本机完整 GUI system prompt 作为 system 前缀去 warm，
    之后真实决策的 system 前缀命中 cache，只剩动态部分（任务/截图/AX树），快。
    """
    import requests as _req
    from osworld_agent.prompts import build_system_prompt
    sys_prompt = build_system_prompt(os_name="windows", browser_only=False)
    dec_url = os.getenv("OSWORLD_DECISION_URL", "http://7.246.80.189:9028/v1/chat/completions")
    grd_url = os.getenv("OSWORLD_GROUNDING_URL", "http://7.246.80.189:8001/v1/chat/completions")
    while True:
        try:
            _req.post(dec_url, json={"model": "Qwen3.5-9B",
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
    threading.Thread(target=_keep_model_warm, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
