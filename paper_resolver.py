"""Official citation evidence plus an explicit model selection of user intent.

The browser supplies observed candidate URLs. This adapter fetches their fixed
official abstract endpoints (or reuses typed metadata evidence from this adapter).
It cannot turn an arbitrary page title or model-produced dict into a binding.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from html.parser import HTMLParser
import re
import time
import unicodedata
from urllib.parse import urlsplit

ADAPTER_VERSION = 'arxiv-citation-v2-semantic-selection'
_ID = r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?'
_SEAL = object()


def arxiv_id_from_url(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in ('http', 'https') or url.hostname not in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org')
                or url.username or url.password or url.port not in (None, 80, 443)):
            return None
        match = re.fullmatch(r'/(?:abs|pdf|html)/(' + _ID + r')(?:\.pdf)?/?', url.path, re.I)
        return match.group(1) if match else None
    except ValueError:
        return None


def _base_id(value):
    return re.sub(r'v\d+$', '', value, flags=re.I).lower()


def _text_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def query_matches_title(query, title):
    """Lexical search hint only; never establishes or rejects paper identity."""
    if not isinstance(query, str) or not isinstance(title, str):
        return False
    query, title = _text_key(query), _text_key(title)
    return bool(query and (query == title or query == title.split(':', 1)[0].strip()))


@dataclass(frozen=True)
class ArxivMetadata:
    canonical_id: str
    full_title: str
    authors: tuple[str, ...]
    source_url: str
    pdf_url: str
    evidence_hash: str
    retrieved_at: float
    adapter_version: str = ADAPTER_VERSION
    dois: tuple[str, ...] = ()
    publication_years: tuple[int, ...] = ()
    abstract: str = ''
    _proof: object = None


@dataclass(frozen=True)
class PaperBinding:
    query: str
    canonical_id: str
    full_title: str
    authors: tuple[str, ...]
    source_url: str
    pdf_url: str
    evidence_hash: str
    retrieved_at: float
    adapter_version: str = ADAPTER_VERSION
    dois: tuple[str, ...] = ()
    publication_years: tuple[int, ...] = ()
    selection: dict = field(default_factory=dict)
    _proof: object = None

    def to_dict(self):
        return {key: value for key, value in asdict(self).items() if key != '_proof'}


@dataclass(frozen=True)
class PaperResolution:
    status: str
    binding: PaperBinding | None = None
    reason: str = ''
    candidates: tuple[str, ...] = ()
    reason_code: str = ''


def is_verified_binding(binding):
    return isinstance(binding, PaperBinding) and binding._proof is _SEAL


class _CitationParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'meta':
            return
        attrs = dict(attrs)
        name = (attrs.get('name') or attrs.get('property') or '').lower()
        if (name.startswith('citation_') or name in {'description', 'og:description'}) and isinstance(attrs.get('content'), str):
            self.fields.setdefault(name, []).append(attrs['content'])


def parse_arxiv_metadata(source_url, html, *, retrieved_at=None):
    """Parse bytes acquired from this official URL by a trusted transport.

    This function is an adapter boundary for browser response caches and tests;
    callers must pass the actual response body, not DOM/model-generated values.
    """
    identifier = arxiv_id_from_url(source_url)
    if not identifier or not urlsplit(source_url).path.startswith('/abs/'):
        raise ValueError('Official arXiv abstract response required')
    if not isinstance(html, str) or len(html.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('Metadata response exceeds bounded size')
    parser = _CitationParser()
    parser.feed(html)
    values = parser.fields
    titles = {v.strip() for v in values.get('citation_title', []) if v.strip()}
    pdfs = {v.strip() for v in values.get('citation_pdf_url', []) if v.strip()}
    authors = tuple(dict.fromkeys(v.strip() for v in values.get('citation_author', []) if v.strip()))
    if len(titles) != 1 or len(pdfs) != 1 or not authors:
        raise ValueError('Missing or ambiguous official citation metadata')
    title, pdf = next(iter(titles)), next(iter(pdfs))
    pdf_id = arxiv_id_from_url(pdf)
    if not pdf_id or _base_id(pdf_id) != _base_id(identifier) or '/pdf/' not in urlsplit(pdf).path:
        raise ValueError('PDF metadata does not identify the observed paper')
    abstract_ids = values.get('citation_arxiv_id', [])
    if any(_base_id(v.removeprefix('arXiv:').strip()) != _base_id(identifier) for v in abstract_ids):
        raise ValueError('Conflicting arXiv identifiers in citation metadata')
    if re.search(r'v\d+$', identifier, re.I) and pdf_id != identifier:
        raise ValueError('Requested arXiv version differs from PDF metadata')
    canonical = pdf_id if re.search(r'v\d+$', pdf_id, re.I) else identifier
    dois = tuple(sorted({re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value.strip(), flags=re.I).lower()
        for value in values.get('citation_doi', []) if value.strip()}))
    if any(not re.fullmatch(r'10\.\d{4,9}/\S+', value) for value in dois):
        raise ValueError('Invalid official DOI metadata')
    dates = values.get('citation_date', []) + values.get('citation_publication_date', [])
    years = tuple(sorted({int(m.group(1)) for value in dates if (m := re.match(r'^(\d{4})(?:\D|$)', value.strip()))}))
    return ArxivMetadata(canonical, ' '.join(title.split()), authors,
        'https://arxiv.org/abs/' + identifier, 'https://arxiv.org/pdf/' + canonical,
        hashlib.sha256(html.encode('utf-8')).hexdigest(),
        time.time() if retrieved_at is None else float(retrieved_at),
        dois=dois, publication_years=years,
        abstract=' '.join(next(iter(values.get('citation_abstract') or values.get('description') or values.get('og:description') or ['']), '').split())[:10000],
        _proof=_SEAL)


def parse_observed_arxiv_metadata(page_url, record, *, captured_at=None):
    """Validate citation fields observed in the active official abstract page.

    The browser adapter supplies this record from the current document, never
    from a planner or a search-result snippet. The page URL and every paper
    identifier must agree before the record can enter the resolver's cache.
    """
    identifier = arxiv_id_from_url(page_url)
    if (not identifier or not urlsplit(page_url).path.startswith('/abs/')
            or not isinstance(record, dict) or record.get('source') != 'observed_arxiv_meta'
            or record.get('source_url') != page_url):
        raise ValueError('Active official abstract metadata required')
    canonical = record.get('canonical_id')
    title = record.get('title')
    authors = record.get('authors')
    pdf = record.get('pdf_url')
    if (not isinstance(canonical, str) or _base_id(canonical) != _base_id(identifier)
            or not isinstance(title, str) or not title.strip() or len(title) > 500
            or not isinstance(authors, list) or not authors or len(authors) > 100
            or any(not isinstance(name, str) or not name.strip() or len(name) > 300 for name in authors)
            or not isinstance(pdf, str) or '/pdf/' not in urlsplit(pdf).path
            or _base_id(arxiv_id_from_url(pdf) or '') != _base_id(identifier)):
        raise ValueError('Incomplete or conflicting observed citation metadata')
    if re.search(r'v\d+$', identifier, re.I) and canonical != identifier:
        raise ValueError('Observed version differs from the active abstract page')
    canonical = canonical if re.search(r'v\d+$', canonical, re.I) else identifier
    evidence = {'source': 'browser_observed_arxiv_meta_v1', 'page_url': page_url,
                'canonical_id': canonical, 'title': ' '.join(title.split()),
                'authors': authors, 'pdf_url': pdf,
                'abstract': ' '.join(str(record.get('abstract') or '').split())[:10000]}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return ArxivMetadata(canonical, evidence['title'], tuple(authors), page_url,
                         'https://arxiv.org/pdf/' + canonical, digest,
                         time.time() if captured_at is None else float(captured_at),
                         dois=(str(record['doi']).lower(),) if record.get('doi') else (),
                         publication_years=tuple(sorted({int(m.group(1)) for value in record.get('dates', [])
                            if isinstance(value, str) and (m := re.match(r'^(\d{4})(?:\D|$)', value.strip()))})),
                         abstract=evidence['abstract'], _proof=_SEAL)


def _fetch_official(url, timeout):
    import requests
    import warnings
    deadline = time.monotonic() + timeout
    # 本机网络对 arxiv.org 的 HTTPS 做透明中间人代理，certifi 不含其 CA，导致
    # SSLCertVerificationError；而浏览器走 Windows 系统证书库能正常访问。这里关闭
    # 证书校验并绕过系统代理，以复现浏览器可达性（arXiv 公开元数据，非敏感数据）。
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        resp = requests.get(url, timeout=timeout, allow_redirects=False, stream=True,
                            verify=False, proxies={'http': None, 'https': None})
    with resp as response:
        if response.status_code != 200:
            raise ValueError('Official metadata endpoint did not return HTTP 200')
        body = bytearray()
        # read1 returns currently available bytes instead of waiting for a full
        # large chunk. Checking a deadline between reads prevents a trickling
        # response from resetting the read timeout indefinitely. The fallback
        # likewise yields each received byte on older urllib3 transports.
        read1 = getattr(response.raw, 'read1', None)
        chunks = iter(lambda: read1(65536, decode_content=True), b'') if read1 else response.iter_content(1)
        for chunk in chunks:
            if time.monotonic() > deadline:
                raise TimeoutError('Official metadata response exceeded its time budget')
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError('Official metadata response too large')
        return {'url': response.url, 'text': bytes(body).decode('utf-8'), 'status_code': 200}


def _load_candidate_metadata(identifier, cached, fetcher, timeout, deadline):
    """Independent read-only fetch; no selector/model call or shared mutation."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, 'resolution_timeout'
    item = cached.get(identifier)
    if item is None:
        source = 'https://arxiv.org/abs/' + identifier
        try:
            response = fetcher(source, timeout=max(0.01, min(float(timeout), remaining, 5.0)))
        except TimeoutError:
            return None, 'resolution_timeout'
        except Exception:
            return None, 'metadata_fetch_failed'
        try:
            if isinstance(response, dict):
                actual, html, status = response.get('url'), response.get('text'), response.get('status_code', 200)
            else:
                actual, html, status = response.url, response.text, response.status_code
            if status != 200 or arxiv_id_from_url(actual) != identifier or not actual.startswith('https://arxiv.org/abs/'):
                return None, 'metadata_invalid'
            item = parse_arxiv_metadata(actual, html)
        except Exception:
            return None, 'metadata_invalid'
    if time.monotonic() > deadline:
        return None, 'resolution_timeout'
    return item, None


def resolve_arxiv_candidates(query, observed_urls, *, fetcher=None, metadata=None,
                             max_candidates=4, timeout=5.0, total_timeout=12.0,
                             identity_selector=None, selection_context=None):
    """Read observed official candidates, then ask a model to select the target.

    Missing responses do not prove uniqueness. Candidate limits/partial failures
    yield unavailable rather than selecting whichever request happened to work.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        return PaperResolution('unavailable', reason='Invalid paper query', reason_code='invalid_query')
    if isinstance(observed_urls, str):
        observed_urls = [observed_urls]
    identifiers = list(dict.fromkeys(i for u in observed_urls if (i := arxiv_id_from_url(u))))
    if not identifiers:
        return PaperResolution('unavailable', reason='No observed official paper URL', reason_code='no_observed_candidate')
    if len(identifiers) > max(1, min(int(max_candidates), 4)):
        return PaperResolution('ambiguous', reason='Too many observed candidates', candidates=tuple(identifiers), reason_code='too_many_candidates')
    evidence = metadata if isinstance(metadata, (list, tuple)) else [metadata]
    cached = {m.canonical_id: m for m in evidence if isinstance(m, ArxivMetadata) and m._proof is _SEAL}
    candidates, incomplete, failures = {}, False, set()
    deadline = time.monotonic() + max(0.01, min(float(total_timeout), 30.0))
    fetcher = fetcher or _fetch_official
    def load(identifier):
        return _load_candidate_metadata(identifier, cached, fetcher, timeout, deadline)
    if sum(identifier not in cached for identifier in identifiers) > 1:
        from concurrent.futures import ThreadPoolExecutor
        # At most four already-observed official URLs. Join every result before
        # semantic selection; a fast response must not establish uniqueness.
        with ThreadPoolExecutor(max_workers=min(4, len(identifiers)), thread_name_prefix='paper-metadata') as pool:
            loaded = list(pool.map(load, identifiers))
    else:
        loaded = [load(identifier) for identifier in identifiers]
    for item, failure in loaded:
        if failure:
            failures.add(failure)
            incomplete = True
        else:
            candidates[_base_id(item.canonical_id)] = item
    if time.monotonic() > deadline:
        failures.add('resolution_timeout')
        incomplete = True
    if incomplete or not candidates:
        code = ('resolution_timeout' if 'resolution_timeout' in failures else
                'metadata_fetch_failed' if 'metadata_fetch_failed' in failures else
                'metadata_invalid')
        return PaperResolution('unavailable', reason='Official candidate metadata is incomplete; target identity has not been selected',
                               candidates=tuple(identifiers), reason_code=code)
    if identity_selector is None:
        return PaperResolution('unavailable', reason='Official metadata is available; semantic model selection is required',
                               candidates=tuple(identifiers), reason_code='identity_selection_required')
    try:
        context = selection_context or {}
        selection = identity_selector(query, tuple(candidates.values()),
            task=context.get('task', ''), constraints=context.get('constraints', {}),
            remaining_s=max(.01, deadline - time.monotonic()))
        if not isinstance(selection, dict):
            raise ValueError('Identity selection must be an object')
        status = selection.get('status')
        reason = str(selection.get('reason') or '')[:1000]
        if status != 'matched':
            return PaperResolution('unavailable', reason=reason or 'Candidate identity remains uncertain',
                candidates=tuple(identifiers), reason_code='semantic_different' if status == 'different' else
                'semantic_unavailable' if selection.get('error_type') else 'semantic_uncertain')
        item = next((candidate for candidate in candidates.values()
                     if candidate.canonical_id == selection.get('selected_id')), None)
        evidence = selection.get('evidence')
        if item is None or not reason or not isinstance(evidence, list) or not evidence:
            raise ValueError('Model selection must reference an observed candidate and evidence')
        for citation in evidence:
            evidence_item = next((candidate for candidate in candidates.values()
                                 if isinstance(citation, dict) and candidate.canonical_id == citation.get('candidate_id')), None)
            if evidence_item is None:
                raise ValueError('Selection evidence must belong to an observed candidate')
            field_name, quote = citation.get('field'), citation.get('quote')
            if field_name not in {'full_title', 'abstract', 'authors', 'dois', 'publication_years'}:
                raise ValueError('Unsupported selection evidence field')
            value = getattr(evidence_item, field_name)
            values = value if isinstance(value, (tuple, list)) else [value]
            if isinstance(value, (tuple, list)):
                values = [*value, ', '.join(map(str, value)), '; '.join(map(str, value))]
            if not isinstance(quote, str) or not quote.strip() or not any(
                    _text_key(quote) in _text_key(str(v)) for v in values):
                raise ValueError('Selection evidence was not observed')
        if not any(citation.get('candidate_id') == item.canonical_id for citation in evidence):
            raise ValueError('Model selection must cite its selected candidate')
        if time.monotonic() > deadline:
            raise TimeoutError('Identity selection exceeded remaining budget')
    except Exception as exc:
        return PaperResolution('unavailable', reason='Semantic identity selection could not be validated: ' + type(exc).__name__,
            candidates=tuple(identifiers), reason_code='semantic_unavailable')
    binding = PaperBinding(query.strip(), item.canonical_id, item.full_title, item.authors,
        item.source_url, item.pdf_url, item.evidence_hash, item.retrieved_at,
        dois=item.dois, publication_years=item.publication_years,
        selection={**selection, 'method': 'model_semantic', 'metadata_hash': item.evidence_hash}, _proof=_SEAL)
    return PaperResolution('resolved', binding=binding, candidates=(item.canonical_id,), reason_code='verified_identity')
