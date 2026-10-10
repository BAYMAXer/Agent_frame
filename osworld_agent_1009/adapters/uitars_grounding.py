"""真实 UI-TARS grounding：把「点搜索框」这种描述定位成屏幕坐标。

对齐原 Agent-S3 的 grounding 协议：
- prompt: "Query:{target}\\nOutput only the coordinate of one point in your response.\\n"
- 图片在前、文本在后（OpenAI vision 多部分格式）
- 响应里取前两个整数作为 (x, y)
- 从 grounding 模型的输入分辨率缩放到实际屏幕分辨率
"""
from __future__ import annotations

import re
from typing import Optional

from ..model.grounding import Grounding, GroundingResult


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
        # grounding 模型的输入分辨率；默认等于屏幕分辨率（不做缩放），
        # 若你的 UI-TARS 端点用了别的分辨率（如 1000x1000），在这里对齐。
        self.grounding_width = grounding_width or width
        self.grounding_height = grounding_height or height
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            from openai import OpenAI
            # trust_env=False：直连内网 endpoint，不走系统代理（否则 HIS 代理会拦掉 7.x，
            # 导致 grounding 抛异常、fallback 成屏幕中心）
            self._client = OpenAI(
                base_url=self.url, api_key=self.api_key,
                http_client=httpx.Client(trust_env=False, timeout=self.timeout),
            )
        return self._client

    def locate(self, screenshot, target: str):
        from ..model.decision_model import encode_image
        from ..model.grounding import GroundingResult
        url = encode_image(screenshot)
        if not url:
            # 截图编码失败：显式失败，不兜底
            return GroundingResult(x=-1, y=-1, confidence=0.0)

        text = f"Query:{target}\nOutput only the coordinate of one point in your response.\n"
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": text},
        ]}]

        last_err = None
        for attempt in range(3):
            try:
                resp = self._get_client().chat.completions.create(
                    model=self.model, messages=messages, max_tokens=128)
                out = (resp.choices[0].message.content or "").strip()
                nums = re.findall(r"\d+", out)
                if len(nums) >= 2:
                    x = round(int(nums[0]) * self.width / self.grounding_width)
                    y = round(int(nums[1]) * self.height / self.grounding_height)
                    return GroundingResult(x=x, y=y, confidence=1.0)
                last_err = f"响应无坐标: {out[:100]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                if attempt < 2:
                    import time
                    time.sleep(1.0 * (attempt + 1))

        # 三次都失败：显式失败，confidence=0，不再兜底成屏幕中心
        print(f"[grounding 失败] target={target!r}: {last_err}")
        return GroundingResult(x=-1, y=-1, confidence=0.0)
