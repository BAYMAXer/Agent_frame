"""Markdown editing and isolated selector experiments; no task/UI actions."""
import asyncio
import json
import time
from fastapi import APIRouter, Request, HTTPException
from ..knowledge_base import (load_knowledge, save_knowledge, validate_document,
    parse_entry_markdown, entry_markdown)
from ..knowledge_selection import select_knowledge, validate_method, configure_selection_transport

router = APIRouter()


def draft_document(value):
    if 'markdown_entries' in value:
        if not isinstance(value['markdown_entries'], list):
            raise ValueError('markdown_entries 必须为列表')
        return validate_document({'version': 1, 'entries': [parse_entry_markdown(s) for s in value['markdown_entries']]})
    return validate_document(value['document'])


async def body_json(request):
    body = await request.body()
    if len(body) > 2_000_000:
        raise HTTPException(413, '内容超过 2 MB')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('需要 JSON 对象')
    return value


@router.get('/knowledge')
async def get_knowledge():
    try:
        return {'ok': True, **await asyncio.to_thread(load_knowledge)}
    except Exception as exc:
        raise HTTPException(422, '知识文件无法解析：' + str(exc)[:200]) from exc


@router.put('/knowledge')
async def put_knowledge(request: Request):
    try:
        value = await body_json(request)
        result = await asyncio.to_thread(save_knowledge, draft_document(value), value['revision'])
        return {'ok': True, **result}
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def run_preview(task, snapshot, method, settings):
    from ..config import ModelConfig
    from ..model.decision_model import ChatModel
    from ..model.decision_judge import DecisionJudge, DecisionJudgeConfig
    started = time.perf_counter()
    model = judge = None
    try:
        if method == 'decision':
            cfg = settings.get('decision') or {}
            model = ChatModel(ModelConfig(name=cfg.get('model', 'Qwen3.6-27B'),
                url=cfg.get('url', 'http://10.44.101.62:8094/v1'),
                api_key=cfg.get('api_key') or 'EMPTY', max_tokens=4096, temperature=0, timeout=120))
            configure_selection_transport(model)
        if method == 'jev':
            cfg = settings.get('jev') or {}
            judge = DecisionJudge(DecisionJudgeConfig(url=cfg.get('url') or 'http://10.44.101.62:8007/v1/systemone',
                model=cfg.get('model') or 'vllm-sr/d3-flash', timeout=120))
        result = select_knowledge(task, snapshot, method=method, model=model, judge=judge)
        return {'ok': True, **result, 'selection_ms': result['latency_ms'],
                'latency_ms': round((time.perf_counter()-started)*1000, 2)}
    except Exception as exc:
        # No credentials/endpoints from transport exception strings in the response.
        failed_calls = getattr(exc, 'calls', None) or [client.last_call for client in (model, judge) if getattr(client, 'last_call', None)]
        return {'ok': False, 'status': 'failed', 'method': method, 'error': type(exc).__name__, 'calls': failed_calls,
                'message': '筛选未完成；请检查模型连接、输入大小或返回格式。测试不会自动更换筛选方法。',
                'latency_ms': round((time.perf_counter()-started)*1000, 2)}
    finally:
        if model: model.close()
        if judge: judge.close()


@router.post('/knowledge/preview')
async def preview_knowledge(request: Request):
    try:
        value = await body_json(request)
        task = value['task']
        if not isinstance(task, str) or not task.strip() or len(task) > 20000:
            raise ValueError('请输入任务，最多 20000 字符')
        snapshot = ({'document': draft_document(value), 'revision': 'unsaved-preview'}
                    if 'document' in value or 'markdown_entries' in value else await asyncio.to_thread(load_knowledge))
        method = validate_method(value.get('method', 'all'))
        settings = value.get('settings') or {}
        if not isinstance(settings, dict): raise ValueError('settings 必须为对象')
        return await asyncio.to_thread(run_preview, task, snapshot, method, settings)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
