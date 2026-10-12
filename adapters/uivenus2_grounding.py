"""真实 UI-Venus-2 grounding：把「点搜索框」这种描述定位成屏幕坐标。

对齐 UI-Venus-2 的 grounding 协议（区别于 UI-TARS）：
- prompt: "Output the center point of the position corresponding to the following instruction: ..."
- 图片在前、文本在后（OpenAI vision 多部分格式）
- 模型输出 [x, y] 归一化坐标（[0, 1000] 空间），不可行时输出 [-1, -1]
- 换算：x/1000*width, y/1000*height（归一化到 [0,1] 再乘屏幕分辨率）
- 关闭 thinking（chat_template_kwargs.enable_thinking=False）
"""
from __future__ import annotations

import re
import time
from typing import Optional

from ..model.grounding import Grounding, GroundingResult
from ..model.request_deadline import (ModelRequestTimeout, call_with_deadline,
                                     make_request_deadline, remaining_seconds, sleep_before_retry)


# UI-Venus-2 grounding prompt。
# 注意：官方 prompt 带 "if infeasible output [-1,-1]" 会因决策模型生成的 target 描述
# 与实际屏幕有细微偏差而大量拒答。这里去掉拒答引导、改为强制给坐标，
# 让模型在描述不完全匹配时也给出"尽力估计"的坐标（对 OSWorld 任务更有利）。
_DEFAULT_USER_PROMPT = (
    "Output the center point of the position corresponding to the following instruction: \n"
    "{instruction}. \n\n"
    "The output should just be the coordinates of a point, in the format [x,y]. "
    "Always output your best estimate of the coordinates."
)


def _extract_coords(text: str):
    """解析 UI-Venus-2 响应，返回 (x_norm, y_norm) 或 None（拒答/失败）。

    支持 [x,y]、[x1,y1,x2,y2]（取中心）、[-1,-1]（拒答）。
    """
    text = (text or "").strip()
    # [-1,-1] 拒答
    if re.search(r"\[\s*-1\s*,\s*-1\s*\]", text):
        return None
    # [x1,y1,x2,y2] 四数取中心
    m = re.search(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        if not all(0 <= value <= 1000 for value in (x1, y1, x2, y2)):
            return None
        return (x1 + x2) / 2, (y1 + y2) / 2
    # [x, y] 两数
    m = re.search(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)
    if m:
        x, y = map(int, m.groups())
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            return None
        return float(x), float(y)
    return None


class UIVenus2Grounding(Grounding):
    """UI-Venus-2 定位客户端（OpenAI 兼容端点）。"""

    def __init__(self, url: str, api_key: str, model: str,
                 width: int = 1920, height: int = 1080,
                 timeout: float = 60.0, retries: int = 0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.width = width
        self.height = height
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.request_deadline: Optional[float] = None
        self.last_call = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            from openai import OpenAI
            # trust_env=False：直连内网 endpoint，不走系统代理
            self._client = OpenAI(
                base_url=self.url, api_key=self.api_key,
                max_retries=0,
                http_client=httpx.Client(trust_env=False, timeout=self.timeout),
            )
        return self._client

    def locate(self, screenshot, target: str):
        from ..model.decision_model import encode_image
        from openai import APITimeoutError
        import httpx
        started = time.perf_counter()
        deadline = make_request_deadline(self.timeout, self.request_deadline)
        self.last_call = {"model": self.model, "target": target, "attempts": [], "confidence": 0.0}
        url = encode_image(screenshot)
        if not url:
            self.last_call["error"] = "截图无法编码"
            return GroundingResult(x=-1, y=-1, confidence=0.0)

        text = _DEFAULT_USER_PROMPT.replace("{instruction}", target.rstrip("."))
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": text},
        ]}]
        self.last_call["messages"] = messages

        last_err = None
        for attempt in range(self.retries + 1):
            attempt_started = time.perf_counter()
            try:
                client = self._get_client()
                remaining = remaining_seconds(deadline)
                resp = call_with_deadline(lambda: client.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=256, temperature=0.0,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}}, timeout=remaining,
                ), remaining)
                out = (resp.choices[0].message.content or "").strip()
                self.last_call["response"] = out
                coords = _extract_coords(out)
                latency = round((time.perf_counter() - attempt_started) * 1000, 2)
                self.last_call["attempts"].append({"status": "ok" if coords is not None else "format_error",
                                                   "response": out, "latency_ms": latency})
                if coords is not None:
                    x = min(self.width - 1, round(coords[0] / 1000.0 * self.width))
                    y = min(self.height - 1, round(coords[1] / 1000.0 * self.height))
                    self.last_call.update({"confidence": 1.0, "x": x, "y": y,
                                           "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
                    self.last_call.pop("error", None)
                    return GroundingResult(x=x, y=y, confidence=1.0)
                last_err = f"定位协议错误：响应无有效 [0,1000] 坐标或拒答: {out[:100]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                self.last_call["error"] = last_err
                self.last_call["attempts"].append({
                    "status": "timeout" if isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)) else "error",
                    "error_type": type(exc).__name__, "error": last_err,
                    "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 2)})
                self.last_call["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                if isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException)):
                    if isinstance(exc, ModelRequestTimeout):
                        raise
                    raise ModelRequestTimeout(f"定位模型请求超时({self.model}): {exc}") from exc
            if attempt < self.retries:
                sleep_before_retry(1.0 * (attempt + 1), deadline)

        self.last_call.update({"error": last_err,
                               "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
        print(f"[grounding 失败] target={target!r}: {last_err}")
        return GroundingResult(x=-1, y=-1, confidence=0.0)
