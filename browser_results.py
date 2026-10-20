"""Compact, evidence-linked paper result cards from a browser AX snapshot."""
from __future__ import annotations

import re

from .paper_resolver import arxiv_id_from_url


def paper_result_cards(block):
    """Group split AX text with the official links in its result list item.

    A card is an observation, not a paper identity binding. Refs remain tied to
    this snapshot and must be checked again before an action is dispatched.
    """
    nodes = [node for node in block.get('nodes', []) if isinstance(node, dict) and node.get('ref') is not None]
    children = {}
    for node in nodes:
        children.setdefault(node.get('parent'), []).append(node)

    def descendants(ref):
        pending = list(children.get(ref, ()))
        while pending:
            node = pending.pop(0)
            yield node
            pending.extend(children.get(node['ref'], ()))

    def text_under(ref):
        return ' '.join(''.join(str(node.get('name', '')) for node in descendants(ref)
                                if node.get('role') == 'StaticText').split())

    cards = []
    for item in nodes:
        if item.get('role') != 'listitem':
            continue
        subtree = list(descendants(item['ref']))
        abstract = next((node for node in subtree if node.get('role') == 'link'
                         and '/abs/' in str(node.get('href', ''))
                         and arxiv_id_from_url(node.get('href'))), None)
        if abstract is None:
            continue
        identifier = arxiv_id_from_url(abstract['href'])
        pdf = next((node for node in subtree if node.get('role') == 'link'
                    and '/pdf/' in str(node.get('href', ''))
                    and arxiv_id_from_url(node.get('href')) == identifier), None)
        paragraphs = [node for node in children.get(item['ref'], ()) if node.get('tag') == 'p']
        meta_ref = abstract.get('parent')
        title = ''
        authors = []
        for paragraph in paragraphs:
            if paragraph['ref'] == meta_ref:
                continue
            value = text_under(paragraph['ref'])
            if not value:
                continue
            if re.match(r'^Authors\s*:', value, re.I):
                authors = [node['name'] for node in descendants(paragraph['ref'])
                           if node.get('role') == 'link' and node.get('name')][:4]
            elif not title and not re.match(r'^(Abstract|Submitted|Comments|Journal)', value, re.I):
                title = value[:300]
        cards.append({'position': len(cards) + 1, 'title': title,
                      'authors': authors, 'abs_ref': abstract['ref'], 'abs_url': abstract['href'],
                      'pdf_ref': pdf['ref'] if pdf else None})
    return cards
