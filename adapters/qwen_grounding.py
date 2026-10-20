"""Qwen Plus visual fallback for targets that have no usable DOM reference."""
from __future__ import annotations

from ..model.grounding_usage import completion

import io
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

from ..config import resolve_secret
from ..model.decision_model import encode_image
from ..model.grounding import Grounding, GroundingResult


class QwenPlusGrounding(Grounding):
    """Locate a target in an image using a labeled grid and the Qwen Plus API.

    A grid is easier for a general vision model to report than unconstrained
    pixel coordinates. Invalid or out-of-range answers fail closed.
    """

    def __init__(self, url: str, api_key: str, model: str = "qwen3.7-plus",
                 width: int = 1920, height: int = 1080, timeout: float = 45.0):
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
            self._client = OpenAI(
                base_url=self.url, api_key=resolve_secret(self.api_key),
                http_client=httpx.Client(trust_env=False, timeout=self.timeout),
            )
        return self._client

    @staticmethod
    def _image(screenshot):
        if isinstance(screenshot, Image.Image):
            return screenshot.convert("RGB")
        if isinstance(screenshot, (str, Path)):
            return Image.open(screenshot).convert("RGB")
        if isinstance(screenshot, bytes):
            return Image.open(io.BytesIO(screenshot)).convert("RGB")
        return None

    @staticmethod
    def _grid(image, cols=12, rows=9):
        marked = image.copy()
        draw = ImageDraw.Draw(marked)
        for col in range(cols):
            x = round(col * image.width / cols)
            draw.line((x, 0, x, image.height), fill="red", width=1)
        for row in range(rows):
            y = round(row * image.height / rows)
            draw.line((0, y, image.width, y), fill="red", width=1)
        for col in range(cols):
            for row in range(rows):
                draw.text((round(col * image.width / cols) + 3,
                           round(row * image.height / rows) + 3),
                          f"{col},{row}", fill="red")
        return marked, cols, rows

    def _cell(self, image, target, *, cols=12, rows=9, detail=False):
        marked, cols, rows = self._grid(image, cols, rows)
        prompt = (
            f"This {'enlarged crop' if detail else 'screenshot'} is {image.width}x{image.height}. "
            f"The red grid has {cols} columns and {rows} rows, labeled column,row at each cell's upper-left. "
            f"Which cell contains the CENTER of this target: {target}? "
            'Return only JSON {"column":number,"row":number}. '
            'If the target is absent, return {"column":-1,"row":-1}. '
            "Do not choose a nearby label or another control."
        )
        response = completion(self,
            model=self.model, messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": encode_image(marked)}},
                {"type": "text", "text": prompt},
            ]}], temperature=0, max_tokens=128,
            response_format={"type": "json_object"},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        answer = json.loads(response.choices[0].message.content or "{}")
        col, row = float(answer.get("column", -1)), float(answer.get("row", -1))
        if not (math.isfinite(col) and math.isfinite(row) and 0 <= col < cols and 0 <= row < rows):
            return None
        return math.floor(col), math.floor(row)

    def locate(self, screenshot, target: str) -> GroundingResult:
        self.last_calls = []
        failed = GroundingResult(x=-1, y=-1, confidence=0.0)
        try:
            image = self._image(screenshot)
            if image is None or not target.strip():
                return failed
            coarse = self._cell(image, target)
            if coarse is None:
                return failed
            col, row = coarse
            x0 = max(0, round((col - 1) * image.width / 12))
            y0 = max(0, round((row - 1) * image.height / 9))
            x1 = min(image.width, round((col + 2) * image.width / 12))
            y1 = min(image.height, round((row + 2) * image.height / 9))
            crop = image.crop((x0, y0, x1, y1))
            scale = min(3, max(1, 1200 // max(crop.width, crop.height)))
            enlarged = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
            fine = self._cell(enlarged, target, cols=12, rows=10, detail=True)
            if fine is None:
                return failed
            x = min(image.width - 1, round(x0 + (fine[0] + .5) * crop.width / 12))
            y = min(image.height - 1, round(y0 + (fine[1] + .5) * crop.height / 10))
            return GroundingResult(x=x, y=y, confidence=1.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[Qwen grounding 失败] {type(exc).__name__}: {str(exc)[:180]}")
            return failed
