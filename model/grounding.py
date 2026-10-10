"""视觉定位模型（grounding）层：把「点搜索框」这种自然语言目标定位成坐标。

决策模型不需要猜像素坐标，坐标由视觉定位模型（UI-TARS）给出。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..config import GroundingConfig


@dataclass
class GroundingResult:
    """一次定位结果：中心坐标 + 置信度（可选）。"""
    x: int
    y: int
    confidence: float = 1.0


class Grounding(ABC):
    """视觉定位接口。"""

    @abstractmethod
    def locate(self, screenshot, target: str) -> GroundingResult:
        """给定截图 + 目标描述，返回要点击的中心坐标。"""
        raise NotImplementedError


class UItarsGrounding(Grounding):
    """UI-TARS 定位客户端。

    走 OpenAI 兼容端点。UI-TARS 的定位模式：输入截图 + 目标描述，
    返回一个边界框，我们取框的中心作为点击坐标。
    """

    def __init__(self, url: str, api_key: str, model: str,
                 width: int = 1920, height: int = 1080, timeout: float = 180.0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.width = width
        self.height = height
        self.timeout = timeout

    def locate(self, screenshot, target: str) -> GroundingResult:
        """定位目标元素。

        TODO(下一步接入)：对齐服务端 49999 的 UI-TARS 具体请求协议
        （截图编码方式、提示词格式、返回的坐标格式）。当前返回屏幕中心作为占位，
        保证整体链路可以先跑通、再替换为真实定位。
        """
        # 占位实现：真实接入后替换为对 self.url 的请求。
        return GroundingResult(x=self.width // 2, y=self.height // 2)


class NullGrounding(Grounding):
    """无定位模型时的降级实现（始终返回屏幕中心），用于离线测试链路。"""

    def __init__(self, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height

    def locate(self, screenshot, target: str) -> GroundingResult:
        return GroundingResult(x=self.width // 2, y=self.height // 2)


def resolve_coords(gr: GroundingResult) -> Optional[Tuple[int, int]]:
    """把定位结果转成坐标；grounding 失败（置信度 0 或负坐标）时返回 None。

    注意：不要在这里把失败结果钳制成 (0,0)——那会让执行器"以为"定位成功而去点
    左上角。失败必须显式返回 None，由执行器拒绝执行。
    """
    if gr is None:
        return None
    if gr.confidence is not None and gr.confidence <= 0:
        return None
    if gr.x < 0 or gr.y < 0:
        return None
    return int(gr.x), int(gr.y)


def mark_coordinate(screenshot, x: int, y: int, radius: int = 14, color: str = "red"):
    """在截图上画一个小圆圈，标记 grounding 定位坐标，返回新图。

    用于视觉记忆：Mark(S(t-1)) 标出上一步实际 grounding 点击位置。
    截图需是 PIL.Image（有 .convert/.copy）；否则原样返回。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return screenshot
    if not hasattr(screenshot, "convert"):
        return screenshot
    img = screenshot.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=color, width=3)
    return img


@dataclass
class _ModelEntry:
    """注册表里的一项：真实模型名 + 能力说明。"""
    name: str
    description: str


# 视觉定位模型注册表
GROUNDING_REGISTRY: Dict[str, _ModelEntry] = {
    "ui_tars_7b": _ModelEntry("UI-TARS-1.5-7B", "UI-TARS 定位模型（vLLM @ 49999）"),
}


def build_grounding_model(cfg: GroundingConfig) -> Grounding:
    """根据配置构造视觉定位客户端。"""
    entry = GROUNDING_REGISTRY.get(cfg.name)
    real_name = entry.name if entry else cfg.name
    return UItarsGrounding(url=cfg.url, api_key=cfg.api_key, model=real_name,
                           width=cfg.width, height=cfg.height, timeout=cfg.timeout)
