"""决策模型层：可插拔注册表 + OpenAI 兼容客户端（支持视觉）。

换决策模型只改两处之一：
1. 改 config.yaml 里的 model.name（对应 MODEL_REGISTRY 的键）；
2. 或在 MODEL_REGISTRY 里新增一项。

所有模型都走 OpenAI 兼容 /chat/completions，所以 vLLM / OpenAI / Claude(兼容层) 都能接。
vision 开启时（默认），每步把当前截图以 OpenAI vision 格式（image_url）塞进用户消息。
"""
from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import ModelConfig, resolve_secret


@dataclass
class ChatMessage:
    role: str  # system / user / assistant
    content: str


class ImageEncodingError(ValueError):
    """A required image must not silently become a text-only model request."""


def _field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _guess_mime(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "image/png"


def encode_image(screenshot) -> str:
    """把截图转成 base64 data URL（OpenAI vision 的 image_url 用）。

    支持 PIL.Image / 文件路径 / bytes / 已是 data-url 的字符串。
    无法识别时返回空字符串，调用方必须拒绝缺失的当前截图。
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
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            import httpx
            self._client = OpenAI(
                base_url=self.cfg.url, api_key=resolve_secret(self.cfg.api_key), max_retries=0,
                http_client=httpx.Client(trust_env=False, timeout=self.cfg.timeout))
        return self._client

    def prepare_client(self):
        """Local SDK/client setup; callers join preparation before chat()."""
        self._get_client()

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def chat(self, messages, retries: int = 2, json_mode: bool = False) -> str:
        """发送一轮对话，返回模型文本；失败自动重试。

        messages 既可以是 ChatMessage 列表，也可以是 {"role","content"} dict 列表，
        content 可以是字符串（纯文本）或 list（OpenAI vision 多部分），内部统一规范化。

        json_mode=True 时加 response_format=json_object，用 vLLM 约束解码强制输出合法
        JSON 对象（减少非 JSON、格式漂移）；这是「约束前移」，仍保留调用方的执行前校验。
        """
        call_started = time.perf_counter()
        client = self._get_client()
        setup_ms = (time.perf_counter() - call_started) * 1000
        normalized = [self._to_message_dict(m) for m in messages]
        self.last_call = {"messages": copy.deepcopy(normalized),
                          "model": resolve_model_name(self.cfg.name), "attempts": [],
                          "tokenizer_revision": self.cfg.tokenizer_revision,
                          "processor_revision": self.cfg.processor_revision,
                          "chat_template_revision": self.cfg.chat_template_revision}
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
                if self.cfg.capture_rollout:
                    create_kwargs["logprobs"] = True
                    create_kwargs.setdefault("extra_body", {}).update(
                        return_token_ids=True, return_tokens_as_token_ids=True)
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
                    "client_setup_ms": round(setup_ms, 2),
                    "wall_time_ms": round((time.perf_counter() - call_started) * 1000, 2),
                })
                self.last_call["attempts"].append({"status": "ok", "latency_ms": latency_ms,
                                                   "generation": copy.deepcopy(generation),
                                                   "response": content})
                if self.cfg.capture_rollout:
                    self.last_call.update(self._capture_rollout(resp))
                # Keep sampled text exact; parsing belongs to the action layer.
                return content
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self.last_call["attempts"].append({
                    "status": "error", "error_type": type(exc).__name__,
                    "generation": copy.deepcopy(self.last_call.get("generation", {})),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
                status = getattr(exc, 'status_code', None)
                error_text = str(exc).lower()
                compatible_error = status in (None, 400, 422)
                adjusted = False
                # Changing unsupported request options needs no outage backoff.
                if compatible_error and self.disable_thinking and "extra_body" in error_text:
                    self.disable_thinking = False
                    adjusted = True
                # 若 json_mode 不被支持，降级为无约束重试
                if compatible_error and json_mode and "response_format" in error_text:
                    json_mode = False
                    adjusted = True
                if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429) and not adjusted:
                    self.last_call['attempts'][-1]['retry_policy'] = 'permanent_http_error'
                    break
                if adjusted:
                    self.last_call['attempts'][-1]['retry_policy'] = 'request_option_adjusted'
                elif attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        self.last_call["client_setup_ms"] = round(setup_ms, 2)
        self.last_call["wall_time_ms"] = round((time.perf_counter() - call_started) * 1000, 2)
        raise RuntimeError(f"模型调用失败({self.cfg.name}): {last_err}")

    def chat_stream(self, messages, on_delta, timeout=None):
        """Stream a user deliverable once; partial output is never retried/duplicated."""
        normalized = [self._to_message_dict(m) for m in messages]
        started = time.perf_counter()
        kwargs = dict(model=resolve_model_name(self.cfg.name), messages=normalized,
                      temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                      stream=True, stream_options={'include_usage': True})
        if self.disable_thinking:
            kwargs['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
        self.last_call = {'messages': copy.deepcopy(normalized), 'model': kwargs['model'],
                          'generation': {k: v for k, v in kwargs.items() if k not in {'messages', 'model'}},
                          'attempts': [], 'streaming': True}
        client = self._get_client()
        if timeout is not None:
            client = client.with_options(timeout=min(float(timeout), self.cfg.timeout))
        parts, finish_reason, usage, response_id, first = [], None, None, None, None
        try:
            with client.chat.completions.create(**kwargs) as stream:
                for chunk in stream:
                    response_id = getattr(chunk, 'id', None) or response_id
                    if getattr(chunk, 'usage', None):
                        usage = chunk.usage.model_dump()
                    for choice in chunk.choices:
                        finish_reason = choice.finish_reason or finish_reason
                        text = getattr(choice.delta, 'content', None)
                        if text:
                            if first is None:
                                first = round((time.perf_counter() - started) * 1000, 2)
                            parts.append(text)
                            on_delta(text)
            if finish_reason == 'length':
                raise RuntimeError('report_output_truncated')
            self.last_call['attempts'].append({'status': 'ok'})
            return ''.join(parts)
        except Exception as exc:
            self.last_call['attempts'].append({'status': 'error', 'error_type': type(exc).__name__})
            raise
        finally:
            self.last_call.update(response=''.join(parts), response_id=response_id, usage=usage,
                                  finish_reason=finish_reason, first_token_ms=first,
                                  latency_ms=round((time.perf_counter()-started)*1000, 2),
                                  wall_time_ms=round((time.perf_counter()-started)*1000, 2))

    def _capture_rollout(self, response):
        """Accept actual server tokens, with explicit revision attestation.

        Supports vLLM return_token_ids/logprobs and a training gateway's
        rollout_metadata. A configured revision alone is not server evidence.
        Missing metadata makes the call unusable for on-policy export, while
        preserving it for diagnosis and SFT. Never tokenize returned text here.
        """
        choice = response.choices[0]
        extra = _field(response, "rollout_metadata", {}) or {}
        if not isinstance(extra, dict):
            extra = {}
        ids = extra.get("token_ids", _field(choice, "token_ids"))
        prompt_ids = extra.get("prompt_token_ids", _field(response, "prompt_token_ids"))
        entries = _field(_field(choice, "logprobs"), "content", []) or []
        lp = extra.get("token_logprobs")
        if lp is None and entries:
            lp = [_field(item, "logprob") for item in entries]
        revision = extra.get("policy_revision", _field(response, "policy_revision"))
        captured = {"prompt_token_ids": copy.deepcopy(prompt_ids), "token_ids": copy.deepcopy(ids),
                    "token_logprobs": copy.deepcopy(lp), "policy_revision": revision,
                    "policy_revision_source": "server" if revision else "unavailable",
                    "expected_policy_revision": self.cfg.policy_revision,
                    "loss_mask": [1] * len(ids) if isinstance(ids, list) else None}
        for key in ("tokenizer_revision", "processor_revision", "chat_template_revision"):
            captured[key] = extra.get(key) or getattr(self.cfg, key)
        valid_ids = lambda x: isinstance(x, list) and bool(x) and all(type(t) is int and t >= 0 for t in x)
        problems = []
        if not valid_ids(prompt_ids) or not valid_ids(ids):
            problems.append("missing or invalid server token IDs")
        if (not isinstance(lp, list) or not isinstance(ids, list) or len(lp) != len(ids)
                or any(type(p) not in (float, int) or not math.isfinite(p) or p > 0 for p in lp)):
            problems.append("missing or invalid token logprobs")
        if not isinstance(revision, str) or not revision:
            problems.append("server did not attest policy_revision")
        elif self.cfg.policy_revision and revision != self.cfg.policy_revision:
            problems.append("policy_revision mismatch")
        if any(not captured[k] for k in ("tokenizer_revision", "processor_revision", "chat_template_revision")):
            problems.append("missing tokenizer/processor/chat-template revision")
        for key in ("tokenizer_revision", "processor_revision", "chat_template_revision"):
            expected = getattr(self.cfg, key)
            if expected and extra.get(key) and extra[key] != expected:
                problems.append(f"{key} mismatch")
        # Invalid server floats must remain diagnosable in strict JSON logs.
        # None is explicitly invalid for training; it must never become zero.
        if isinstance(lp, list):
            captured["token_logprobs"] = [
                p if type(p) in (float, int) and math.isfinite(p) else None for p in lp]
        captured.update(rl_ready=not problems, rollout_validation_errors=problems)
        return captured

    def build_vision_message(self, text: str, images=None) -> Dict[str, Any]:
        """构造一条用户消息；vision 开启时按顺序附上多张图（OpenAI vision 格式）。

        images: [(label, screenshot), ...]，label 会作为图片前的文字说明（如 "S(t-1)"）。
        """
        if self.vision and images:
            content: Any = [{"type": "text", "text": text}]
            for index, (label, shot) in enumerate(images):
                url = encode_image(shot)
                if not url:
                    if index == len(images) - 1:
                        raise ImageEncodingError("当前截图编码失败，未发送纯文本替代请求")
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
    "9b": _ModelEntry("Qwen3.5-9B", "9B 决策模型（vLLM @ 10.44.101.62:8094）"),
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
