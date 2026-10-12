"""可视化 logging：包装决策模型 / grounding，把每次模型调用推给 viz 服务器。

思路（对齐原 Agent-S3 的 patch.py，但用包装器而非 monkey-patch）：
- LoggingChatModel 包装 ChatModel，chat() 前后把 (prompt + 截图 + response) POST 到 /log；
- LoggingGrounding 包装 grounding，locate() 前后把 (target + 截图 + 坐标) POST 到 /log；
- pending 与 response 带同一个 id（log_id），前端据此把「加载中」卡片替换成实际结果；
- 全部 try/except 兜底，viz 服务不可用时不影响主流程。
"""
from __future__ import annotations

import os
import time

import requests

SERVER_URL = os.getenv("AGENTS_VIZ_SERVER_URL", "http://127.0.0.1:8088")
_session = None
_step = [0]                  # 当前步（每次决策调用递增）
_next_log_id = [0]           # 全局 log id 计数器（pending→response 匹配）
_results_dir = None          # 历史截图落盘目录（runner 设置）
_decisions = []              # 采集决策调用：{step, response, screenshot_file, elapsed}
_groundings = []             # 采集 grounding 调用：{step, query, coord, elapsed}


def get_collected() -> dict:
    """返回本次运行采集到的决策/grounding 调用（供 runner 写历史轨迹）。"""
    return {"decisions": list(_decisions), "groundings": list(_groundings)}


def reset_collected() -> None:
    _decisions.clear()
    _groundings.clear()


def set_results_dir(path: str) -> None:
    global _results_dir
    _results_dir = path


def _post(payload: dict) -> None:
    global _session
    if _session is None:
        _session = requests.Session()
    try:
        _session.post(f"{SERVER_URL}/log", json=payload, timeout=2)
    except Exception:
        pass


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


def _classify_mtype(text: str, screenshot) -> str:
    """给一次模型调用分类（供前端展示）。区分决策/格式重试/完成验证/反思/真子Agent，
    避免把「无截图的辅助调用」统统标成 code_agent（子Agent）。"""
    if screenshot:
        return "decision"
    t = text or ""
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

    @property
    def request_deadline(self):
        return getattr(self.inner, "request_deadline", None)

    @request_deadline.setter
    def request_deadline(self, value):
        self.inner.request_deadline = value

    def chat(self, messages, retries: int = 0, json_mode: bool = False) -> str:
        text, screenshot = _extract_last_user(messages)
        mtype = _classify_mtype(text, screenshot)
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
                "screenshot_file": f"step_{step}.png",
                "elapsed": round(time.time() - start, 3),
            })
        if screenshot and _results_dir:
            self._save_shot(screenshot, step)
        return response

    def build_vision_message(self, text: str, images=None) -> dict:
        return self.inner.build_vision_message(text, images)

    @staticmethod
    def _save_shot(screenshot_b64: str, step: int) -> None:
        try:
            import base64
            path = os.path.join(_results_dir, f"step_{step}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(screenshot_b64))
            if step == 1:  # 第一次决策的截图即初始截图，供历史"step_0.png"
                with open(os.path.join(_results_dir, "step_0.png"), "wb") as f:
                    f.write(base64.b64decode(screenshot_b64))
        except Exception:
            pass


class LoggingGrounding:
    """包装 grounding，记录每次定位调用。"""

    def __init__(self, inner):
        self.inner = inner

    @property
    def last_call(self):
        return getattr(self.inner, "last_call", {})

    @property
    def request_deadline(self):
        return getattr(self.inner, "request_deadline", None)

    @request_deadline.setter
    def request_deadline(self, value):
        self.inner.request_deadline = value

    @property
    def width(self):
        return self.inner.width

    @width.setter
    def width(self, value):
        self.inner.width = value

    @property
    def height(self):
        return self.inner.height

    @height.setter
    def height(self, value):
        self.inner.height = value

    def locate(self, screenshot, target: str):
        from osworld_agent.model.decision_model import encode_image
        url = encode_image(screenshot)
        b64 = url.split("base64,", 1)[-1] if "base64," in url else None
        log_id = _new_log_id()
        _post({"kind": "model", "type": "grounding", "prompt": target, "screenshot": b64,
               "pending": True, "step": _step[0], "id": log_id, "ts": time.time()})
        start = time.time()
        try:
            gr = self.inner.locate(screenshot, target)
        except Exception as exc:
            _post({"kind": "model", "type": "grounding", "prompt": target,
                   "response": str(exc), "step": _step[0], "id": log_id, "error": True,
                   "elapsed": round(time.time() - start, 3), "ts": time.time()})
            raise
        details = self.last_call if isinstance(self.last_call, dict) else {}
        error = details.get("error")
        response = f"{gr.x}, {gr.y}" + (f"\n定位失败: {error}" if error else "")
        _post({"kind": "model", "type": "grounding", "prompt": target, "screenshot": b64,
               "response": response, "step": _step[0], "id": log_id,
               "elapsed": round(time.time() - start, 3), "ts": time.time(),
               "confidence": gr.confidence, "error": bool(error),
               "raw_response": details.get("response", "")})
        _groundings.append({"step": _step[0], "query": target,
                            "coord": [gr.x, gr.y], "elapsed": round(time.time() - start, 3)})
        return gr
