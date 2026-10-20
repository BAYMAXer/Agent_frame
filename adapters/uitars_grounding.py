"""真实 UI-TARS grounding：把「点搜索框」这种描述定位成屏幕坐标。

对齐原 Agent-S3 的 grounding 协议：
- prompt: "Query:{target}\\nOutput only the coordinate of one point in your response.\\n"
- 图片在前、文本在后（OpenAI vision 多部分格式）
- 响应必须是单一坐标对，拒绝负数、额外数字和越界值
- 从 grounding 模型的输入分辨率缩放到实际屏幕分辨率
"""
from __future__ import annotations

from ..model.grounding_usage import completion
from ..config import resolve_secret

import re
from typing import Optional

from ..model.grounding import Grounding, GroundingResult, screenshot_size


def _extract_coords(text: str):
    """Accept an unambiguous point, never extract digits out of prose."""
    value = (text or "").strip()
    if len(value) >= 2 and (value[0], value[-1]) in (("[", "]"), ("(", ")")):
        value = value[1:-1].strip()
    match = re.fullmatch(r"(-?\d+)\s*,\s*(-?\d+)", value)
    if match is None:
        return None
    x, y = map(int, match.groups())
    return (x, y) if x >= 0 and y >= 0 else None


class RealUItarsGrounding(Grounding):
    """真实 UI-TARS 定位客户端（OpenAI 兼容端点）。"""

    def __init__(self, url: str, api_key: str, model: str,
                 width: int = 1920, height: int = 1080,
                 grounding_width: Optional[int] = None,
                 grounding_height: Optional[int] = None,
                 timeout: float = 180.0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.width = width
        self.height = height
        # grounding 模型的输出坐标空间；默认等于实际截图（不做缩放），
        # 若你的 UI-TARS 端点用了别的分辨率（如 1000x1000），在这里对齐。
        if (grounding_width is None) != (grounding_height is None):
            raise ValueError("grounding_width and grounding_height must be provided together")
        if grounding_width is not None and (grounding_width <= 0 or grounding_height <= 0):
            raise ValueError("grounding dimensions must be positive")
        self.grounding_width = grounding_width
        self.grounding_height = grounding_height
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            from openai import OpenAI
            # trust_env=False：直连内网 endpoint，不走系统代理（否则 HIS 代理会拦掉 7.x，
            # 导致 grounding 抛异常、fallback 成屏幕中心）
            self._client = OpenAI(
                base_url=self.url, api_key=resolve_secret(self.api_key),
                http_client=httpx.Client(trust_env=False, timeout=self.timeout),
            )
        return self._client

    def locate(self, screenshot, target: str):
        self.last_calls = []
        from ..model.decision_model import encode_image
        import os
        if isinstance(screenshot, os.PathLike):
            screenshot = os.fspath(screenshot)
        size = screenshot_size(screenshot)
        url = encode_image(screenshot)
        if not url or size is None:
            # 截图编码失败：显式失败，不兜底
            return GroundingResult(x=-1, y=-1, confidence=0.0)
        width, height = size
        source_width = self.grounding_width or width
        source_height = self.grounding_height or height

        text = f"Query:{target}\nOutput only the coordinate of one point in your response.\n"
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": text},
        ]}]

        last_err = None
        for attempt in range(3):
            try:
                resp = completion(self,
                    model=self.model, messages=messages, max_tokens=128)
                out = (resp.choices[0].message.content or "").strip()
                point = _extract_coords(out)
                if point is not None and point[0] < source_width and point[1] < source_height:
                    x = min(width - 1, round(point[0] * width / source_width))
                    y = min(height - 1, round(point[1] * height / source_height))
                    return GroundingResult(x=x, y=y, confidence=1.0)
                last_err = f"响应坐标无效/越界: {out[:100]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                if attempt < 2:
                    import time
                    time.sleep(1.0 * (attempt + 1))

        # 三次都失败：显式失败，confidence=0，不再兜底成屏幕中心
        print(f"[grounding 失败] target={target!r}: {last_err}")
        return GroundingResult(x=-1, y=-1, confidence=0.0)
