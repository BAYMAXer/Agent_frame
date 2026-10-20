"""Shared paper-search preference, subordinate to the original user's sources.

This chooses a search entry, never a paper identity, author, or download URL.
Ambiguous/unknown source instructions remain with the planner rather than being
silently overridden by the default.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

ARXIV_SEARCH_URL = 'https://arxiv.org/search/'
SOURCE_POLICY_VERSION = 'paper-sources-arxiv-first-v1'

_SOURCES = (
    ('scholar', r'谷歌学术|Google\s+Scholar|scholar\.google\.[a-z.]+', 'https://scholar.google.com/scholar?hl=en'),
    ('semantic_scholar', r'Semantic\s+Scholar|语义学者', 'https://www.semanticscholar.org/'),
    ('arxiv', r'arXiv', ARXIV_SEARCH_URL),
    ('google', r'谷歌(?!学术)|Google(?!\s+Scholar)', 'https://www.google.com/'),
    ('openreview', r'OpenReview', 'https://openreview.net/'),
    ('acl', r'ACL\s*Anthology|ACL论文库|ACL', 'https://aclanthology.org/'),
    ('pubmed', r'PubMed', 'https://pubmed.ncbi.nlm.nih.gov/'),
    ('ieee', r'IEEE(?:\s*Xplore)?', 'https://ieeexplore.ieee.org/'),
    ('nature', r'Nature(?:官网)?|自然期刊', 'https://www.nature.com/'),
    ('sciencedirect', r'ScienceDirect', 'https://www.sciencedirect.com/'),
    ('cnki', r'中国知网|知网|CNKI', 'https://www.cnki.net/'),
    ('wanfang', r'万方(?:数据)?', 'https://www.wanfangdata.com.cn/'),
)


def _exclusive(task):
    return bool(re.search(r'只(?:能)?(?:在|从|用|使用)|仅(?:限)?(?:在|从|用|使用)|不得切换|不要切换|'
                          r'\bonly\s+(?:on|from|use|using|via)|\bexclusively\b', task, re.I))


def _negated(text, start):
    prefix = text[max(0, start - 28):start]
    prefix = re.split(r'而是|改用|转而|而使用|而用|\binstead\s+(?:use|using)|\bbut\s+(?:use|using)', prefix, flags=re.I)[-1]
    return bool(re.search(r'(?:不要|不用|别|不去|禁止|避免|而非|不是|不使用|勿|不在|不从|不用去)'
                          r'[^，,。；;\n]{0,10}$|(?:do\s+not|don.t|avoid|not)\s+(?:use\s+|using\s+|on\s+)?$',
                          prefix, re.I))


def paper_search_policy(task):
    """Return a preference without manufacturing an unmentioned destination."""
    task = task if isinstance(task, str) else ''
    # A title about Google Scholar is not a request to search on Google Scholar.
    source_text = re.sub(r'《[^》]*》|"[^"\n]*"|“[^”]*”', lambda m: ' ' * len(m.group()), task)
    urls = []
    for match in re.finditer(r'https?://[^\s<>"“”《》，。；！\u4e00-\u9fff]+', task, re.I):
        url = match.group().rstrip('.,;。！!，；、)）]】')
        if not _negated(task, match.start()) and urlsplit(url).hostname:
            urls.append(url)
    if urls:
        url = urls[0]
        host = (urlsplit(url).hostname or '').lower()
        source = ('arxiv' if host in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'} else
                  'scholar' if host.startswith('scholar.google.') else 'specified')
        return {'preferred_source': source, 'search_url': url, 'explicit': True,
                'exclusive': _exclusive(task), 'reason': '原始任务明确提供来源网址；按用户网址和顺序执行'}

    choices = []
    for source, pattern, url in _SOURCES:
        for match in re.finditer(pattern, source_text, re.I):
            if _negated(source_text, match.start()):
                continue
            before, after = source_text[max(0, match.start() - 24):match.start()], source_text[match.end():match.end() + 24]
            located = (re.search(r'(?:在|从|用|使用|通过|到|去|优先|访问|打开|来源(?:为|是)?|指定)\s*$', before)
                       or re.search(r'\b(?:on|from|using|via|use|visit|open|search)\s+(?:the\s+)?$', before, re.I)
                       or re.match(r'\s*(?:上|官网|网站|平台|搜索|检索|查找|下载)', after)
                       or re.search(r'\b(?:search|find|download)\s+(?:the\s+)?(?:paper\s+)?(?:on|from|using|via)\s*$', before, re.I))
            if located:
                choices.append((match.start(), source, url))
    if choices:
        _, source, url = min(choices)
        return {'preferred_source': source, 'search_url': url, 'explicit': True,
                'exclusive': _exclusive(task), 'reason': '原始任务明确指定检索或下载来源，优先于框架默认入口'}

    if re.search(r'(?:在|从|通过|使用)[^，,。；;\n]{1,50}(?:官网|网站|平台|数据库|出版社)|'
                 r'(?:在|从|通过|使用|去|到)\s*[A-Za-z][A-Za-z0-9 ._-]{1,45}\s*(?:上|搜索|检索|查找|下载)|'
                 r'官方(?:网站|项目页)|出版社(?:网站|官网)|\b(?:on|from|using)\s+the\s+[^\n,.]{1,40}\b(?:website|publisher|repository)\b',
                 source_text, re.I):
        return {'preferred_source': 'specified', 'search_url': None, 'explicit': True,
                'exclusive': _exclusive(task), 'reason': '任务包含其他来源要求，由规划模型依据原文选择入口'}

    if re.search(r'(?:不要|不用|不在|不从|避免|禁止|勿)[^，,。；;\n]{0,10}arxiv|'
                 r'(?:do\s+not|don.t|avoid|not)\s+(?:use\s+|using\s+|on\s+)?arxiv', source_text, re.I):
        return {'preferred_source': 'specified', 'search_url': None, 'explicit': True,
                'exclusive': False, 'reason': '用户排除 arXiv，不能使用默认入口'}
    return {'preferred_source': 'arxiv', 'search_url': ARXIV_SEARCH_URL, 'explicit': False,
            'exclusive': False, 'reason': '论文任务未指定来源，默认优先在 arXiv 检索'}


def paper_search_instruction(task):
    policy = paper_search_policy(task)
    if policy['explicit']:
        text = '论文来源策略：用户明确指定或限制了来源，按原始任务要求执行，用户要求优先于默认 arXiv。'
        if policy['search_url']:
            text += '已识别的用户来源入口：' + policy['search_url'] + '。'
        text += '不得把用户指定的其他来源擅自换成 arXiv。'
    else:
        text = ('论文来源策略：未指定来源时，默认优先使用 arXiv 搜索入口 ' + ARXIV_SEARCH_URL
                + '，先在搜索框按用户原名称检索，再从实际结果确认候选；不要先去 Google Scholar。')
    if policy['exclusive']:
        text += '用户限制了来源范围；即使检索受阻也不得自行换站，应说明障碍并保留原要求。'
    else:
        text += ('首选入口不可访问、限流，或检索并语义核实候选后仍无合适结果时，可在任务允许范围内换搜索入口；'
                 '不要仅因当前第一页没有字面命中就放弃。')
    return text + ('来源入口不代表论文身份，不得据此编造作者、论文ID或具体PDF地址。'
                   '此策略只用于选择检索入口；已核实目标论文后沿已确认的全文链接继续，不要返回搜索首页重来。')


def default_paper_start_url(task, *, is_paper_download, browser_start_url=None):
    """Explicit initial environments win; only unspecified paper tasks get arXiv."""
    if browser_start_url is not None or not is_paper_download:
        return browser_start_url
    policy = paper_search_policy(task)
    return None if policy['explicit'] else ARXIV_SEARCH_URL
