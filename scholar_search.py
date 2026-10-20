"""Google Scholar URL and result-title facts derived from the current page."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .paper_resolver import query_matches_title


def is_scholar(url):
    try:
        return urlsplit(str(url)).hostname == 'scholar.google.com'
    except ValueError:
        return False


def english_scholar_url(url):
    """Keep the observed destination and query, changing only Scholar's UI locale."""
    if not is_scholar(url):
        return url
    parsed = urlsplit(url)
    params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
              if key.lower() != 'hl']
    params.append(('hl', 'en'))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(params), parsed.fragment))


def scholar_title_links(block):
    """Only result heading links; sidebar and citation controls are not titles."""
    nodes = [node for node in block.get('nodes', []) if isinstance(node, dict)]
    by_ref = {str(node['ref']): node for node in nodes if node.get('ref') is not None}
    result = []
    for node in nodes:
        if str(node.get('role', '')).casefold() != 'link' or not node.get('href'):
            continue
        parent = by_ref.get(str(node.get('parent')))
        if parent is None or (str(parent.get('tag', '')).casefold() != 'h3'
                              and str(parent.get('role', '')).casefold() != 'heading'):
            continue
        title = ' '.join(str(node.get('name') or parent.get('name') or '').split())
        if title:
            result.append({'ref': str(node.get('ref')), 'title': title,
                           'href': str(node['href'])})
    return result


def exact_scholar_titles(block, query):
    return [item for item in scholar_title_links(block)
            if query_matches_title(query, item['title'])]


def scholar_result_cards(block):
    """Associate PDFs only within a result container holding one heading title.

    A shared page/list root is never enough to pair a PDF with a title. When AX
    omits the result wrapper, retain its title link and leave PDFs unassociated.
    """
    nodes = [node for node in block.get('nodes', []) if isinstance(node, dict)]
    by_ref = {str(node['ref']): node for node in nodes if node.get('ref') is not None}
    titles = scholar_title_links(block)
    title_refs = {item['ref'] for item in titles}

    def under(node, ancestor):
        current, seen = str(node.get('ref')), set()
        while current in by_ref and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = str(by_ref[current].get('parent'))
        return False

    results = []
    for title in titles:
        container = str(by_ref[title['ref']].get('parent'))
        chosen, seen = None, set()
        while container in by_ref and container not in seen:
            seen.add(container)
            node = by_ref[container]
            if node.get('role') in {'RootWebArea', 'main', 'navigation', 'banner'} or node.get('tag') in {'#document', 'main'}:
                break
            members = [ref for ref in title_refs if under(by_ref[ref], container)]
            if len(members) != 1:
                break
            chosen = container
            # Scholar marks each result with data-cid/gs_r in the DOM; AX may
            # retain a listitem wrapper instead. Do not climb past that row.
            if node.get('role') == 'listitem' or str(node.get('id') or '').startswith('gs_r_'):
                break
            container = str(node.get('parent'))
        links = []
        for node in nodes:
            href, label = str(node.get('href') or ''), str(node.get('name') or '')
            if (str(node.get('ref')) == title['ref'] or node.get('role') != 'link'
                    or not href or chosen is None or not under(node, chosen)):
                continue
            try:
                path = urlsplit(href).path.casefold()
            except ValueError:
                continue
            if '/pdf/' in path or path.endswith('.pdf') or '[pdf]' in label.casefold() or label.strip().casefold() == 'pdf':
                # Even a malformed same-row link cannot override a known paper ID.
                from .paper_resolver import arxiv_id_from_url
                expected, actual = arxiv_id_from_url(title['href']), arxiv_id_from_url(href)
                if expected and actual and expected != actual:
                    continue
                links.append({'ref': str(node.get('ref')), 'href': href, 'label': label})
        results.append({**title, 'container_ref': chosen if chosen in by_ref else None, 'pdf_links': links})
    return results
