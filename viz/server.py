"""本地 Windows 工作台：自由文本任务、实时模型调用与本机历史轨迹。

启动：python launch.py。所有任务通过本机 runner 执行。
"""
from __future__ import annotations

import json
import os
import base64
import hashlib
import math
from collections import OrderedDict
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from .logging import compact_messages

app = FastAPI(title="osworld_agent Viz")

# ---- 配置（环境变量覆盖）----
_PKG = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PKG.parent
AGENT_ROOT = os.getenv("OSWORLD_AGENT_ROOT", str(_PROJECT_ROOT))
LOCAL_RUNS_DIR = os.getenv("OSWORLD_LOCAL_RUNS_DIR", str(_PKG / "local_runs"))
STATIC_DIR = str(Path(__file__).resolve().parent / "static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def _runner_python() -> str:
    """使用显式指定的本机解释器，默认与工作台保持一致。"""
    return os.getenv("OSWORLD_RUNNER_PYTHON") or sys.executable


MAX_LOGS = 200
logs = []
clients = []
MAX_LIVE_SHOTS = 40
MAX_LIVE_SHOT_BYTES = 32 * 1024 * 1024
_live_shots = OrderedDict()
_live_shot_bytes = 0


def _cache_live_screenshot(encoded):
    """Content-address recent screenshot bytes and keep memory use bounded."""
    global _live_shot_bytes
    if not isinstance(encoded, str) or not encoded:
        return None
    if encoded.startswith("data:image/") and ";base64," in encoded:
        encoded = encoded.split(",", 1)[1]
    if len(encoded) > (MAX_LIVE_SHOT_BYTES * 4 // 3 + 4):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if not data or len(data) > MAX_LIVE_SHOT_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    token = hashlib.sha256(data).hexdigest()
    if token in _live_shots:
        _live_shots.move_to_end(token)
        return "/live/shot/" + token
    _live_shots[token] = (data, mime)
    _live_shot_bytes += len(data)
    while len(_live_shots) > MAX_LIVE_SHOTS or _live_shot_bytes > MAX_LIVE_SHOT_BYTES:
        _, (old_data, _) = _live_shots.popitem(last=False)
        _live_shot_bytes -= len(old_data)
    return "/live/shot/" + token


def _clear_live_screenshots():
    global _live_shot_bytes
    _live_shots.clear()
    _live_shot_bytes = 0

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
    "plan": None, "current_plan_step_id": None, "plan_revision": None,
    "plan_update": None, "task_start": None, "browser_observation": None,
}


def _clear_plan_state():
    """A new task must not recover a previous task's plan after reconnecting."""
    _agent_state.update(plan=None, current_plan_step_id=None, plan_revision=None,
                        plan_update=None, task_start=None, browser_observation=None)


def _plan_revision(payload):
    plan = payload.get("plan")
    revision = payload.get("revision", plan.get("revision") if isinstance(plan, dict) else None)
    try:
        return int(revision) if revision is not None else None
    except (TypeError, ValueError):
        return None


def _recover_local_window_presentation(proc, config):
    """The runner can be force-killed; recover only its recorded task window."""
    cfg = config or {}
    if proc is None or cfg.get("mode") != "local" or not cfg.get("result_dir"):
        return True
    try:
        from ..adapters.windows_window import recover_window_presentation
        recovered = recover_window_presentation(
            Path(cfg["result_dir"]) / "window_presentation.json", expected_runner_pid=proc.pid)
    except Exception:
        recovered = False
    if not recovered:
        _agent_state.setdefault("output", []).append(
            "[window] 任务已退出，但窗口置顶状态未能恢复；已保留恢复记录")
    return recovered


def _reader_thread(proc, config=None):
    cfg = dict(config if config is not None else (_agent_state.get("config") or {}))
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
        _recover_local_window_presentation(proc, cfg)
        if _agent_state.get("proc") is proc and _agent_state["status"] == "running":
            _agent_state["status"] = "done" if rc == 0 else "failed"


# ================= 实时日志 ================= #
@app.post("/log")
async def receive_log(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "log payload must be an object"}
    return await _publish_log(payload)


async def _publish_log(payload: dict):
    """Publish runner and server lifecycle events through the same live history."""
    payload.setdefault("ts", time.time())
    if isinstance(payload.get("messages"), list):
        payload["messages"] = compact_messages(payload["messages"])
    screenshot_url = _cache_live_screenshot(payload.get("screenshot"))
    if screenshot_url:
        payload["screenshot_url"] = screenshot_url
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
            _clear_plan_state()
            _agent_state["task_start"] = stored
        elif ev == "plan_update" and isinstance(payload.get("plan"), (dict, list)):
            revision = _plan_revision(payload)
            previous = _agent_state.get("plan_revision")
            if previous is None or revision is None or revision >= previous:
                plan = payload["plan"]
                _agent_state["plan"] = plan
                _agent_state["current_plan_step_id"] = payload.get(
                    "current_plan_step_id", plan.get("current_plan_step_id", plan.get("current_step_id")) if isinstance(plan, dict) else None)
                _agent_state["plan_revision"] = revision
                _agent_state["plan_update"] = stored
        if (payload.get("prompt_metadata") or {}).get("browser_selection") is not None:
            _agent_state["browser_observation"] = stored
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
    # MAX_LOGS can evict task_start/plan_update during a long run. Keep the current
    # snapshot available without growing the log buffer indefinitely.
    return {"logs": logs, "task_start": _agent_state.get("task_start"),
            "plan_update": _agent_state.get("plan_update"),
            "browser_observation": _agent_state.get("browser_observation")}


@app.get("/live/shot/{token}")
async def live_shot(token: str):
    shot = _live_shots.get(token)
    if shot is None:
        return Response(status_code=404)
    data, mime = shot
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "no-store"})


@app.get("/capabilities")
async def capabilities():
    return {
        "run_modes": ["local"],
        "local": {"task_text": True, "timeouts": ["model_timeout", "ground_timeout", "step_timeout"],
                  "execution_modes": ["browser_first", "desktop"],
                  "browser_context": ["ax_max_chars", "screenshot_max_edge"]},
        "model_io": {"messages": True, "image_metadata": True, "usage": True,
                     "grounding_raw_response": True},
        "live_screenshots": {"max_count": MAX_LIVE_SHOTS, "max_bytes": MAX_LIVE_SHOT_BYTES,
                             "volatile": True},
        "features": {"experiments": False, "manual_eval": False, "debug_replay": False,
                     "debug_chat": True},
    }


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
            "prompt_metadata": s.get("prompt_metadata") or {},
            "observation": {"info": (s.get("observation") or {}).get("info") or {}},
            "plan_step_id": s.get("plan_step_id"),
            "plan_snapshot": s.get("plan_snapshot"),
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
        lines[-1].update({key: pending.get(key) for key in
                         ("prompt_metadata", "plan_step_id", "timing_ms")})
        lines[-1]["observation"] = {"info": (pending.get("observation") or {}).get("info") or {}}
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
    execution_mode = cfg.get("execution_mode", os.getenv("OSWORLD_LOCAL_EXECUTION_MODE", "browser_first"))
    if not isinstance(execution_mode, str) or execution_mode not in {"browser_first", "desktop"}:
        return {"ok": False, "error": "执行模式必须是 browser_first 或 desktop"}
    from osworld_agent.config import ContextConfig
    try:
        max_steps = cfg.get("max_steps", 20)
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("最大步数必须为正整数")
        browser_context = ContextConfig(ax_max_chars=cfg.get("ax_max_chars", 5000),
                                        screenshot_max_edge=cfg.get("screenshot_max_edge", 1280))
        timeouts = {name: cfg.get(name, float(os.getenv(env_name, default)))
                    for name, env_name, default in
                    (("model_timeout", "OSWORLD_LOCAL_MODEL_TIMEOUT", "60"),
                     ("ground_timeout", "OSWORLD_LOCAL_GROUND_TIMEOUT", "60"),
                     ("step_timeout", "OSWORLD_LOCAL_STEP_TIMEOUT", "120"))}
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or value <= 0 or not math.isfinite(value) for value in timeouts.values()):
            raise ValueError("模型、定位及每步超时必须为有限正数")
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    result_dir = cfg.get("result_dir") or os.path.join(
        LOCAL_RUNS_DIR, "ui_" + time.strftime("%Y%m%d_%H%M%S"))
    cfg = {**cfg, "result_dir": result_dir, "execution_mode": execution_mode,
           "max_steps": max_steps, **timeouts, "ax_max_chars": browser_context.ax_max_chars,
           "screenshot_max_edge": browser_context.screenshot_max_edge}

    cmd = [
        _runner_python(), str(_PKG / "launch.py"), "--runner", "local",
        "--task", task,
        "--model", model,
        "--model-url", model_url,
        "--ground-model", ground_model,
        "--ground-url", ground_url,
        "--ground-type", ground_type,
        "--model-timeout", str(timeouts["model_timeout"]),
        "--ground-timeout", str(timeouts["ground_timeout"]),
        "--step-timeout", str(timeouts["step_timeout"]),
        "--execution-mode", execution_mode,
        "--ax-max-chars", str(browser_context.ax_max_chars),
        "--screenshot-max-edge", str(browser_context.screenshot_max_edge),
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
    _clear_plan_state()
    logs.clear()
    _clear_live_screenshots()
    try:
        proc = subprocess.Popen(cmd, cwd=AGENT_ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        _agent_state["proc"] = proc
        _agent_state["status"] = "running"
        threading.Thread(target=_reader_thread, args=(proc, dict(cfg)), daemon=True).start()
        return {"ok": True, "task": _agent_state["task_name"], "task_total": 1}
    except Exception as e:
        _agent_state["status"] = "failed"
        return {"ok": False, "error": str(e)}


@app.post("/run/start")
async def start_run(request: Request):
    try:
        cfg = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}
    if not isinstance(cfg, dict):
        return {"ok": False, "error": "任务配置必须是 JSON 对象"}
    mode = cfg.get("mode", "local")
    if mode != "local":
        return {"ok": False, "error": "仅支持本地 Windows 任务，mode 必须为 local"}
    if _agent_state["status"] in {"running", "starting"}:
        return {"ok": False, "error": "已有任务在运行中，请先停止"}
    return _start_local({**cfg, "mode": "local"})


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
        "plan": _agent_state.get("plan"),
        "current_plan_step_id": _agent_state.get("current_plan_step_id"),
        "plan_revision": _agent_state.get("plan_revision"),
        "plan_update": _agent_state.get("plan_update"),
        "task_start": _agent_state.get("task_start"),
        "browser_observation": _agent_state.get("browser_observation"),
    }


@app.post("/run/stop")
async def stop_run():
    proc = _agent_state["proc"]
    cfg = dict(_agent_state.get("config") or {})
    was_running = (_agent_state.get("status") in {"running", "starting"}
                   or (proc is not None and proc.poll() is None))
    stopped_task = _agent_state.get("task_name")
    stopped_step = _agent_state.get("current_step", 0)
    if proc and proc.poll() is None:
        try:
            command = ["taskkill"]
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
    if proc is not None:
        _recover_local_window_presentation(proc, cfg)
    _agent_state["status"] = "idle"
    _agent_state["proc"] = None
    _agent_state["task_name"] = None
    if was_running:
        await _publish_log({"kind": "status", "event": "interrupted", "status": "interrupted",
                            "step": stopped_step, "task": stopped_task,
                            "error": "运行已手动停止，未完成的调用和动作已中断。"})
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
                             os_name: str = "windows"):
    if os_name != "windows":
        return {"ok": False, "error": "当前框架仅支持本地 Windows"}
    try:
        from osworld_agent.prompts import build_system_prompt, build_subtask_system_prompt
        return {"ok": True, "backend": "native",
                "prompt": (build_subtask_system_prompt(os_name=os_name, mode="browser") if browser_only
                           else build_system_prompt(os_name=os_name))}
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
