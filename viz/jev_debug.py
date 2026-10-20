"""Manual System One experiments. No episode, actions or training records created."""
import asyncio
import base64
import io
import json
import os
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from PIL import Image

from ..model.decision_judge import DecisionJudge, DecisionJudgeConfig, validate_questions

router = APIRouter(prefix='/jev', tags=['Jev debugger'])
MAX_BODY = 36 * 1024 * 1024


async def _body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(413, '请求过大（上限 36 MiB）')
    try:
        body = json.loads(data)
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except (ValueError, UnicodeError):
        raise HTTPException(422, '需要 JSON 对象') from None


def _config(data):
    if not isinstance(data, dict):
        raise ValueError('config 必须是对象')
    config = DecisionJudgeConfig(**data)
    if not config.url:
        raise ValueError('请填写 Jev 服务地址')
    if not isinstance(config.api_key, str):
        raise ValueError('API Key 必须是文本')
    return config


def _images(images):
    if not isinstance(images, list) or len(images) > 4:
        raise ValueError('最多上传 4 张截图')
    formats = {'png': 'PNG', 'jpeg': 'JPEG', 'jpg': 'JPEG', 'webp': 'WEBP'}
    for image in images:
        if not isinstance(image, str) or ',' not in image:
            raise ValueError('截图必须是 base64 data URL，不能使用文件路径或网址')
        header, encoded = image.split(',', 1)
        subtype = header.removeprefix('data:image/').removesuffix(';base64')
        if header != f'data:image/{subtype};base64' or subtype not in formats:
            raise ValueError('只接受 PNG / JPEG / WebP 截图')
        if len(encoded) > 12 * 1024 * 1024:
            raise ValueError('单张截图不得超过 9 MiB')
        try:
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format != formats[subtype] or picture.width * picture.height > 40_000_000:
                    raise ValueError()
                picture.verify()
        except Exception:
            raise ValueError('截图内容无效或像素过大') from None
    return images


@router.post('/decide')
async def decide(request: Request):
    data = await _body(request)
    try:
        if set(data) - {'config', 'state', 'questions', 'images'}:
            raise ValueError('请求包含未知字段')
        config = _config(data.get('config', {}))
        questions = data.get('questions')
        validate_questions(questions)
        state = data.get('state', '')
        if len(json.dumps(state, ensure_ascii=False)) > 32000:
            raise ValueError('状态文本过长（上限 32000 字符）')
        images = _images(data.get('images', []))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    # Blocking remote work runs off the websocket/event-loop thread. Credentials
    # live only in this request; do not persist the debug payload in trajectories.
    result = await asyncio.to_thread(DecisionJudge(config).decide, state, questions, images)
    return {'ok': result['status'] == 'answered', **result}


def _probe(config):
    parsed = urlsplit(config.url)
    path = parsed.path.rstrip('/')
    for suffix in ('/v1/systemone', '/v1', '/systemone'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    root = urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    key = config.api_key or os.getenv('DECISION_JUDGE_API_KEY', '')
    started = time.monotonic()
    results = {}
    with httpx.Client(trust_env=False, timeout=min(config.timeout, 10)) as client:
        for name, route in [('health', '/health'), ('models', '/v1/models')]:
            try:
                headers = {'Authorization': 'Bearer ' + key} if key and name == 'models' else {}
                response = client.get(root + route, headers=headers)
                results[name] = {'http_status': response.status_code, 'data': response.json()}
            except Exception as exc:
                results[name] = {'error_type': type(exc).__name__}
    return {'ok': all(r.get('http_status') == 200 for r in results.values()), **results,
            'latency_ms': round((time.monotonic() - started) * 1000, 2)}


@router.post('/probe')
async def probe(request: Request):
    data = await _body(request)
    try:
        if set(data) != {'config'}:
            raise ValueError('仅接受 config')
        config = _config(data['config'])
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    return await asyncio.to_thread(_probe, config)
