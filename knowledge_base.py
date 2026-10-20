"""User-maintained planning references: bounded, deterministic local retrieval."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent / 'knowledge' / 'user_knowledge.md'
MAX_BYTES = 2_000_000
_lock = threading.RLock()


def knowledge_path():
    return Path(os.environ.get('AGENT_KNOWLEDGE_PATH') or DEFAULT_PATH).expanduser().resolve()


def validate_document(doc):
    if not isinstance(doc, dict) or doc.get('version') != 1 or not isinstance(doc.get('entries'), list):
        raise ValueError('知识库需要 version: 1 和 entries 列表')
    if len(doc['entries']) > 2000:
        raise ValueError('最多 2000 条知识；请拆分过大的资料')
    clean, ids = [], set()
    for item in doc['entries']:
        if not isinstance(item, dict):
            raise ValueError('每条知识必须是对象')
        unknown = set(item) - {'id', 'title', 'aliases', 'keywords', 'url', 'content', 'enabled'}
        if unknown:
            raise ValueError('不支持的知识字段：' + ', '.join(sorted(unknown)))
        row = {}
        for key, limit in [('id', 80), ('title', 160), ('url', 1000), ('content', 3000)]:
            value = item.get(key, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'{key} 必须是字符串，最多 {limit} 字符')
            row[key] = value.strip()
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', row['id']) or row['id'] in ids:
            raise ValueError('id 需唯一，且只含字母、数字、下划线或连字符')
        ids.add(row['id'])
        if not row['title'] or not (row['content'] or row['url']):
            raise ValueError('知识需要标题，以及网址或内容')
        if row['url']:
            url = urlsplit(row['url'])
            if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
                raise ValueError('网址必须是无用户名密码的 http(s) URL')
        for key in ('aliases', 'keywords'):
            words = item.get(key, [])
            if not isinstance(words, list) or len(words) > 30 or any(
                    not isinstance(w, str) or not 2 <= len(w.strip()) <= 100 for w in words):
                raise ValueError(f'{key} 最多 30 项，每项 2–100 字符')
            row[key] = list(dict.fromkeys(w.strip() for w in words))
        row['enabled'] = item.get('enabled', True)
        if type(row['enabled']) is not bool:
            raise ValueError('enabled 必须为 true 或 false')
        clean.append(row)
    return {'version': 1, 'entries': clean}


ENTRY_SEPARATOR = '\n<!-- knowledge-entry -->\n'


def entry_markdown(entry):
    if ENTRY_SEPARATOR in '\n' + entry['content'] + '\n':
        raise ValueError('正文不能包含知识条目分隔符 <!-- knowledge-entry -->')
    meta = {key: entry[key] for key in ('id', 'title', 'aliases', 'keywords', 'url', 'enabled')}
    return '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip() + '\n---\n\n' + entry['content'] + '\n'


def parse_entry_markdown(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Markdown 内容无效或超过大小限制')
    match = re.fullmatch(r'---\s*\n(.*?)\n---[ \t]*\n(.*)', text.strip() + '\n', re.S)
    if not match:
        raise ValueError('每条知识需以 --- 包围的 YAML 元数据开头，随后填写 Markdown 正文')
    try:
        meta = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError('知识元数据 YAML 格式无效') from exc
    if not isinstance(meta, dict) or 'content' in meta:
        raise ValueError('元数据必须是字段对象；正文请写在第二个 --- 后')
    return validate_document({'version': 1, 'entries': [{**meta, 'content': match[2].strip()}]})['entries'][0]


def document_markdown(doc):
    return ENTRY_SEPARATOR.join(entry_markdown(e) for e in doc['entries'])


def parse_markdown_document(text):
    entries = [parse_entry_markdown(part) for part in text.split(ENTRY_SEPARATOR) if part.strip()]
    return validate_document({'version': 1, 'entries': entries})


def load_knowledge(path=None):
    path = Path(path) if path else knowledge_path()
    if path.exists() and path.stat().st_size > MAX_BYTES:
        raise ValueError('知识文件超过 2 MB')
    source = path
    if path == DEFAULT_PATH and not path.exists() and path.with_suffix('.yaml').exists():
        source = path.with_suffix('.yaml')  # read legacy data until the first Markdown save
    raw = source.read_bytes() if source.exists() else (b'' if path.suffix == '.md' else b'version: 1\nentries: []\n')
    if len(raw) > MAX_BYTES:
        raise ValueError('知识文件超过 2 MB')
    try:
        doc = parse_markdown_document(raw.decode('utf-8')) if source.suffix == '.md' else validate_document(yaml.safe_load(raw))
    except yaml.YAMLError as exc:
        raise ValueError('知识库 YAML 格式无效，请检查缩进和字段') from exc
    return {'document': doc, 'revision': hashlib.sha256(raw).hexdigest(), 'path': str(path), 'markdown': document_markdown(doc),
            'entry_markdown': [entry_markdown(e) for e in doc['entries']]}


def save_knowledge(document, revision, path=None):
    path = Path(path) if path else knowledge_path()
    doc = validate_document(document)
    raw = (document_markdown(doc) if path.suffix == '.md' else yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('知识文件超过 2 MB')
    with _lock:
        if load_knowledge(path)['revision'] != revision:
            raise FileExistsError('知识库已被其他页面或编辑器修改，请重新加载后合并')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.knowledge-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return load_knowledge(path)


def _matches(term, text):
    term = term.casefold()
    # English acronyms must not match inside another word; Chinese aliases
    # naturally appear without whitespace in task descriptions.
    if re.fullmatch(r'[\x00-\x7f]+', term):
        return bool(re.search(r'(?<![a-z0-9_])' + re.escape(term) + r'(?![a-z0-9_])', text))
    return term in text


def retrieve_knowledge(task, snapshot, *, max_entries=4, max_chars=6000):
    task = str(task)[:20000].casefold()
    ranked = []
    for item in snapshot['document']['entries']:
        if not item['enabled']:
            continue
        aliases = [w for w in dict.fromkeys([item['title'], *item['aliases']]) if len(w) >= 2 and _matches(w, task)]
        keywords = [w for w in item['keywords'] if _matches(w, task)]
        host = urlsplit(item['url']).hostname if item['url'] else None
        host_hit = bool(host and _matches(host, task))
        if not (aliases or host_hit or len(keywords) >= 2):
            continue
        score = 10 * bool(aliases) + 15 * host_hit + min(len(keywords), 5) * 2
        ranked.append((score, item, aliases + keywords + ([host] if host_hit else [])))
    ranked.sort(key=lambda x: (-x[0], x[1]['id']))
    term_urls = {}
    for _, item, matches in ranked:
        for term in matches:
            if term in [item['title'], *item['aliases']] and item['url']:
                term_urls.setdefault(term, set()).add(item['url'])
    conflicts = [term for term, urls in term_urls.items() if len(urls) > 1]
    entries, used = [], 0
    for score, item, matches in ranked[:max_entries]:
        entry = {k: item[k] for k in ('id', 'title', 'url', 'content')}
        entry.update(score=score, matched_terms=matches)
        size = len(json.dumps(entry, ensure_ascii=False))
        if used + size > max_chars:
            # Keep entry boundaries; do not truncate a domain's restrictions.
            continue
        entries.append(entry)
        used += size
    return {'revision': snapshot['revision'], 'entries': entries, 'matched_count': len(ranked),
            'selected_chars': used, 'limit_entries': max_entries, 'limit_chars': max_chars,
            'method': 'alias_keyword_v1', 'ambiguous_terms': conflicts[:10]}
