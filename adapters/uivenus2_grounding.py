"""真实 UI-Venus-2 grounding：把「点搜索框」这种描述定位成屏幕坐标。

对齐 UI-Venus-2 的 grounding 协议（区别于 UI-TARS）：
- prompt: "Output the center point of the position corresponding to the following instruction: ..."
- 图片在前、文本在后（OpenAI vision 多部分格式）
- 模型输出 [x, y] 归一化坐标（[0, 1000] 空间），不可行时输出 [-1, -1]
- 换算：x/1000*width, y/1000*height（归一化到 [0,1] 再乘屏幕分辨率）
- 关闭 thinking（chat_template_kwargs.enable_thinking=False）
"""
from __future__ import annotations

from ..model.grounding_usage import completion
from ..config import resolve_secret

import re
from ..model.grounding import Grounding, GroundingResult, screenshot_size


# UI-Venus-2 grounding prompt。
# 明确允许拒答：不能把不可定位目标变成任意的点击。
_DEFAULT_USER_PROMPT = (
    "Output the center point of the position corresponding to the following instruction: \n"
    "{instruction}. \n\n"
    "The output should just be the coordinates of a point, in the format [x,y]. "
    "If infeasible, output [-1,-1]."
)


def _extract_coords(text: str):
    """解析 UI-Venus-2 响应，返回 (x_norm, y_norm) 或 None（拒答/失败）。

    支持 [x,y]、[x1,y1,x2,y2]（取中心）、[-1,-1]（拒答）。
    """
    text = (text or "").strip()
    # [x1,y1,x2,y2] 四数取中心
    m = re.fullmatch(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        if not (0 <= x1 <= x2 <= 1000 and 0 <= y1 <= y2 <= 1000):
            return None
        return (x1 + x2) / 2, (y1 + y2) / 2
    # [x, y] 两数
    m = re.fullmatch(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)
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
                 timeout: float = 180.0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.width = width
        self.height = height
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx
            from openai import OpenAI
            # trust_env=False：直连内网 endpoint，不走系统代理
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
            return GroundingResult(x=-1, y=-1, confidence=0.0)
        width, height = size

        text = _DEFAULT_USER_PROMPT.replace("{instruction}", target.rstrip("."))
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": text},
        ]}]

        last_err = None
        for attempt in range(3):
            try:
                resp = completion(self,
                    model=self.model, messages=messages,
                    max_tokens=256, temperature=0.0,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                out = (resp.choices[0].message.content or "").strip()
                coords = _extract_coords(out)
                if coords is not None:
                    # Normalized 1000 denotes the image edge, i.e. last pixel.
                    x = min(width - 1, round(coords[0] / 1000.0 * width))
                    y = min(height - 1, round(coords[1] / 1000.0 * height))
                    return GroundingResult(x=x, y=y, confidence=1.0)
                last_err = f"响应无坐标/拒答: {out[:100]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                if attempt < 2:
                    import time
                    time.sleep(1.0 * (attempt + 1))

        print(f"[grounding 失败] target={target!r}: {last_err}")
        return GroundingResult(x=-1, y=-1, confidence=0.0)
