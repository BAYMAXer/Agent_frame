"""真实 UI-TARS grounding：把「点搜索框」这种描述定位成屏幕坐标。

对齐原 Agent-S3 的 grounding 协议：
- prompt: "Query:{target}\\nOutput only the coordinate of one point in your response.\\n"
- 图片在前、文本在后（OpenAI vision 多部分格式）
- 只解析坐标对或明确标注的 point 坐标，不从解释文字中猜数字
- 从 grounding 模型的输入分辨率缩放到实际屏幕分辨率
"""
from __future__ import annotations

import re
import json
import math
import time
from typing import Optional

from ..model.grounding import Grounding, GroundingResult
from ..model.request_deadline import (ModelRequestTimeout, call_with_deadline,
                                     make_request_deadline, remaining_seconds, sleep_before_retry)


_NUMBER = r"-?\d+(?:\.\d+)?"
_PAIR = rf"(?:\[\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\]|\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\))"


def _extract_coords(text: str):
    """Accept a coordinate pair or an explicit point, preserving negative values."""
    text = (text or "").strip()
    fenced = re.fullmatch(r"```(?:json|text|python)?\s*\n?([\s\S]*?)\n?```", text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    pair = re.fullmatch(_PAIR, text)
    if pair:
        values = [value for value in pair.groups() if value is not None]
        return tuple(map(float, values))
    bare_pair = re.fullmatch(rf"({_NUMBER})\s*,\s*({_NUMBER})", text)
    if bare_pair:
        return tuple(map(float, bare_pair.groups()))
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        obj = None
    if isinstance(obj, dict):
        values = obj.get("point")
        if values is None and set(obj) == {"x", "y"}:
            values = [obj["x"], obj["y"]]
        if (isinstance(values, (list, tuple)) and len(values) == 2
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)):
            return tuple(map(float, values))
    # A labelled point may accompany prose. Multiple labelled points are ambiguous.
    labelled = list(re.finditer(rf"\b(?:point|coordinates?)\s*[:=]\s*{_PAIR}", text, re.I))
    tags = list(re.finditer(rf"<point>\s*({_NUMBER})\s*[, ]\s*({_NUMBER})\s*</point>", text, re.I))
    if len(labelled) + len(tags) != 1:
        return None
    match = (labelled or tags)[0]
    values = [value for value in match.groups() if value is not None]
    return tuple(map(float, values))


class RealUItarsGrounding(Grounding):
    """真实 UI-TARS 定位客户端（OpenAI 兼容端点）。"""

    def __init__(self, url: str, api_key: str, model: str,
                 width: int = 1920, height: int = 1080,
                 grounding_width: Optional[int] = None,
                 grounding_height: Optional[int] = None,
                 timeout: float = 60.0, retries: int = 0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.width = width
        self.height = height
        # grounding 模型的输入分辨率；默认等于屏幕分辨率（不做缩放），
        # 若你的 UI-TARS 端点用了别的分辨率（如 1000x1000），在这里对齐。
        # None means the current screenshot dimensions; Pipeline updates width/height
        # when switching between full desktop and browser viewport screenshots.
        self.grounding_width = grounding_width
        self.grounding_height = grounding_height
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.request_deadline: Optional[float] = None
        self.last_call = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            from openai import OpenAI
            # trust_env=False：直连内网 endpoint，不走系统代理（否则 HIS 代理会拦掉 7.x，
            # 导致 grounding 抛异常、fallback 成屏幕中心）
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
        self.last_call = {"model": self.model, "target": target, "messages": [], "response": "",
                          "error": None, "attempts": [], "latency_ms": 0.0,
                          "confidence": 0.0, "x": -1, "y": -1}
        deadline = make_request_deadline(self.timeout, self.request_deadline)
        url = encode_image(screenshot)
        if not url:
            return self._failed_result("截图无法编码", started)
        source_width = self.width if self.grounding_width is None else self.grounding_width
        source_height = self.height if self.grounding_height is None else self.grounding_height
        if not all(math.isfinite(float(value)) and value > 0
                   for value in (self.width, self.height, source_width, source_height)):
            return self._failed_result("截图或参考坐标尺寸无效", started)

        text = f"Query:{target}\nOutput only the coordinate of one point in your response.\n"
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
                    model=self.model, messages=messages, max_tokens=128, timeout=remaining), remaining)
                out = (resp.choices[0].message.content or "").strip()
                self.last_call["response"] = out
                coords = _extract_coords(out)
                valid = (coords is not None and all(math.isfinite(value) for value in coords)
                         and 0 <= coords[0] < source_width and 0 <= coords[1] < source_height)
                self.last_call["attempts"].append({
                    "status": "ok" if valid else "format_error", "response": out,
                    "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 2)})
                if valid:
                    x = min(self.width - 1, round(coords[0] * self.width / source_width))
                    y = min(self.height - 1, round(coords[1] * self.height / source_height))
                    self.last_call.update({"confidence": 1.0, "x": x, "y": y, "error": None,
                                           "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
                    return GroundingResult(x=x, y=y, confidence=1.0)
                # Protocol mismatches will not improve by repeating the same request.
                return self._failed_result(f"定位协议错误：响应无有效坐标或坐标越界: {out[:100]}", started)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                is_timeout = isinstance(exc, (TimeoutError, APITimeoutError, httpx.TimeoutException))
                self.last_call["attempts"].append({
                    "status": "timeout" if is_timeout else "error", "error_type": type(exc).__name__,
                    "error": last_err, "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 2)})
                self.last_call.update({"error": last_err,
                                       "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
                if is_timeout:
                    if isinstance(exc, ModelRequestTimeout):
                        raise
                    raise ModelRequestTimeout(f"定位模型请求超时({self.model}): {exc}") from exc
            if attempt < self.retries:
                try:
                    sleep_before_retry(1.0 * (attempt + 1), deadline)
                except ModelRequestTimeout as exc:
                    self.last_call.update({"error": str(exc),
                                           "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
                    raise

        return self._failed_result(last_err or "定位模型调用失败", started)

    def _failed_result(self, error: str, started: float) -> GroundingResult:
        self.last_call.update({"error": error, "confidence": 0.0, "x": -1, "y": -1,
                               "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
        return GroundingResult(x=-1, y=-1, confidence=0.0)
