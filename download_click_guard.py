"""Reject one unchanged browser download click after its bounded wait found nothing.

This records neither successful transfers nor arbitrary click failures. An observed
page/document/content change releases the guard; observation IDs alone do not.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re


_POINT_TOLERANCE = 8.0  # CSS pixels, to catch the same icon's slightly shifted grounding.
_DOWNLOAD_INTENT = re.compile(r'\bdownload\b|\bsave\b|下载|保存|另存', re.I)
_SIGNALS = ('download_events', 'artifacts', 'pending_downloads', 'artifact_events')


def _point(action, execution_info=None):
    info = execution_info or {}
    point = info.get('coord')
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        args = getattr(action, 'args', {}) or {}
        point = (args.get('x'), args.get('y'))
    if all(type(v) in (int, float) and math.isfinite(v) for v in point):
        return [float(point[0]), float(point[1])]
    return None


def _has_signal(observation, execution_info=None):
    return any((source or {}).get(key) for source in
               (getattr(observation, 'info', {}), execution_info) for key in _SIGNALS)


def _screenshot_hash(screenshot):
    try:
        from PIL import Image
        if isinstance(screenshot, Image.Image):
            image = screenshot.convert('RGB')
        elif isinstance(screenshot, bytes):
            image = Image.open(BytesIO(screenshot)).convert('RGB')
        elif isinstance(screenshot, (str, Path)):
            image = Image.open(screenshot).convert('RGB')
        else:
            return None
        digest = hashlib.sha256()
        digest.update(str(image.size).encode())
        digest.update(image.tobytes())
        return digest.hexdigest()
    except (OSError, ValueError, TypeError):
        return None


def _fingerprint(observation):
    if observation is None:
        return None
    info = observation.info or {}
    if (info.get('screenshot_space') != 'css_viewport' or info.get('screenshot_stale')
            or info.get('observation_error') or info.get('observation_errors')):
        return None
    blocks = [b for b in observation.context or [] if b.get('kind') == 'browser_ax']
    block = blocks[0] if blocks else {}
    identity = {key: info.get(key, block.get(key)) for key in ('page_id', 'document_id', 'url')}
    if not identity['page_id'] or identity['document_id'] is None:
        return None
    screenshot_hash = _screenshot_hash(observation.screenshot)
    if screenshot_hash is None:
        return None
    # Ref numbering, capture time, focus and snapshot IDs can change after a
    # harmless observation. Text, values and actionable states are real changes.
    semantic = []
    for b in blocks:
        for node in b.get('nodes', []):
            states = {k: v for k, v in (node.get('states') or {}).items() if k != 'focused'}
            semantic.append({key: node.get(key) for key in ('role', 'name', 'value', 'href', 'id', 'tag', 'direct')}
                            | {'states': states})
    return {**identity, 'screenshot_hash': screenshot_hash,
            'structure_hash': hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False,
                                                       default=str).encode()).hexdigest(),
            'screenshot_size': list(info.get('screenshot_size') or []),
            'scroll_offset': list(info.get('scroll_offset') or [])}


def remember_unconfirmed_download_click(state, original_action, resolved_action, execution):
    """Record only a dispatched download-intent click with no observed effect."""
    info = execution.info or {}
    if (getattr(original_action, 'action', None) != 'click' or info.get('dispatched') is not True
            or (info.get('framework_wait') or {}).get('status') != 'no_signal'):
        return
    args = original_action.args or {}
    intent = str(args.get('target') or '')
    if not _DOWNLOAD_INTENT.search(intent):
        return
    before, after = state.obs, execution.obs or state.obs
    if _has_signal(before, info) or _has_signal(after):
        state.unconfirmed_download_click = {}
        return
    fingerprint = _fingerprint(before)
    # A click which changed the page may have opened a menu/dialog. That is a
    # different state, so never prevent its next interaction as a duplicate.
    if fingerprint is None or fingerprint != _fingerprint(after):
        state.unconfirmed_download_click = {}
        return
    point = _point(resolved_action or original_action, info)
    if point is None or info.get('coordinate_space', 'css_viewport') != 'css_viewport':
        return
    state.unconfirmed_download_click = {'fingerprint': fingerprint, 'point': point, 'intent': intent[:160]}


def repeated_download_click_error(action, state):
    """Read the current observation; never dispatch or initiate a transfer."""
    previous = getattr(state, 'unconfirmed_download_click', {})
    if not previous:
        return None
    current = _fingerprint(state.obs)
    if _has_signal(state.obs) or current is None or current != previous['fingerprint']:
        state.unconfirmed_download_click = {}
        return None
    point = _point(action)
    if getattr(action, 'action', None) != 'click' or point is None:
        return None
    if math.dist(point, previous['point']) > _POINT_TOLERANCE:
        return None
    reason = ('上次相同下载位置的点击已派发，但等待后没有下载事件或文件，当前页面和画面仍未变化；'
              '本次重复点击未执行。请换下载入口/保存路径，或先 observe_desktop 检查保存对话框、'
              'observe_browser 检查页面；新观察内容发生变化后可重新定位操作。')
    return {'status': 'rejected', 'dispatched': False, 'error_code': 'repeated_unconfirmed_download_click',
            'reason': reason, 'coord': point, 'coordinate_space': 'css_viewport',
            'previous_coord': previous['point'], 'previous_download_intent': previous['intent']}
