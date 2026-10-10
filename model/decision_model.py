"""决策模型层：可插拔注册表 + OpenAI 兼容客户端（支持视觉）。

换决策模型只改两处之一：
1. 改 config.yaml 里的 model.name（对应 MODEL_REGISTRY 的键）；
2. 或在 MODEL_REGISTRY 里新增一项。

所有模型都走 OpenAI 兼容 /chat/completions，所以 vLLM / OpenAI / Claude(兼容层) 都能接。
vision 开启时（默认），每步把当前截图以 OpenAI vision 格式（image_url）塞进用户消息。
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import ModelConfig


@dataclass
class ChatMessage:
    role: str  # system / user / assistant
    content: str


def _guess_mime(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "image/png"


def encode_image(screenshot) -> str:
    """把截图转成 base64 data URL（OpenAI vision 的 image_url 用）。

    支持 PIL.Image / 文件路径 / bytes / 已是 data-url 的字符串。
    无法识别时返回空字符串，调用方会回退到纯文本。
    """
    import base64
    from io import BytesIO

    if screenshot is None:
        return ""
    if isinstance(screenshot, str) and screenshot.startswith("data:"):
        return screenshot
    if isinstance(screenshot, bytes):
        return "data:image/png;base64," + base64.b64encode(screenshot).decode()
    if isinstance(screenshot, str):
        try:
            with open(screenshot, "rb") as f:
                data = f.read()
        except OSError:
            return ""
        return f"data:{_guess_mime(screenshot)};base64," + base64.b64encode(data).decode()
    # PIL.Image 或任何有 save 的对象
    try:
        buf = BytesIO()
        img = screenshot.convert("RGB") if hasattr(screenshot, "convert") else screenshot
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return ""


class ChatModel:
    """OpenAI 兼容的决策模型客户端（带超时与重试，支持视觉）。"""

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.vision = bool(getattr(cfg, "vision", True))
        self.disable_thinking = bool(getattr(cfg, "disable_thinking", True))
        self.last_call: Dict[str, Any] = {}   # 最近一次调用的完整元数据（供轨迹记录 decision_calls）

    def chat(self, messages, retries: int = 2, json_mode: bool = False) -> str:
        """发送一轮对话，返回模型文本；失败自动重试。

        messages 既可以是 ChatMessage 列表，也可以是 {"role","content"} dict 列表，
        content 可以是字符串（纯文本）或 list（OpenAI vision 多部分），内部统一规范化。

        json_mode=True 时加 response_format=json_object，用 vLLM 约束解码强制输出合法
        JSON 对象（减少非 JSON、格式漂移）；这是「约束前移」，仍保留调用方的执行前校验。
        """
        from openai import OpenAI  # 延迟导入，避免非模型场景也依赖 openai
        import httpx

        # trust_env=False：直连内网 endpoint，不走系统代理（同 grounding，避免 HIS 代理拦掉 7.x）
        client = OpenAI(
            base_url=self.cfg.url, api_key=self.cfg.api_key,
            http_client=httpx.Client(trust_env=False, timeout=self.cfg.timeout),
        )
        normalized = [self._to_message_dict(m) for m in messages]
        self.last_call = {"messages": copy.deepcopy(normalized),
                          "model": resolve_model_name(self.cfg.name), "attempts": []}
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                create_kwargs: Dict[str, Any] = dict(
                    model=resolve_model_name(self.cfg.name),
                    messages=normalized,
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                )
                # 关闭 Qwen 思考模式：避免模型生成大段 chain-of-thought 陷入循环、不收敛到答案
                if self.disable_thinking:
                    create_kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                if json_mode:
                    create_kwargs["response_format"] = {"type": "json_object"}
                generation = {k: v for k, v in create_kwargs.items() if k not in ("model", "messages")}
                self.last_call["generation"] = copy.deepcopy(generation)
                resp = client.chat.completions.create(**create_kwargs)
                msg = resp.choices[0].message
                content = getattr(msg, "content", "") or ""
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                self.last_call.update({
                    "response_id": getattr(resp, "id", None),
                    "response_model": getattr(resp, "model", None),
                    "response": content,
                    "generation": generation,
                    "finish_reason": getattr(resp.choices[0], "finish_reason", None),
                    "usage": resp.usage.model_dump() if getattr(resp, "usage", None) else None,
                    "latency_ms": latency_ms,
                })
                self.last_call["attempts"].append({"status": "ok", "latency_ms": latency_ms,
                                                   "generation": copy.deepcopy(generation),
                                                   "response": content})
                return content.strip()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self.last_call["attempts"].append({
                    "status": "error", "error_type": type(exc).__name__,
                    "generation": copy.deepcopy(self.last_call.get("generation", {})),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
                # 若因 extra_body 不被支持而报错，去掉 extra_body 重试一次
                if self.disable_thinking and "extra_body" in str(exc).lower():
                    self.disable_thinking = False
                # 若 json_mode 不被支持，降级为无约束重试
                if json_mode and ("response_format" in str(exc).lower() or "json" in str(exc).lower()):
                    json_mode = False
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"模型调用失败({self.cfg.name}): {last_err}")

    def build_vision_message(self, text: str, images=None) -> Dict[str, Any]:
        """构造一条用户消息；vision 开启时按顺序附上多张图（OpenAI vision 格式）。

        images: [(label, screenshot), ...]，label 会作为图片前的文字说明（如 "S(t-1)"）。
        """
        if self.vision and images:
            content: Any = [{"type": "text", "text": text}]
            for label, shot in images:
                url = encode_image(shot)
                if not url:
                    continue
                if label:
                    content.append({"type": "text", "text": label})
                content.append({"type": "image_url", "image_url": {"url": url}})
            if len(content) > 1:
                return {"role": "user", "content": content}
        return {"role": "user", "content": text}

    @staticmethod
    def _to_message_dict(m) -> Dict[str, Any]:
        """把 ChatMessage 或 dict 统一成 {"role","content"}（content 可为 str 或 list）。"""
        if isinstance(m, dict):
            return {"role": m.get("role", "user"), "content": m.get("content", "")}
        return {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}


@dataclass
class _ModelEntry:
    """注册表里的一项：真实模型名 + 能力说明。"""
    name: str
    description: str


# 决策模型注册表：key 是给人看的别名，换模型改 config 里的 name 即可。
MODEL_REGISTRY: Dict[str, _ModelEntry] = {
    "9b": _ModelEntry("Qwen3.5-9B", "9B 决策模型（vLLM @ 7.246.80.237:9028）"),
    "27b": _ModelEntry("Qwen3.6-27B", "27B 决策模型（需要 api_key）"),
    # 下面是要接外部 API 时再加的占位示例：
    # "gpt-4o": _ModelEntry("gpt-4o", "OpenAI GPT-4o"),
    # "claude": _ModelEntry("claude-3-7-sonnet", "Anthropic Claude"),
}


def resolve_model_name(alias: str) -> str:
    """把别名转成真实模型名。"""
    entry = MODEL_REGISTRY.get(alias)
    return entry.name if entry else alias


def build_decision_model(cfg: ModelConfig) -> ChatModel:
    """根据配置构造决策模型客户端。"""
    return ChatModel(cfg)
