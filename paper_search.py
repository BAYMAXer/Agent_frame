"""Observed paper-search state and bounded recovery advice, without navigation.

The operator still chooses and dispatches every action. These checks only reject
redundant input/submission against a uniquely observed search field. They never
invent a paper URL, change the identity goal or treat a candidate as verified.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit, urljoin

from .browser_results import paper_result_cards
from .paper_resolver import query_matches_title
from .scene import browser_context
from .scholar_search import is_scholar, scholar_title_links
from .paper_sources import paper_search_policy


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def _unquote(value):
    return _key(value).strip('"“”')


def _repeated_query_input(value, query):
    """Recognize only two or more complete copies of the original query."""
    compact = _key(value).translate(str.maketrans('', '', ' "“”'))
    compact_base = _unquote(query).replace(' ', '')
    return bool(compact_base and re.fullmatch('(?:' + re.escape(compact_base) + '){2,}', compact))


def _select_options(control, nodes):
    if control.get('options'):
        return list(control['options'])
    children = {}
    for node in nodes:
        children.setdefault(str(node.get('parent')), []).append(node)
    pending, seen, labels = list(children.get(str(control.get('ref')), [])), set(), []
    while pending:
        node = pending.pop(0)
        ref = str(node.get('ref'))
        if ref in seen:
            continue
        seen.add(ref)
        if node.get('role') == 'option' and node.get('name'):
            labels.append(str(node['name']))
        else:
            pending.extend(children.get(ref, []))
    return labels


def _query_words(text):
    return set(re.findall(r'[a-z0-9]+(?:[._-][a-z0-9]+)*|[\u4e00-\u9fff]+', _key(text)))


def _unobserved_query_terms(text, query, page):
    # Name expansion is allowed, but an unknown author/identifier from model
    # memory cannot substitute for finding the target in the page.
    allowed = _query_words(query) | {'paper', 'papers', 'pdf', 'arxiv', 'benchmark', 'project', 'dataset',
                                    '论文', '研究', '项目', '文章', '下载'}
    for node in page['nodes']:
        allowed.update(_query_words(node.get('name', '')))
        allowed.update(_query_words(node.get('href', '')))
    return _query_words(text) - allowed


def search_observation(obs, query):
    """Recognize search controls by real IDs/roles, independent of UI language."""
    info = getattr(obs, 'info', None) or {}
    if info.get('active_surface') == 'desktop' or info.get('screenshot_stale'):
        return None
    for block in browser_context(obs):
        url = str(block.get('url') or info.get('url') or '')
        parsed = urlsplit(url)
        if is_scholar(url):
            engine, parameter, control_id = 'scholar', 'q', 'gs_hdr_tsi'
            rows = scholar_title_links(block)
        elif parsed.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
            engine, parameter, control_id = 'arxiv', 'query', 'query'
            rows = [{'ref': c['abs_ref'], 'title': c['title'], 'href': c['abs_url']}
                    for c in paper_result_cards(block)]
        else:
            continue
        nodes = block.get('nodes', [])
        controls = [n for n in nodes if n.get('id') == control_id
                    and n.get('role') in {'textbox', 'searchbox'}
                    and not (n.get('states') or {}).get('disabled')]
        if len(controls) != 1 or not isinstance(controls[0].get('value'), str):
            return None
        params = parse_qs(parsed.query)
        sort_params = parse_qs(parsed.query, keep_blank_values=True)
        submitted = params.get(parameter, [''])[0]
        matches = [r for r in rows if query_matches_title(query, r['title'])]
        empty = any(re.search(r'^(?:sorry[, :].*)?no results|did not match any|returned no results|'
                              r'keine (?:such)?ergebnisse|没有.{0,8}(?:结果|匹配)',
                              str(n.get('name') or '').strip(), re.I)
                    for n in nodes if n.get('role') in {'heading', 'status', 'StaticText'})
        # Selecting Title is not a submission: the URL still represents the
        # previous form. Do not suppress the subsequent Search button/Enter.
        labels = {'all fields': 'all', 'title': 'title', 'author': 'author', 'abstract': 'abstract',
                  'arxiv id': 'id', 'doi': 'doi'}
        field_changed = (engine == 'arxiv' and any(
            n.get('id') == 'searchtype' and bool(n.get('value'))
            and labels.get(_key(n.get('value')), _key(n.get('value'))) != _key(params.get('searchtype', ['all'])[0])
            for n in nodes))
        relevance = next((n for n in nodes if engine == 'arxiv' and n.get('id') == 'order'
                          and n.get('role') == 'combobox' and 'Relevance' in _select_options(n, nodes)), None)
        relevance_submitted = sort_params.get('order') == ['']
        current_start = int(params.get('start', ['0'])[0]) if params.get('start', ['0'])[0].isdigit() else 0
        next_result = None
        for node in nodes:
            if engine != 'arxiv' or node.get('role') != 'link' or _key(node.get('name')) != 'next' or not node.get('direct'):
                continue
            next_url = urljoin(url, str(node.get('href') or ''))
            destination = urlsplit(next_url)
            next_params = parse_qs(destination.query)
            start = next_params.get('start', [''])[0]
            if (destination.hostname == parsed.hostname and destination.path.startswith('/search/')
                    and _key(next_params.get('query', [''])[0]) == _key(submitted)
                    and start.isdigit() and int(start) > current_start):
                next_result = {'ref': node.get('ref'), 'url': next_url, 'start': int(start)}
                break
        return {'engine': engine, 'url': url, 'submitted_query': submitted,
                'control': controls[0], 'nodes': nodes, 'results': rows,
                'matches': matches, 'results_observed': bool(rows) or empty,
                'form_changed': field_changed, 'relevance_control': relevance,
                'next_result': next_result,
                'relevance_submitted': relevance_submitted,
                'sort_changed': bool(relevance and _key(relevance.get('value')) == 'relevance' and not relevance_submitted),
                'filters': {k: params[k] for k in
                    ('as_ylo', 'as_yhi', 'lr', 'scisbd', 'searchtype', 'order') if k in params}}
    return None


def _targets_control(action, page):
    args, control = action.args, page['control']
    if args.get('target_ref'):
        return str(args['target_ref']) == str(control.get('ref'))
    if args.get('element_id'):
        return args['element_id'] == control.get('id')
    literal = str(args.get('target') or '').strip()
    if literal == str(control.get('ref')) or literal in {str(control.get('id')), '#' + str(control.get('id'))}:
        return True
    return bool(control.get('name') and _key(args.get('target')) == _key(control['name']))


def _submits_search(action, page):
    # Enter on a focused result link must not be mistaken for form submission.
    if action.action == 'press' and _key(action.args.get('key')) in {'enter', 'return'}:
        return (page['control'].get('states') or {}).get('focused') is True
    if action.action != 'click':
        return False
    args = action.args
    node = next((n for n in page['nodes'] if
                 (args.get('target_ref') and str(n.get('ref')) == str(args['target_ref']))
                 or (args.get('element_id') and n.get('id') == args['element_id'])), None)
    if node is None and not args.get('target_ref') and not args.get('element_id'):
        literal = str(args.get('target') or '').strip()
        matches = [n for n in page['nodes'] if n.get('role') == 'button' and literal
                   and (literal == str(n.get('ref'))
                        or n.get('id') and literal in {n['id'], '#' + n['id']}
                        or n.get('name') and _key(literal) == _key(n['name']))]
        node = matches[0] if len(matches) == 1 else None
    if not node or node.get('role') != 'button':
        return False
    if node.get('id') == 'gs_hdr_tsb':
        return True
    by_ref = {str(n.get('ref')): n for n in page['nodes']}
    def form(n):
        seen = set()
        while n and str(n.get('ref')) not in seen:
            seen.add(str(n.get('ref')))
            if n.get('role') == 'form' or n.get('tag') == 'form':
                return str(n.get('ref'))
            n = by_ref.get(str(n.get('parent')))
        return None
    owner = form(page['control'])
    return bool(owner and owner == form(node) and _key(node.get('name')) in {'search', '搜索', 'suchen'})


@dataclass
class PaperSearchRecovery:
    """Remember submitted result states, not cursor/focus/ref changes or claims."""
    attempts: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)
    task: str = ''

    @staticmethod
    def _scope(query, page):
        return (_key(query), page['engine'], json.dumps(page['filters'], sort_keys=True))

    @staticmethod
    def _variants(query):
        raw = query.strip('"“”')
        return [raw, raw + ' paper']

    def _next_advice(self, page, query):
        tried = self.attempts.get(self._scope(query, page), [])
        proposed = next((v for v in self._variants(query) if _key(v) not in tried
                         and _key(v) != _key(page['submitted_query'])), None)
        if proposed:
            return ('建议检索词：' + proposed + '。搜索框 ref=' + str(page['control'].get('ref'))
                    + '使用 type(text=' + json.dumps(proposed, ensure_ascii=False)
                    + ', overwrite:true)，然后 press Enter 或点击同一表单提交；不要猜作者或论文ID。')
        source = paper_search_policy(self.task)
        if source['explicit']:
            return ('用户已明确指定或限制了来源；继续在指定来源内依据实际标题、摘要调整检索，'
                    '检查筛选条件或查看相关候选详情，不得因暂未找到就擅自换成其他搜索入口。'
                    '若指定来源不可用或已无法继续，应如实说明障碍；用户明确允许换站时再更换。')
        other = 'arxiv' if page['engine'] == 'scholar' else 'scholar'
        other_tried = self.attempts.get((_key(query), other, '{}'), [])
        if any(_key(v) not in other_tried for v in self._variants(query)):
            portal = 'https://arxiv.org/search/' if other == 'arxiv' else 'https://scholar.google.com/scholar?hl=en'
            return ('当前入口的原名称及名称扩展已经尝试；先判断现有候选的语义相关性，如仍无合适候选且任务允许，换搜索入口 ' + portal
                    + '，按还未尝试的原名称或名称加paper检索。不要重复提交同一查询。')
        return ('两个学术入口的原名称及名称扩展都已尝试；不要仅因没有名称字面命中否定候选。若语义核实后仍未找到且任务允许，改用普通网页搜索 '
                'https://www.google.com/ 检索原名称和paper，从实际结果打开官方项目页/论文页；'
                '否则报告尚未找到及已尝试的路径，不能断言论文不存在，也不要在两个耗尽入口之间循环。')

    def observe(self, obs, query):
        page = search_observation(obs, query)
        if page is None:
            return None
        if page['results_observed'] and page['submitted_query']:
            payload = [page['engine'], _key(query), _key(page['submitted_query']), page['filters'],
                       [(r['title'], r['href']) for r in page['results']]]
            fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
            if fingerprint not in self.seen:
                self.seen.add(fingerprint)
                key = self._scope(query, page)
                tried = self.attempts.setdefault(key, [])
                value = _key(page['submitted_query'])
                if value not in tried:
                    tried.append(value)
        return page

    def facts(self, obs, query):
        page = self.observe(obs, query)
        if page is None:
            return []
        control = page['control']
        value, submitted = control['value'], page['submitted_query']
        location = '搜索框 ref=' + str(control.get('ref'))
        if page['matches']:
            candidates = '；'.join('ref=' + str(r['ref']) + ' ' + r['title'][:180]
                                  for r in page['matches'][:3])
            return ['当前结果中有名称字面命中的候选：' + candidates
                    + '。字面命中只作检索提示；结合标题、摘要和项目说明作语义判断，其他实际结果也可打开详情检查。'
                    '不要重新输入相同查询；任何候选下载前仍须核实身份。']
        if page['form_changed']:
            return [location + '检索字段已改变，当前结果仍对应旧表单；直接点击同一搜索表单的 Search 提交，不要重填相同查询。']
        if page['sort_changed']:
            return ['排序控件已选择 Relevance，但结果 URL 仍对应旧排序；点击排序表单的 Go 提交，'
                    '不要重新选择 Relevance，也不要点击上方搜索表单的 Search。']
        if value and _key(value) != _key(submitted):
            # Repeated append corruption is observable; do not submit it.
            if _repeated_query_input(value, query):
                return [location + '出现重复追加的查询，使用 type(text=' + json.dumps(query, ensure_ascii=False)
                        + ', overwrite:true) 替换，再提交；输入值变化本身不证明检索有进展。']
            if _key(value) in self.attempts.get(self._scope(query, page), []):
                return ['框内查询在本入口和筛选条件下已尝试，不要重复提交相同查询。' + self._next_advice(page, query)]
            return [location + '已填写 ' + json.dumps(value, ensure_ascii=False)
                    + '，结果URL仍对应 ' + json.dumps(submitted, ensure_ascii=False)
                    + '；不要再次输入，直接 press Enter 提交（先确认焦点在搜索框），或点击同一搜索表单的提交按钮。']
        if page['results_observed'] and submitted:
            lead = (f'当前结果页读取 {len(page["results"])} 条标题，尚无名称字面命中；这不表示候选不相关，也不能证明论文不存在。'
                    '先结合标题、摘要和项目说明作语义判断，可直接打开相关候选详情。若没有合适候选，')
            if page['relevance_control'] and not page['relevance_submitted']:
                return [lead + '当前结果按日期排序，目标可能在后续页；可用排序控件 ref='
                        + str(page['relevance_control'].get('ref'))
                        + '选择当前真实选项 Relevance，再点击其排序表单的 Go；保持用户原名称，不猜作者或ID。']
            if page['next_result']:
                return [lead + '当前只是其中一页，可点击实际 Next 链接 ref='
                        + str(page['next_result']['ref']) + ' 读取后续结果（start='
                        + str(page['next_result']['start']) + '），或依据实际证据调整检索；不必为名称字面匹配读完所有页面。']
            lead += self._next_advice(page, query)
            if page['filters']:
                lead += '当前有筛选参数 ' + json.dumps(page['filters'], ensure_ascii=False) + '；检查年份、语言或排序是否排除了目标。'
            return [lead]
        return [location + self._next_advice(page, query)]

    def action_error(self, action, obs, query):
        page = search_observation(obs, query)
        if page is None:
            return ''
        value = page['control']['value']
        corrupted = _repeated_query_input(value, query)
        if page['sort_changed'] and (
                action.action == 'select' or _submits_search(action, page)
                or action.action == 'type' and _targets_control(action, page)):
            return ('Relevance 已选中但排序尚未提交；继续选择下拉框或修改搜索框不是提交。'
                    '请从当前 AX 找排序表单的 Go 按钮并点击，重新观察排序结果后再决定其他检索变化。')
        if corrupted and _submits_search(action, page):
            return ('搜索框出现重复追加的查询，不能提交损坏输入。先使用 type(text='
                    + json.dumps(query, ensure_ascii=False)
                    + ', overwrite:true) 清理为原名称，或覆盖为系统建议的新查询，再按当前检索反馈提交。')
        if action.action == 'type' and _targets_control(action, page) and isinstance(action.args.get('text'), str):
            text = action.args['text']
            if value and action.args.get('overwrite') is not True:
                return '搜索框已有内容；本次type会追加而不是替换查询。修改检索请填写完整新查询并设置overwrite:true；已填好时直接提交。'
            if _key(text) == _key(value):
                return '搜索框已经是本次要输入的查询，不再执行相同输入。' + ' '.join(self.facts(obs, query))
            unseen = _unobserved_query_terms(text, query, page)
            if unseen:
                return ('检索词加入了原名称和当前页面没有观察到的词：'
                        + json.dumps(sorted(unseen), ensure_ascii=False)
                        + '。不要凭模型常识补作者或论文ID；使用原名称、paper等中性扩展、'
                        '已观察到的完整标题或页面现有筛选/相关性排序，再提交。')
            if corrupted and _key(text) == _key(query) and not action.args.get('submit_key'):
                # Cleaning a damaged input does not grant permission to submit
                # an already exhausted query; the submission guard stays active.
                return ''
            tried = self.attempts.get(self._scope(query, page), [])
            if not page['form_changed'] and _key(text) in tried:
                return '同一入口和筛选条件下已提交过这个查询，不能重复绕回相同查询。' + ' '.join(self.facts(obs, query))
        tried = self.attempts.get(self._scope(query, page), [])
        redundant = (page['results_observed'] and page['submitted_query']
                     and _key(value) == _key(page['submitted_query'])) or _key(value) in tried
        if (not page['form_changed'] and redundant and _submits_search(action, page)):
            return '当前查询已提交并显示结果；重复提交不会改进检索，请判断现有候选或改进查询。' + ' '.join(self.facts(obs, query))
        return ''
