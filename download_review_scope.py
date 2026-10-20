"""Scope an already accepted semantic review to an immutable named download.

This never infers a completion contract or declares success. It only prevents
UI-only changes from invalidating an accepted whole-task review whose sole
remaining durable result is the exact named file. Unknown/compound instructions
retain normal screenshot-based revalidation.
"""
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit


def named_download_receipts(task, facts):
    direct = re.fullmatch(r'\s*(?:请\s*)?下载\s+(?P<name>[^\n]+?)\s*[。]?\s*', task)
    portal = re.fullmatch(
        r'\s*(?:请\s*)?打开\s*(?P<url>https?://[^\s，,]+)\s*[，,]\s*'
        r'(?:去|到|进入)(?P<region>[^，,；;。\n]+?)(?:中|里)下载\s+(?P<name>[^\n]+?)\s*[。]?\s*', task)
    match = portal or direct
    if not match:
        return []
    name = match['name'].strip().strip('“”"《》')
    # Conservative allowlist of instruction shapes, not an NLP success rule.
    if (not name or re.search(r'[，,；;。\n/\\]|然后|并且|同时|并|改名|重命名|保存到|上传|发送|提交|审批|填写|筛选|之后|以后|最新|所有|全部|以及|和|与|\b(?:and|then|rename|save|upload|send|all|latest)\b', name, re.I)
            or (portal and re.search(r'填写|提交|审批|筛选|修改|删除|上传|登录|然后|并', portal['region']))):
        return []
    host = urlsplit(portal['url']).hostname if portal else None
    selected = []
    for fact in facts:
        if fact.get('status') != 'passed' or not fact.get('sha256'):
            continue
        filename = PurePosixPath(str(fact.get('path', '')).replace('\\', '/')).name
        # The name comes from the user, never the planner. No substring matches.
        if name not in {filename, PurePosixPath(filename).stem}:
            continue
        if host and not any(urlsplit(u).hostname == host for u in fact.get('source_urls', [])):
            continue
        selected.append(fact['artifact_id'])
    return selected
