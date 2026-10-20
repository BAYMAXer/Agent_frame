"""Local title-search rehearsal with a real arXiv handoff.

The page is deliberately branded as a replay. It cannot be mistaken for a
successful request to arxiv.org/search, which currently rate-limits this host.
"""
from __future__ import annotations

from html import escape
import re

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

router = APIRouter(prefix='/demo/paper-search', tags=['presentation rehearsal'])

TITLE = 'Attention Is All You Need'
OFFICIAL_URL = 'https://export.arxiv.org/abs/1706.03762'
SURVEY_URL = '/demo/paper-search/survey'
PAPERS = (
    {'title': TITLE, 'url': OFFICIAL_URL,
     'authors': 'Ashish Vaswani, Noam Shazeer, Niki Parmar et al.',
     'identifier': '1706.03762', 'year': '2017',
     'related': 'A Survey of Attention Mechanisms'},
    {'title': 'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
     'url': 'https://export.arxiv.org/abs/1810.04805',
     'authors': 'Jacob Devlin, Ming-Wei Chang, Kenton Lee, Kristina Toutanova',
     'identifier': '1810.04805', 'year': '2018',
     'related': 'A Demonstration Survey of BERT Applications'},
    {'title': 'Deep Residual Learning for Image Recognition',
     'url': 'https://export.arxiv.org/abs/1512.03385',
     'authors': 'Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun',
     'identifier': '1512.03385', 'year': '2015',
     'related': 'A Demonstration Survey of Residual Networks'},
)


def _title_words(text: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', text.casefold()))


def _matching_paper(query: str):
    words = _title_words(query)
    return next((paper for paper in PAPERS if _title_words(paper['title']) in words), None)


def _page(content: str) -> HTMLResponse:
    return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>arXiv title-search rehearsal</title><style>
body{margin:0;font:16px/1.55 Arial,sans-serif;background:#f7f8fa;color:#202a33}
header{background:#b31b1b;color:white;padding:22px max(6vw,24px)}
header strong{font-size:30px;letter-spacing:.03em}header span{margin-left:18px;font-size:15px}
.notice{background:#fff3d5;border-bottom:1px solid #e8d5a3;padding:12px max(6vw,24px);font-size:14px}
main{max-width:1050px;margin:38px auto;padding:0 24px}h1{font-size:31px;margin:0 0 6px}
p{color:#52606c}.search{display:flex;gap:10px;margin:26px 0 35px}
input{flex:1;min-width:0;padding:14px 16px;border:1px solid #8a9aa8;border-radius:4px;font:18px Arial}
button{background:#176c98;border:0;border-radius:4px;color:white;padding:13px 28px;font-size:17px;cursor:pointer}
.result{background:#fff;border:1px solid #dde2e6;border-left:5px solid #aab8c0;margin:15px 0;padding:18px 24px}
.result.correct{border-left-color:#207555}.result a{font-size:22px;font-weight:700;color:#145c93}
.meta{font-size:14px;color:#586978;margin:6px 0}.tag{font-size:12px;padding:3px 7px;border:1px solid #8ca7a0;border-radius:3px}
</style></head><body><header><strong>arXiv</strong><span>Title search rehearsal</span></header>
<div class="notice"><strong>LOCAL REPLAY · NOT LIVE arxiv.org SEARCH</strong> — Search interactions and results are controlled fixtures. Paper details and PDF open on the official arXiv export mirror.</div>
<main>''' + content + '</main></body></html>')


@router.get('')
@router.get('/')
def search(query: str = Query(default='', max_length=200)):
    safe_query = escape(query, quote=True)
    content = (f'<h1>Search arXiv by paper title</h1><p>Type the paper name, submit, then verify the result title and authors.</p>'
               f'<form class="search" action="/demo/paper-search" method="get">'
               f'<input name="query" type="search" aria-label="Search paper title" value="{safe_query}" placeholder="Paper title">'
               f'<button type="submit">Search</button></form>')
    if query.strip():
        if paper := _matching_paper(query):
            related = escape(paper['related'])
            title = escape(paper['title'])
            url = escape(paper['url'], quote=True)
            authors = escape(paper['authors'])
            content += (f'<h2>Search results for: {safe_query}</h2>'
                '<article class="result"><span class="tag">Related demonstration result</span><div>'
                f'<a href="{SURVEY_URL}">{related}</a></div>'
                '<p class="meta">Local distractor · not an official paper record</p></article>'
                '<article class="result correct"><span class="tag">Paper record</span><div>'
                f'<a href="{url}">{title}</a></div>'
                f'<p class="meta">{authors} · arXiv:{paper["identifier"]} · {paper["year"]}</p>'
                '<p>Open the official arXiv mirror to inspect the paper and download its PDF.</p></article>')
        else:
            content += f'<h2>Search results for: {safe_query}</h2><p>No matching paper in this bounded rehearsal fixture.</p>'
    return _page(content)


@router.get('/survey')
def survey():
    return _page('<h1>A Survey of Attention Mechanisms</h1><p>Jane Researcher · related survey</p>'
                 '<p>This survey cites Attention Is All You Need. It is not the requested original paper.</p>'
                 '<a href="/demo/paper-search?query=Attention+Is+All+You+Need">Return to search results</a>')
