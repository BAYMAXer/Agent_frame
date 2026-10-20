"""Presentation sandbox: synthetic observations, real judge or explicit scripted replay.

No browser actions, filesystem artifacts or training trajectories are produced.
The live path uses the same DecisionJudge.review and code_review as the agent.
"""
import asyncio
import copy
import threading
import time
from dataclasses import replace
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request

from ..env import Observation
from ..model.decision_judge import DecisionJudge
from ..step_verifier import code_review
from .jev_debug import _body, _config, _images

router = APIRouter(prefix='/jev/demo', tags=['Jev presentation'])
_live_lock = threading.Lock()
TASK = '搜索论文 Attention Is All You Need 并下载，然后改名为 data.pdf。'
TITLE = 'Attention Is All You Need'
SCENARIOS = [
    dict(id='loading_wait', title='黑屏仍在加载', kicker='01 · FAST POLLING', before='load_pending', after='load_pending',
         goal='等待 Attention Is All You Need 搜索结果页面完成加载', expected='Attention Is All You Need 搜索结果列表已经可见且可操作。',
         candidate='observe_browser()', action='observe_browser', args={}, effect='只读刷新页面观察', replay='pending', role='jev',
         takeaway='Jev 保留 pending；一秒后再核查，不让操作员在黑屏上重新决策。',
         narration='合成的加载帧；现场模式会真正向 Jev 发起本次核查请求。'),
    dict(id='loading_ready', title='内容出现，继续任务', kicker='02 · READY SIGNAL', before='load_pending', after='load_ready',
         goal='等待 Attention Is All You Need 搜索结果页面完成加载', expected='Attention Is All You Need 搜索结果列表已经可见且可操作。',
         candidate='observe_browser()', action='observe_browser', args={}, effect='只读刷新页面观察', replay='passed', role='jev',
         takeaway='看到可操作内容后再唤醒操作员；加载期间不计新决策步数。',
         narration='与前一幕同一目标，变化只有当前画面。演示核查调用不等于真实网页已经加载。'),
    dict(id='search', title='输入成功 ≠ 搜索完成', kicker='03 · ACTION VS GOAL', before='search_empty', after='search_typed',
         goal='搜索 Attention Is All You Need 并显示搜索结果列表', expected='已提交搜索，页面显示搜索结果列表，而不只是搜索框有文字。',
         candidate='type("Attention Is All You Need")', action='type', args={'element_id': 'query', 'text': 'Attention Is All You Need'},
         effect='输入框出现 Attention Is All You Need', replay='progress', role='jev',
         takeaway='局部动作有效，保留当前子任务，继续点击搜索；不要重复输入。',
         narration='候选动作来自预设剧本。Jev 对比前后截图：文字输入有效，但搜索尚未提交。'),
    dict(id='wrong_paper', title='搜到相关论文，不等于目标论文', kicker='04 · SEMANTIC CHECK', before='results', after='wrong_pdf',
         goal='核实当前文档就是 Attention Is All You Need 论文本身，不能把仅在摘要提到 Attention Is All You Need 的综述当作目标论文',
         expected='当前文档标题和主体是 Attention Is All You Need 的研究论文；仅在摘要提及不满足要求。',
         candidate='click("[PDF] related survey")', action='click', args={'target': 'PDF link'},
         effect='打开目标 Attention Is All You Need 论文本身', replay='failed', role='jev',
         takeaway='语义核查未通过时，演示控制器拦住后续保存，返回浏览器重搜。',
         narration='这是失败报告中的关键区别：点中了 PDF 链接，却打开了综述。DOM 命中并不证明任务选对。'),
    dict(id='unblocked', title='弹窗消失，论文内容可见', kicker='05 · BEFORE / AFTER', before='paper_modal', after='paper_clear',
         goal='关闭遮挡论文信息的订阅弹窗，使论文标题和 View PDF 链接可见且无遮挡',
         expected='订阅弹窗已消失；Attention Is All You Need 论文标题与 View PDF 链接清晰可见且可操作。',
         candidate='click("Close newsletter dialog")', action='click', args={'target': 'Close newsletter dialog'},
         effect='弹窗关闭，标题和 PDF 入口不再被遮挡', replay='passed', role='jev',
         takeaway='前后画面与结构证据一致时，采信核查结果，推进当前子任务。',
         narration='这一关展示截图的价值：链接存在于 AX 中，不等于视觉上没有遮挡。'),
    dict(id='transferring', title='文件还在传输，框架等待', kicker='06 · NO MODEL NEEDED', before='download_pending', after='download_pending',
         goal='等待浏览器原生下载完成', expected='浏览器事件确认文件完整落盘。',
         candidate='浏览器事件：in_progress', action='click', args={}, effect='浏览器开始下载', replay='pending', role='code',
         takeaway='等待期间不调用 Jev、不重复点击，也不增加决策步数。',
         narration='此处使用预置的 pending_downloads 事件，让同一套代码核查识别“正在处理”。'),
    dict(id='artifact', title='文件事实，由代码验收', kicker='07 · EVIDENCE FIRST', before='download_pending', after='download_done',
         goal='检查已登记下载产物完成且名称为 data.pdf', expected='已完成的 artifact 有路径和正字节数，文件名为 data.pdf。',
         candidate='框架检查示例 artifact 记录', action='observe_browser', args={}, effect='只读核查文件证据', replay='passed', role='code',
         takeaway='明确的文件证据交给代码；Jev 不凭截图猜测下载或改名成功。',
         narration='这是一条合成 artifact 记录，不是实际下载。真正运行时，记录必须来自浏览器事件和文件检查。'),
]


def _observation(scene, image):
    url, nodes, info = 'https://demo.invalid/scholar', [], {'active_surface': 'browser', 'demo': True}
    if scene.startswith('load_'):
        if scene == 'load_ready':
            nodes = [{'role': 'heading', 'name': 'Search results for Attention Is All You Need'},
                     {'role': 'link', 'name': TITLE, 'href': 'https://arxiv.org/abs/1706.03762'}]
        else:
            info['loading'] = True
    elif scene.startswith('search_'):
        nodes = [{'id': 'query', 'role': 'textbox', 'name': 'Search', 'value': 'Attention Is All You Need' if scene == 'search_typed' else ''},
                 {'id': 'submit', 'role': 'button', 'name': 'Search'}]
    elif scene == 'results':
        nodes = [{'role': 'heading', 'name': 'A Survey of Attention Mechanisms'},
                 {'role': 'link', 'name': '[PDF] related survey', 'href': 'https://demo.invalid/pdf/survey'},
                 {'role': 'link', 'name': TITLE, 'href': 'https://arxiv.org/abs/1706.03762'}]
    elif scene == 'wrong_pdf':
        url = 'https://demo.invalid/pdf/survey'
        nodes = [{'role': 'heading', 'name': 'A Survey of Attention Mechanisms'},
                 {'role': 'StaticText', 'name': 'This survey cites Attention Is All You Need but is a different paper.'}]
    elif scene.startswith('paper_'):
        url = 'https://arxiv.org/abs/1706.03762'
        nodes = [{'role': 'heading', 'name': TITLE}, {'role': 'link', 'name': 'View PDF', 'href': 'https://arxiv.org/pdf/1706.03762'}]
        if scene == 'paper_modal':
            nodes += [{'role': 'dialog', 'name': 'Subscribe to the newsletter', 'states': {'modal': True}},
                      {'role': 'button', 'name': 'Close newsletter dialog'}]
    else:
        artifact = {'artifact_id': 'demo-artifact', 'path': 'demo-workspace/data.pdf', 'bytes': 4156037,
                    'completed': scene == 'download_done', 'status': 'completed' if scene == 'download_done' else 'in_progress'}
        info['artifacts' if scene == 'download_done' else 'pending_downloads'] = [artifact]
    for i, node in enumerate(nodes):
        node.update(ref=str(i), in_viewport=True)
    info.update(url=url, page_id='demo-page', document_id=scene.removesuffix('_empty').removesuffix('_typed'))
    return Observation(screenshot=image, context=[{'kind': 'browser_ax', 'url': url, 'nodes': nodes}], info=info)


def _evaluate(scenario, mode, config, images):
    started = time.perf_counter()
    before, after = [_observation(scenario[key], images[i] if images else None) for i, key in enumerate(('before', 'after'))]
    step = {'id': scenario['id'], 'goal': scenario['goal'], 'expected_evidence': scenario['expected'], 'verification': [{'kind': 'visual'}]}
    execution = {'status': 'executed', 'dispatched': True, 'action': scenario['action'], 'args': scenario['args'], 'expected_effect': scenario['effect']}
    contract = {}
    if scenario['id'] == 'artifact':
        artifact = after.info['artifacts'][0]
        valid = artifact['completed'] and artifact['bytes'] > 0 and PurePosixPath(artifact['path']).name == 'data.pdf'
        step.update(check_ids=['demo-file'], verification=[])
        contract = {'checks': [{'id': 'demo-file', 'status': 'passed' if valid else 'failed',
                               'reason': '核对合成 artifact 的完成标记、正字节数及目标文件名'}]}
    code = code_review(step, after, execution, contract)
    code_ms = round((time.perf_counter() - started) * 1000, 3)
    result = {'demo': True, 'trainable': False, 'synthetic_observations': True, 'mode': mode, 'scenario_id': scenario['id'],
              'code_review': code, 'code_latency_ms': code_ms, 'model_called': False, 'model_latency_ms': None,
              'evidence': {'before': before.context, 'after': after.context, 'system': after.info},
              'notice': '预设候选动作与合成页面/事件；不执行浏览器或文件操作，不属于真实任务成功轨迹。'}
    if scenario['role'] == 'code':
        result.update(report=code, outcome='pending' if after.info.get('pending_downloads') else code['status'], source='code_on_fixture')
    elif mode == 'replay':
        label = scenario['replay']
        result.update(outcome=label, source='scripted_replay', report={'status': label if label in {'passed', 'failed'} else 'unknown',
            'step_outcome': label, 'reason': '离线剧本预置结果；未调用 Jev，没有模型分数或实测模型耗时。'})
    else:
        judge = DecisionJudge(replace(config, review_mode='exclusive'))
        try:
            if scenario['id'] in {'loading_wait', 'loading_ready'}:
                judgment = judge.readiness(after.screenshot)
                answer = judgment.get('answers', {}).get('readiness', {})
                label = answer.get('label', 'unknown')
                outcome = {'ready': 'passed', 'waiting': 'pending'}.get(label, 'unknown')
                report = {'method': 'decision_model', 'status': 'passed' if outcome == 'passed' else 'unknown',
                          'step_outcome': outcome, 'reason': '页面已可操作' if outcome == 'passed' else '页面仍在加载' if outcome == 'pending' else '加载状态不确定',
                          'judgment': judgment, 'latency_ms': judgment['latency_ms']}
            else:
                report = judge.review(TASK, step, before, after, execution, code)
        finally:
            judge.close()
        result.update(report=report, outcome=report.get('step_outcome', report['status']), source='live_jev',
                      model_called=True, model_latency_ms=report.get('latency_ms'),
                      model=report.get('judgment', {}).get('model'), usage=report.get('judgment', {}).get('usage', {}))
    result['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)
    return result


@router.get('/scenarios')
def scenarios():
    return {'task': TASK, 'scenarios': copy.deepcopy(SCENARIOS), 'synthetic_observations': True}


@router.post('/evaluate')
async def evaluate(request: Request):
    body = await _body(request)
    try:
        if set(body) - {'scenario_id', 'mode', 'config', 'images'}:
            raise ValueError('未知字段')
        scenario = next((s for s in SCENARIOS if s['id'] == body.get('scenario_id')), None)
        if scenario is None or body.get('mode') not in {'live', 'replay'}:
            raise ValueError('请选择有效场景与 live/replay 模式')
        images = _images(body.get('images', []))
        if len(images) not in {0, 2}:
            raise ValueError('演示截图需要前后两张，或离线模式不提供截图')
        mode = body['mode']
        config = _config(body.get('config', {})) if mode == 'live' and scenario['role'] == 'jev' else None
        if config and len(images) != 2:
            raise ValueError('现场 Jev 核查需要前后两张演示截图')
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    def work():
        if config is None:
            return _evaluate(scenario, mode, config, images)
        if not _live_lock.acquire(blocking=False):
            raise HTTPException(409, '另一个演示核查仍在执行，请等待其结束')
        try:
            return _evaluate(scenario, mode, config, images)
        finally:
            _live_lock.release()
    return await asyncio.to_thread(work)
