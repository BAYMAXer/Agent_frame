"""Bounded, task-relevant observations for the 9B judge; no policy claims as facts."""
import hashlib
import json
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

from .model.decision_model import encode_image
from .browser_results import paper_result_cards
from .paper_resolver import query_matches_title
from .scholar_search import is_scholar, scholar_result_cards

NODE_KEYS = ('ref', 'parent', 'id', 'role', 'name', 'value', 'states', 'href', 'in_viewport')
ARTIFACT_KEYS = ('artifact_id', 'download_id', 'status', 'completed', 'path', 'bytes', 'size_bytes', 'url', 'final_url')


def bounded(value, text_limit=240, items=8, depth=0):
    if depth > 4:
        return str(value)[:text_limit]
    if isinstance(value, str):
        return value[:text_limit]
    if isinstance(value, dict):
        return {str(k): bounded(v, text_limit, items, depth+1) for k, v in list(value.items())[:items]}
    if isinstance(value, (list, tuple)):
        return [bounded(v, text_limit, items, depth+1) for v in value[:items]]
    return value


def nodes(obs):
    return [n for b in (getattr(obs, 'context', None) or []) if b.get('kind') == 'browser_ax'
            for n in b.get('nodes', []) if isinstance(n, dict)]


def page_url(obs):
    return (getattr(obs, 'info', None) or {}).get('url') or next(
        (b.get('url', '') for b in (getattr(obs, 'context', None) or []) if b.get('kind') == 'browser_ax'), '')


def terms(value):
    text = json.dumps(value, ensure_ascii=False).casefold()
    words = re.findall(r'[a-z0-9_./:-]{3,}|[\u4e00-\u9fff]+', text)
    result = set()
    for word in words:
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            result.update(word[i:i+2] for i in range(len(word)-1))
        else:
            result.add(word)
    return result


def _key(node):
    return ('id', node['id']) if node.get('id') else (node.get('role'), node.get('name', ''))


def _values(node):
    # Ignore changing refs, capture times and layout-only properties.
    return json.dumps({k: node.get(k) for k in ('name', 'value', 'states', 'href')}, sort_keys=True, ensure_ascii=False)


def target_identity(obs, args):
    candidates = nodes(obs)
    if args.get('target_ref') is not None:
        candidates = [n for n in candidates if str(n.get('ref')) == str(args['target_ref'])]
    elif args.get('element_id'):
        candidates = [n for n in candidates if n.get('id') == args['element_id']]
    elif args.get('target'):
        candidates = [n for n in candidates if n.get('name') == args['target']]
    else:
        return {}
    if len(candidates) != 1:
        return {}
    return {k: candidates[0][k] for k in ('id', 'role', 'name', 'href') if k in candidates[0]}


def select_frame(obs, step=None, execution=None, other=None, *, max_nodes=40):
    if obs is None:
        return None
    step, execution = step or {}, execution or {}
    info = getattr(obs, 'info', None) or {}
    all_nodes, other_nodes = nodes(obs), nodes(other)
    args = execution.get('args') or {}
    target = execution.get('target_node', {})
    focus = terms([step.get('goal'), step.get('expected_evidence'), step.get('verification'), args, target])
    other_values = {_key(n): _values(n) for n in other_nodes}
    ranked = []
    for index, node in enumerate(all_nodes):
        content = ' '.join(str(node.get(k, '')) for k in ('name', 'value', 'href', 'id')).casefold()
        score = min(12, sum(t in content for t in focus)) * 4
        if node.get('role') in {'dialog', 'alert', 'alertdialog', 'status'}:
            score += 35
        if node.get('states', {}).get('focused'):
            score += 20
        # Refs belong to the before snapshot; semantic/id matches survive reindexing.
        if (args.get('element_id') and str(node.get('id', '')) == str(args['element_id'])) or (args.get('target') and str(args['target']).casefold() == str(node.get('name', '')).casefold()):
            score += 65
        if target and _key(node) == _key(target):
            score += 80
        if other_values and other_values.get(_key(node)) != _values(node):
            score += 18
        if node.get('in_viewport') is False or node.get('states', {}).get('hidden'):
            score -= 12
        ranked.append((score, index, node))
    chosen = sorted(ranked, key=lambda item: (-item[0], item[1]))[:max_nodes]
    # Include nearby labels/parents around highest-ranked targets.
    indices = {index for _, index, _ in chosen[:max(1, max_nodes-8)]}
    refs = {str(n.get('ref')): i for i, n in enumerate(all_nodes)}
    for _, index, node in chosen[:4]:
        for near in (refs.get(str(node.get('parent'))), index-1, index+1):
            if near is not None and 0 <= near < len(all_nodes) and len(indices) < max_nodes:
                indices.add(near)
    for _, index, _ in chosen:
        if len(indices) < max_nodes:
            indices.add(index)
    selected = [{k: bounded(all_nodes[i][k], 180, 10) for k in NODE_KEYS if k in all_nodes[i]} for i in sorted(indices, key=lambda i: (-ranked[i][0], i))]
    artifacts = [a for a in info.get('artifacts', []) if isinstance(a, dict)]
    old_ids = {a.get('artifact_id') or a.get('download_id') or a.get('path')
               for a in (getattr(other, 'info', None) or {}).get('artifacts', []) if isinstance(a, dict)}
    def artifact_rank(pair):
        index, a = pair
        content = json.dumps(a, ensure_ascii=False).casefold()
        related = sum(t in content for t in focus)
        ident = a.get('artifact_id') or a.get('download_id') or a.get('path')
        return (related, bool(other is not None and ident not in old_ids), index)
    selected_artifacts = [a for _, a in sorted(enumerate(artifacts), key=artifact_rank, reverse=True)[:5]]
    paper_blocks = [block for block in (getattr(obs, 'context', None) or []) if block.get('kind') == 'browser_ax']
    cards, result_pages = [], []
    for block in paper_blocks:
        page_cards = paper_result_cards(block)
        source = 'arxiv'
        if not page_cards and is_scholar(block.get('url', '')):
            source = 'google_scholar'
            page_cards = [{'position': i, 'title': item['title'], 'authors': [],
                           'abs_ref': item['ref'], 'abs_url': item['href'],
                           'pdf_links': item['pdf_links']}
                          for i, item in enumerate(scholar_result_cards(block), 1)]
        if not page_cards:
            continue
        origin = {'source': source, 'snapshot_id': str(block.get('snapshot_id', ''))[:200],
                  'page_url': str(block.get('url', ''))[:1200]}
        cards.extend({**card, **origin} for card in page_cards)
        result_pages.append({**origin, 'result_count': len(page_cards)})
    query = next((str(p.get('query', '')) for p in step.get('verification', [])
                  if p.get('kind') == 'search_results' and p.get('query')), '')
    if not query:
        match = re.search(r'[“"]([^”"]{3,300})[”"]', str(step.get('goal', '')))
        query = match.group(1) if match else ''
    key = ' '.join(unicodedata.normalize('NFKC', query).casefold().split())
    matches = [card for card in cards if query_matches_title(query, card['title'])]
    observed_matches = []
    for card in matches[:5]:
        entry = bounded({k: card[k] for k in ('position', 'title', 'authors', 'abs_url')}, 300, 4)
        entry.update(match_kind='full_title' if ' '.join(unicodedata.normalize(
            'NFKC', card['title']).casefold().split()) == key else 'leading_alias',
            source=card['source'], snapshot_id=card['snapshot_id'], page_url=card['page_url'],
            result_ref=str(card['abs_ref']))
        if card.get('pdf_links'):
            entry['pdf_links'] = bounded(card['pdf_links'], 500, 8)
        observed_matches.append(entry)
    paper_results = {'current_page_count': len(cards), 'query_title': query[:300],
                     'exact_title_matches': observed_matches,
                     'match_rule': 'complete_title_or_leading_alias', 'identity_verified': False,
                     'matched_count': len(matches), 'matches_shown': len(observed_matches),
                     'matches_omitted': len(matches) - len(observed_matches),
                     'pages': result_pages,
                     'scope': 'current observation browser AX snapshots only'} if cards else None
    runtime = info.get('runtime_state') or {}
    apps = sorted(runtime.get('applications', []), key=lambda a: not a.get('foreground'))[:4]
    runtime_view = {k: runtime[k] for k in ('source', 'complete') if k in runtime}
    runtime_view['applications'] = [{k: bounded(a[k], 180, 6) for k in (
        'process_name', 'pid', 'hwnd', 'title', 'class_name', 'foreground', 'bounds',
        'visible', 'minimized', 'cloaked', 'enabled', 'responsive') if k in a} for a in apps]
    def cli_evidence(result):
        # Pick semantic evidence before bounding: slicing dictionary insertion
        # order drops file/equal fields behind status/transport metadata.
        return {k: bounded(result[k], 2000, 16) for k in ('operation', 'path', 'destination', 'cwd', 'status',
            'exit_code', 'effect_verified', 'file', 'source', 'other_file', 'equal', 'sha256', 'other_sha256',
            'stdout', 'stderr', 'stdout_truncated', 'error_code') if k in result}
    frame = {'cli_result': cli_evidence(info.get('cli_result') or {}),
             'cli_checks': [{'predicate': c['predicate'], 'result': cli_evidence(c['result'])}
                            for c in info.get('cli_checks', [])[:8]], 'url': str(page_url(obs))[:1200], 'surface': info.get('active_surface'),
             'nodes': selected, 'artifacts': [{k: bounded(a[k], 500) for k in ARTIFACT_KEYS if k in a} for a in selected_artifacts],
             'paper_results': paper_results,
             'pending_downloads': bounded(info.get('pending_downloads', []), 300, 4),
             'runtime_state': runtime_view,
             'desktop_focus_stable': info.get('desktop_focus_stable'),
             'desktop_window_wait': bounded(info.get('desktop_window_wait'), 200, 8),
             'workspace': bounded(info.get('initial_state'), 400, 6),
             'dialogs': bounded(info.get('dialogs', info.get('dialog', [])), 400, 4),
             'text': str(getattr(obs, 'text', '') or '')[:1600],
             'selection': {'nodes_total': len(all_nodes), 'nodes_selected': len(selected),
                           'artifacts_total': len(artifacts), 'artifacts_selected': len(selected_artifacts),
                           'truncated': len(selected) < len(all_nodes) or len(selected_artifacts) < len(artifacts)}}
    return frame


_NON_INTERACTIVE_ROLES = {
    'StaticText', 'text', 'paragraph', 'heading', 'image', 'img', 'LineBreak',
    'caption', 'note', 'figure', 'document', 'article', 'section', 'separator',
    'LabelText',
}
_ACTIVE_STATES = ('focused', 'selected', 'checked', 'expanded', 'pressed', 'current')


def _new_artifacts(before, after):
    old = {a.get('artifact_id') or a.get('download_id')
           for a in (before.info or {}).get('artifacts', []) if isinstance(a, dict)}
    return any(isinstance(a, dict) and a.get('completed') is True
               and (a.get('artifact_id') or a.get('download_id')) not in old
               for a in (after.info or {}).get('artifacts', []))


def _target_node(obs, execution):
    args = execution.get('args') or {}
    cands = nodes(obs)
    ref = args.get('target_ref')
    eid = args.get('element_id')
    target = args.get('target')
    if ref is not None:
        cands = [n for n in cands if str(n.get('ref')) == str(ref)]
    elif eid:
        cands = [n for n in cands if n.get('id') == eid]
    elif target:
        cands = [n for n in cands if n.get('name') == target]
    else:
        return None
    return cands[0] if len(cands) == 1 else None


def _is_active(node):
    states = node.get('states') or {}
    return any(states.get(k) for k in _ACTIVE_STATES)


def _interactive_signature(obs_nodes):
    """交互控件的身份+状态签名，忽略非交互文本（公告轮播）的漂移。"""
    return tuple(sorted(
        (_key(n), _values(n))
        for n in obs_nodes
        if n.get('role') not in _NON_INTERACTIVE_ROLES
    ))


def click_effect_denied(before, after, execution):
    """对「已激活控件上的死点击」给出确定性否认；未否认返回 None。

    页面带公告轮播时，每次观察截图都变，屏幕差分永远触发，框架无法证明点击无效。
    这里用结构信号兜底：URL / 下载 / 页面结构 / 交互控件状态都没变，只有非交互文本
    （轮播公告）变化，且点击目标本就是已激活控件 → 判定为死点击。
    """
    if before is None or after is None:
        return None
    action = execution.get('action')
    if action not in {'click', 'double_click'}:
        return None
    if execution.get('status') != 'executed' or execution.get('dispatched') is False:
        return None
    if page_url(before) != page_url(after):
        return None
    if _new_artifacts(before, after):
        return None
    before_nodes, after_nodes = nodes(before), nodes(after)
    if len(after_nodes) > len(before_nodes):
        return None
    if _interactive_signature(before_nodes) != _interactive_signature(after_nodes):
        return None
    target = _target_node(before, execution)
    if target is None or not _is_active(target):
        return None
    return {'no_effect': True,
            'reason': '该点击未产生任何可观察变化：目标控件本已处于激活态，'
                      '且 URL、下载、页面结构与交互状态均未改变，仅公告轮播等非交互文本变化。'}


def effect_review(before, after, execution):
    """Narrow claims about dispatched inputs/navigation, never business completion."""
    result = {'method': 'code', 'status': 'unknown', 'reason': '尚无确定性动作效果证据', 'evidence': []}
    if execution.get('status') in {'failed', 'error', 'rejected'} or execution.get('dispatched') is False:
        return {**result, 'status': 'failed', 'reason': '动作未成功派发或执行器报告失败'}
    if execution.get('status') in {'pending', 'in_progress'}:
        return {**result, 'status': 'pending', 'reason': '系统报告操作仍在进行'}
    action, args = execution.get('action'), execution.get('args') or {}
    if action == 'cli' and execution.get('effect_verified') is True:
        return {'method': 'code', 'status': 'passed', 'reason': 'CLI 执行器已回读核对文件操作结果；不代表整项任务完成',
                'evidence': [execution.get('destination') or execution.get('path') or 'file_effect']}
    if action == 'goto' and args.get('url') and execution.get('dispatched') is True and before is not None and after is not None:
        # Navigating to a downloadable resource can leave the visible URL
        # unchanged and raise "Download is starting" from Playwright. A new
        # completed browser artifact for that URL proves the local effect.
        old_ids = {a.get('artifact_id') or a.get('download_id')
                   for a in (before.info or {}).get('artifacts', []) if isinstance(a, dict)}
        matched = [a for a in (after.info or {}).get('artifacts', []) if isinstance(a, dict)
                   and a.get('completed') is True and (a.get('artifact_id') or a.get('download_id'))
                   and (a.get('artifact_id') or a.get('download_id')) not in old_ids
                   and str(args['url']).split('#', 1)[0] in
                       {str(a.get(k) or '').split('#', 1)[0] for k in ('url', 'final_url', 'source_url')}]
        if len(matched) == 1:
            return {**result, 'status': 'passed', 'reason': '目标 URL 触发浏览器已完成下载；文件身份和任务要求另行核查',
                    'evidence': [str(matched[0].get('artifact_id') or matched[0].get('download_id'))]}
    if execution.get('status') != 'executed' or before is None or after is None:
        return result
    if action == 'scroll':
        scroll = execution.get('scroll_feedback') or {}
        if scroll.get('available') is True:
            delta, requested = scroll.get('actual_delta'), scroll.get('requested_delta')
            if scroll.get('moved') is True and type(delta) in (int, float):
                if type(requested) in (int, float) and delta * requested < 0:
                    return {**result, 'reason': '实际滚动方向与请求不同，需要结合当前页面核查'}
                return {**result, 'status': 'passed', 'reason': '滚动容器产生了实际位移；目标内容仍须核查',
                        'evidence': [str(scroll.get('target_container') or {}), str(delta)]}
            if scroll.get('noop') is True:
                boundary = ('已到达底部' if type(requested) in (int, float) and requested > 0 and scroll.get('at_bottom')
                            else '已到达顶部' if type(requested) in (int, float) and requested < 0 and scroll.get('at_top')
                            else '需要检查滚动区域、遮挡或加载状态')
                return {**result, 'reason': '未测得滚动位移；' + boundary}
    def normalize(url):
        value = urlsplit(url)
        return urlunsplit((value.scheme.lower(), value.netloc.lower(), value.path or '/', value.query, value.fragment))
    if action == 'goto' and args.get('url'):
        if normalize(str(page_url(after))) == normalize(str(args['url'])):
            return {**result, 'status': 'passed', 'reason': '目标 URL 已打开；不代表页面业务已完成', 'evidence': [str(page_url(after))]}
    target = execution.get('target_node') or target_identity(before, args)
    if action in {'click', 'double_click'}:
        if target.get('href'):
            destination = urljoin(page_url(before), target['href'])
            if normalize(page_url(after)) == normalize(destination):
                return {**result, 'status': 'passed', 'reason': '点击后已导航到目标链接；页面内容仍须核查',
                        'evidence': [destination]}
        old_artifacts = {a.get('artifact_id') or a.get('download_id') for a in
                         (before.info or {}).get('artifacts', []) if isinstance(a, dict)}
        new_artifacts = [a for a in (after.info or {}).get('artifacts', []) if isinstance(a, dict)
                         and a.get('completed') is True
                         and (a.get('artifact_id') or a.get('download_id')) not in old_artifacts]
        if len(new_artifacts) == 1:
            return {**result, 'status': 'passed', 'reason': '点击后浏览器登记了一个已完成下载；论文身份另行核查',
                    'evidence': [str(new_artifacts[0].get('artifact_id') or new_artifacts[0].get('download_id'))]}
        if target.get('id'):
            old = [n for n in nodes(before) if n.get('id') == target['id']]
            new = [n for n in nodes(after) if n.get('id') == target['id']]
            if len(old) == len(new) == 1 and old[0].get('states', {}).get('expanded') != new[0].get('states', {}).get('expanded'):
                return {**result, 'status': 'passed', 'reason': '目标控件展开状态已改变',
                        'evidence': [target['id']]}
    if action == 'select' and args.get('option'):
        candidates = [n for n in nodes(before) if (args.get('target_ref') is not None and
                      str(n.get('ref')) == str(args['target_ref'])) or
                      (args.get('element_id') and n.get('id') == args['element_id'])]
        if len(candidates) == 1:
            old = candidates[0]
            matches = [n for n in nodes(after) if _key(n) == _key(old)]
            if (len(matches) == 1 and old.get('value') != args['option']
                    and matches[0].get('value') == args['option']):
                return {**result, 'status': 'passed', 'reason': '下拉框选中值已改变；不代表表单已提交',
                        'evidence': [str(args['option'])]}
    if action == 'press' and str(args.get('key', '')).lower() in {'enter', 'return'}:
        if page_url(before) and page_url(after) and normalize(page_url(before)) != normalize(page_url(after)):
            return {**result, 'status': 'passed', 'reason': '按 Enter 后页面 URL 已改变；目标内容仍须核查',
                    'evidence': [page_url(after)]}
    if action in {'click', 'double_click'}:
        denied = click_effect_denied(before, after, execution)
        if denied is not None:
            return {**result, 'no_effect': True, 'reason': denied['reason']}
    if action == 'type' and args.get('submit_key'):
        # Filled text alone proves only the first primitive, never submission
        # or search completion. Keep the full combined outcome for Jev/queue.
        return {**result, 'reason': '已请求输入并提交；须核查提交后的结果，输入值变化不证明搜索已完成'}
    if action != 'type' or not isinstance(args.get('text'), str) or not args['text']:
        return result
    candidates = [n for n in nodes(before) if n.get('role') in {'textbox', 'searchbox', 'combobox'}]
    if args.get('target_ref') is not None:
        candidates = [n for n in candidates if str(n.get('ref')) == str(args['target_ref'])]
    elif args.get('element_id'):
        candidates = [n for n in candidates if n.get('id') == args['element_id']]
    elif args.get('target'):
        candidates = [n for n in candidates if n.get('name') == args['target']]
    else:
        candidates = [n for n in candidates if n.get('states', {}).get('focused')]
    if len(candidates) != 1 or page_url(before) != page_url(after):
        return result
    for key in ('page_id', 'document_id'):
        old_id, new_id = (before.info or {}).get(key), (after.info or {}).get(key)
        if old_id and new_id and old_id != new_id:
            return result
    old = candidates[0]
    matches = [n for n in nodes(after) if _key(n) == _key(old)]
    if len(matches) == 1 and isinstance(matches[0].get('value'), str):
        value, previous = matches[0]['value'], old.get('value', '')
        if value != previous and (value == args['text'] or value == str(previous) + args['text']):
            return {**result, 'status': 'passed', 'reason': '目标输入框已出现本次输入；子任务仍需单独核查',
                    'evidence': [str(old.get('name') or old.get('id') or '输入框') + ': ' + value[:160]]}
    return result


def evidence_fingerprint(obs, step, execution):
    frame = select_frame(obs, step, execution)
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items() if k not in {
                'ref', 'parent', 'snapshot_id', 'captured_at', 'timestamp', 'latency_ms', 'elapsed_s', 'selection'}}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    frame = stable(frame)
    if frame:
        frame['nodes'] = sorted(frame['nodes'], key=lambda n: json.dumps(n, sort_keys=True, ensure_ascii=False))
    encoded = encode_image(getattr(obs, 'screenshot', None))
    return hashlib.sha256((json.dumps(frame, ensure_ascii=False, sort_keys=True) + encoded).encode()).hexdigest()


def checkpoint(obs, step, report, decision_step):
    if obs is None or obs.screenshot is None or (obs.info or {}).get('screenshot_stale'):
        return None
    image = encode_image(obs.screenshot)
    if not image:
        return None
    return {'id': f"{step['id']}:a{decision_step}", 'goal': step.get('goal', ''),
            'decision_step': decision_step, 'review_method': report.get('method'),
            'frame': select_frame(obs, step, max_nodes=16), 'screenshot': image,
            'sha256': hashlib.sha256(image.encode()).hexdigest()}
