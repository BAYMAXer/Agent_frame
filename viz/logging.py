"""可视化 logging：包装决策模型 / grounding，把每次模型调用推给 viz 服务器。

思路（对齐原 Agent-S3 的 patch.py，但用包装器而非 monkey-patch）：
- LoggingChatModel 包装 ChatModel，chat() 前后把 (prompt + 截图 + response) POST 到 /log；
- LoggingGrounding 包装 grounding，locate() 前后把 (target + 截图 + 坐标) POST 到 /log；
- pending 与 response 带同一个 id（log_id），前端据此把「加载中」卡片替换成实际结果；
- 全部 try/except 兜底，viz 服务不可用时不影响主流程。
"""
from __future__ import annotations

import os
import queue
import threading
import time

import requests

SERVER_URL = os.getenv("AGENTS_VIZ_SERVER_URL", "http://127.0.0.1:8088")
_session = None
_step = [0]                  # 当前步（每次决策调用递增）
_next_log_id = [0]           # 全局 log id 计数器（pending→response 匹配）
_results_dir = None          # 历史截图落盘目录（runner 设置）
_results_prefix = ''         # 同一天平铺保存多次任务，避免覆盖
_decisions = []              # 采集决策调用：{step, response, screenshot_file, elapsed}
_groundings = []             # 采集 grounding 调用：{step, query, coord, elapsed}
_log_queue = queue.Queue(maxsize=64)
_log_worker = None
_log_worker_lock = threading.Lock()
_dropped_logs = 0


def get_collected() -> dict:
    """返回本次运行采集到的决策/grounding 调用（供 runner 写历史轨迹）。"""
    return {"decisions": list(_decisions), "groundings": list(_groundings)}


def reset_collected() -> None:
    _decisions.clear()
    _groundings.clear()


def set_results_dir(path: str, prefix: str = '') -> None:
    global _results_dir, _results_prefix
    _results_dir = path
    _results_prefix = prefix + '_' if prefix else ''


def _log_worker_main(pending) -> None:
    """One bounded background transport; network delays never hold the agent."""
    global _session
    while True:
        payload = pending.get()
        try:
            if _session is None:
                _session = requests.Session()
                _session.trust_env = False
            _session.post(f"{SERVER_URL}/log", json=payload, timeout=(0.3, 0.7))
        except Exception:
            pass
        finally:
            pending.task_done()


def _post(payload: dict) -> None:
    global _log_worker, _dropped_logs
    if _log_worker is None or not _log_worker.is_alive():
        with _log_worker_lock:
            if _log_worker is None or not _log_worker.is_alive():
                _log_worker = threading.Thread(target=_log_worker_main, args=(_log_queue,),
                                               name="agent-viz-logs", daemon=True)
                _log_worker.start()
    try:
        _log_queue.put_nowait(dict(payload))
    except queue.Full:
        _dropped_logs += 1


def flush_logs(timeout: float = 1.0) -> bool:
    """Wait at most timeout seconds; return whether all queued sends were attempted."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _log_queue.all_tasks_done:
        while _log_queue.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _log_queue.all_tasks_done.wait(remaining)
    return True


def log_status(status: dict) -> None:
    _post({"kind": "status", **status, "ts": time.time()})


def _new_log_id() -> int:
    _next_log_id[0] += 1
    return _next_log_id[0]


def _extract_last_user(messages) -> tuple:
    """从 messages 提取最后一条 user 的【主 prompt 文本】+ 最后一张截图 base64。

    注意：build_vision_message 的 content 是 [主文本, 图片标签, 图, 图片标签, 图, ...]，
    主 prompt 是第一个文本块，后面的「【...】」是图片标签，不应覆盖主文本。
    """
    text, screenshot = "", None
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for c in content:
                    t = c.get("type")
                    if t == "text":
                        if not text:  # 取第一个文本块（主 prompt），后面的图片标签不取
                            text = c.get("text", "")
                    elif t == "image_url":
                        url = c.get("image_url", {}).get("url", "")
                        if "base64," in url:
                            screenshot = url.split("base64,", 1)[-1]
            else:
                text = str(content)
            if text:
                break
    return text, screenshot


def _classify_mtype(text: str, screenshot, messages=None) -> str:
    """给一次模型调用分类（供前端展示）。区分决策/格式重试/完成验证/反思/真子Agent，
    避免把「无截图的辅助调用」统统标成 code_agent（子Agent）。"""
    t = text or ""
    if any(m.get('role') == 'user' and '你是任务规划器' in str(m.get('content', ''))
           for m in (messages or [])):
        return 'planner'
    if any(m.get('role') == 'user' and '## 当前观察' in str(m.get('content', ''))
           for m in (messages or [])) and not screenshot:
        return 'format_retry'
    if "独立步骤核查" in t or "任务完成度核对" in t:
        return "verify"
    if screenshot:
        return "decision"
    if "你是任务规划器" in t:
        return "planner"
    if "## 当前观察" in t and "## 任务" in t:
        return "decision"
    if "不符合要求" in t or "漏了 next_action" in t or "重新只输出一个合法" in t:
        return "format_retry"
    if "任务完成度核对" in t:
        return "verify"
    if "回顾你上一步的操作" in t:
        return "reflect"
    return "code_agent"


class LoggingChatModel:
    """包装 ChatModel，记录每次决策/子Agent 调用。"""

    def __init__(self, inner):
        self.inner = inner
        self.vision = getattr(inner, "vision", True)

    @property
    def last_call(self):
        """转发 inner 的 last_call（决策耗时 latency_ms 等元数据，供轨迹 decision_calls 记录）。

        之前这里漏了转发，导致 decide() 里 getattr(self.model, "last_call", {})
        拿到空 dict，decision_calls 丢失 latency_ms / usage 等，本地运行历史拿不到决策耗时。
        """
        return getattr(self.inner, "last_call", {})

    def chat(self, messages, retries: int = 2, json_mode: bool = False) -> str:
        text, screenshot = _extract_last_user(messages)
        mtype = _classify_mtype(text, screenshot, messages)
        # 主决策的第一步（system+user 两条消息且带截图）才递增步号并发 step 事件；
        # 重试（消息数 >2）、子 Agent（无截图）、Finish 验证等不算新步。
        is_new_step = (mtype == "decision" and len(messages) == 2)
        if is_new_step:
            _step[0] += 1
            log_status({"event": "step", "step": _step[0]})
        step = _step[0]
        log_id = _new_log_id()
        _post({"kind": "model", "type": mtype, "prompt": text, "screenshot": screenshot,
               "pending": True, "step": step, "id": log_id, "ts": time.time()})
        start = time.time()
        try:
            response = self.inner.chat(messages, retries, json_mode=json_mode)
        except Exception as exc:  # noqa: BLE001
            _post({"kind": "model", "type": mtype, "prompt": text, "screenshot": screenshot,
                   "response": str(exc)[:300], "step": step, "id": log_id, "error": True,
                   "elapsed": round(time.time() - start, 3), "ts": time.time()})
            raise
        _post({"kind": "model", "type": mtype, "prompt": text, "screenshot": screenshot,
               "response": response, "step": step, "id": log_id,
               "elapsed": round(time.time() - start, 3), "ts": time.time()})
        if mtype == "decision":
            _decisions.append({
                "step": step, "response": response,
                "screenshot_file": f"{_results_prefix}step_{step}.png",
                "elapsed": round(time.time() - start, 3),
            })
        if screenshot and _results_dir and mtype == "decision":
            self._save_shot(screenshot, step)
        return response

    def build_vision_message(self, text: str, images=None) -> dict:
        return self.inner.build_vision_message(text, images)

    def chat_stream(self, messages, on_delta, timeout=None):
        # Reporter events are emitted by the pipeline, not counted as actions.
        return self.inner.chat_stream(messages, on_delta=on_delta, timeout=timeout)

    @staticmethod
    def _save_shot(screenshot_b64: str, step: int) -> None:
        try:
            import base64
            path = os.path.join(_results_dir, f"{_results_prefix}step_{step}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(screenshot_b64))
            if step == 1:  # 第一次决策的截图即初始截图，供历史"step_0.png"
                with open(os.path.join(_results_dir, f"{_results_prefix}step_0.png"), "wb") as f:
                    f.write(base64.b64decode(screenshot_b64))
        except Exception:
            pass


class LoggingGrounding:
    """包装 grounding，记录每次定位调用。"""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        """Expose endpoint capabilities without copying mutable dimensions."""
        return getattr(self.inner, name)

    def locate(self, screenshot, target: str):
        from ..model.decision_model import encode_image
        url = encode_image(screenshot)
        b64 = url.split("base64,", 1)[-1] if "base64," in url else None
        log_id = _new_log_id()
        _post({"kind": "model", "type": "grounding", "prompt": target, "screenshot": b64,
               "pending": True, "step": _step[0], "id": log_id, "ts": time.time()})
        start = time.time()
        gr = self.inner.locate(screenshot, target)
        response = f"{gr.x}, {gr.y}"
        _post({"kind": "model", "type": "grounding", "prompt": target, "screenshot": b64,
               "response": response, "step": _step[0], "id": log_id,
               "elapsed": round(time.time() - start, 3), "ts": time.time()})
        _groundings.append({"step": _step[0], "query": target,
                            "coord": [gr.x, gr.y], "elapsed": round(time.time() - start, 3)})
        return gr
