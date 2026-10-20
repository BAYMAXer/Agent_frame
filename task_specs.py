"""Conservative task typing and public workflow hints for small planners.

Intent hints do not grant completion authority. Unsupported requirements remain
visible even when no deterministic checker can compile the full instruction.
"""
from __future__ import annotations

import copy
import re

from .paper_sources import paper_search_instruction

TASK_ANALYSIS_VERSION = "intent-contract-v2"


def paper_download_intent(task):
    """Order-independent planning hint, deliberately separate from check parsing."""
    if not isinstance(task, str):
        return False
    # Negated/conditional requests need semantic interpretation, not a forced
    # download workflow. This is only a hint; it never creates a trusted check.
    if re.search(r'不要|无需|不用|禁止|不必|如果|若是|do not|don.t|without', task, re.I):
        return False
    return bool(re.search(r'论文|\bpaper\b', task, re.I) and
                re.search(r'下载|保存|\bdownload\b', task, re.I))


def infer_paper_goal(task: str):
    """Recognize an explicit, single-title paper download instruction only."""
    if not isinstance(task, str):
        return None
    patterns = (
        r"(?:请\s*)?下载\s*(?:一篇|1篇)?\s*(?:叫做|叫|名为|题为)\s*(?P<title>.+?)\s*的论文[。！]?",
        r"(?:请\s*)?下载\s*(?:一篇|1篇)?\s*论文\s*[《\"](?P<title>[^》\"]+)[》\"][。！]?",
        r"(?:please\s+)?download\s+(?:a\s+|the\s+)?paper\s+(?:called|named|titled)\s+[\"](?P<title>[^\"]+)[\"][.!]?",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, task.strip(), re.IGNORECASE)
        if not match:
            continue
        title = match.group("title").strip()
        quoted = title.startswith(("《", '"')) or pattern == patterns[1] or pattern == patterns[2]
        if title.startswith("《") and title.endswith("》"):
            title = title[1:-1].strip()
        elif title.startswith('"') and title.endswith('"'):
            title = title[1:-1].strip()
        # Unquoted task-like suffixes and list separators are ambiguous. Being
        # conservative is intentional: explicit trusted specs support all titles.
        if (not title or len(title) > 500 or re.search(r"[\n\r;；。！]|https?://", title)
                or re.search(r"(?:然后|并且|并下载|再下载|以及|两篇|三篇|\d+篇)", title)
                or (not quoted and re.search(r"(?:和|、|与)|\s+(?:and|then)\s+", title, re.I))
                or "《" in title or "》" in title or '"' in title):
            return None
        return {"kind": "paper_download", "title": title, "count": 1, "format": "pdf"}
    return None


def infer_paper_spec(task: str):
    """Compile a bounded paper/download/rename grammar without dropping tails.

    Old exact-title sentences retain their literal-title check. Shorthand or
    compound sentences become a query that needs an official identity binding.
    Anything left over is an explicit unresolved obligation, never success.
    """
    exact = infer_paper_goal(task)
    if exact:
        return {'checks': [exact]}
    if not isinstance(task, str) or not (re.search(r'论文', task) and re.search(r'下载|保存', task)):
        return None
    request = r'(?:请(?:你|帮我|帮忙)?\s*|帮我\s*)?'
    prefix = request + r'下载\s*(?:一篇|这篇|该)?\s*'
    search = request + r'(?:先\s*)?(?:搜索|检索|查找|寻找|找到)(?:一下)?\s*(?:一篇)?\s*'
    # Delimit quoted names before connectors: a connector inside a title must
    # never be consumed as an instruction. Fullmatch preserves all trailing goals.
    name = r'(?:《[^》]+》|"[^"]+"|[^《》"，,；;。！\n]+?)'
    download = r'\s*[，,]?\s*(?:并且|并|然后|再|后)?\s*下载(?:它|该论文|这篇论文|全文)?(?:下来)?'
    patterns = (
        search + r'论文\s*(?:叫做|叫|名为|题为)?\s*(?P<query>' + name + r')' + download + r'(?P<tail>.*)',
        search + r'(?:叫做|叫|名为|题为)?\s*(?P<query>' + name + r')\s*(?:的)?论文' + download + r'(?P<tail>.*)',
        request + r'(?:搜索|检索|查找)并下载\s*论文\s*(?P<query>' + name + r')(?P<tail>(?:并且|并|然后|再|[，,；;。]).*)?',
        prefix + r'(?:叫做|叫|名为|题为)?\s*(?P<query>.+?)\s*(?:这篇|的)?论文(?P<tail>.*)',
        prefix + r'论文\s*(?P<query>《[^》]+》|"[^"]+"|[^，,；;。!！]+?)(?P<tail>(?:并且|并|然后|再|[，,；;。]).*)?',
    )
    match = next((m for pattern in patterns if (m := re.fullmatch(pattern, task.strip()))), None)
    unresolved = lambda text: {'kind': 'unresolved_requirement', 'text': text}
    if not match:
        return {'checks': [unresolved(task.strip())]}
    query = match.group('query').strip()
    quoted = ((query.startswith('《') and query.endswith('》')) or
              (query.startswith('"') and query.endswith('"')))
    if quoted:
        query = query[1:-1].strip()
    if (not query or len(query) > 500 or re.search(r'两篇|三篇|\d+篇|https?://|[\n\r]', query)
            or (not quoted and re.search(r'和|与|、|以及|并|然后|不要|作者|最新|发表于|[《》"]', query))):
        return {'checks': [unresolved(task.strip())]}
    goal = {'kind': 'paper_download', 'query': query, 'count': 1, 'format': 'pdf'}
    tail = (match.group('tail') or '').strip().strip('。！.! ')
    if not tail:
        return {'checks': [goal]}
    remainder = re.sub(r'^[\s,，;；]*(?:并且|然后|并|再|后)?\s*', '', tail)
    # The browser session creates a dedicated child of the selected default
    # project folder. Keep this as an explicit artifact-path check, rather
    # than dropping it or leaving the otherwise completed task unresolved.
    if re.fullmatch(r'(?:PDF\s*)?到\s*默认(?:工程|工作|项目)(?:文件夹|目录)', remainder, re.I):
        goal['workspace'] = 'default'
        return {'checks': [goal]}
    rename = re.fullmatch(
        r'(?:(?:将|把)(?:下载的|下载后(?:的)?)?(?:论文|文件)(?:名)?\s*)?'
        r'(?:重命名|改名|命名|另存|保存)(?:为|成)\s*'
        r'(?P<name>《[^》]+》|"[^"]+"|[^，,；;。!！]+?)(?P<rest>(?:并且|并|然后|再|[，,；;。]).*)?', remainder)
    if not rename:
        rename = re.fullmatch(r'以\s*(?P<name>[^，,；;。!！]+?)\s*命名(?P<rest>.*)', remainder)
    checks = [goal]
    if rename:
        name = rename.group('name').strip().strip('《》"')
        if (name and len(name) <= 180 and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                and name not in ('.', '..')):
            # Supply the PDF extension only for a bare name. An explicit
            # extension is part of the user's filename constraint, even when
            # the actual downloaded document must still parse as PDF.
            goal['filename'] = name if '.' in name else name + '.pdf'
            rest = (rename.group('rest') or '').strip(' ,，;；。！.!')
            if rest:
                checks.append(unresolved(rest))
        else:
            checks.append(unresolved(tail))
    else:
        checks.append(unresolved(tail))
    return {'checks': checks}


def task_profile(task: str, public_spec=None):
    """Return only public goals and procedural hints, never evaluation answers."""
    checks = public_spec.get("checks", []) if isinstance(public_spec, dict) else []
    papers = [copy.deepcopy(c) for c in checks if isinstance(c, dict) and c.get("kind") == "paper_download"]
    if not papers and public_spec is None:
        inferred = infer_paper_spec(task)
        papers = [c for c in (inferred or {}).get('checks', []) if c['kind'] == 'paper_download']
    hinted = paper_download_intent(task)
    if not papers and not hinted:
        return {"kind": "generic", "goals": [], "workflow": []}
    return {
        "kind": "paper_download",
        "intent_source": "completion_contract" if papers else "task_hint",
        "contract_available": bool(papers),
        "goals": papers,
        "workflow": [
            {"phase": "search", "instruction": paper_search_instruction(task) + "确认浏览器可用；在符合来源策略的入口，先按用户原名称不加引号检索。名称可能是简称，不能要求完整标题只等于简称，也不默认使用精确短语搜索；只有观察到完整标题且需要缩小范围时才考虑双引号。找不到候选时按原名称加paper等中性词，仅在来源策略允许时更换入口；来源受限时报告当前来源受阻。作者与论文ID必须来自实际观察。已有搜索框只有符合来源策略或调用方显式预载且不与用户来源冲突时才复用；框内已填好则直接提交，不重复追加输入。界面语言不影响AX引用。若用户给出精确URL/ID，可直接打开原任务指定页面核对；限流时不要反复刷新。"},
            {"phase": "identify", "instruction": "由模型结合用户意图与实际观察到的标题、摘要、项目说明判断候选相关性；名称可能是简称、译名或主题描述，字面不相等不能拒绝打开详情。语义确认后由框架根据官方元数据绑定完整标题、作者、论文ID和下载链接；有歧义先补充观察，不能自行改验收目标。"},
            {"phase": "locate_fulltext", "instruction": "找到对应论文的 PDF 全文地址；不要将摘要页、引用记录、登录页当作全文。"},
            {"phase": "download", "instruction": "点击页面中已核对身份的 PDF/下载链接；查看器按钮不在 AX 时切到桌面截图定位。等待框架通过实际产物核实目标下载完成，不用截图猜测，也不重复下载。文件名不符时框架会安排后续界面改名步骤；禁止后台下载或直接改文件。"},
            {"phase": "verify", "instruction": "检查框架对实际文件、PDF 可解析性和论文身份的验收结果；未通过时根据证据继续处理。"},
        ],
        "interaction_guidance": "每步结合当前截图与 AX；优先使用 DOM 引用。被弹窗遮挡时先处理弹窗，再重新观察和定位；不要重复派发状态不明的操作。",
    }


def task_analysis(task, public_spec):
    """Public diagnostics; an intent hint is not evidence that a task is done."""
    checks = public_spec.get('checks', [])
    unresolved = [c['text'] for c in checks if c.get('kind') == 'unresolved_requirement']
    hard = [c for c in checks if c.get('kind') != 'unresolved_requirement']
    return {'version': TASK_ANALYSIS_VERSION, 'kind': task_profile(task, public_spec)['kind'],
            'contract_status': 'partial' if hard and unresolved else 'deterministic' if hard else 'semantic',
            'check_count': len(hard), 'unresolved_requirements': unresolved or ([task] if not checks else []),
            'original_task': task}


def audit_requirement_proposals(task, proposals):
    """Validate planner annotations without promoting them to trusted checks.

    Literal spans establish provenance, not semantic correctness or coverage.
    The whole original instruction remains a final-review obligation whenever
    deterministic compilation is incomplete. No model-supplied URL/ID is trusted.
    """
    accepted, rejected = [], []
    if proposals is None:
        proposals = []
    if not isinstance(proposals, list) or len(proposals) > 16:
        return {'accepted': [], 'rejected': [{'reason': 'requirements 必须为至多 16 项的数组'}],
                'coverage_verified': False, 'original_task': task}
    for item in proposals:
        if not isinstance(item, dict):
            rejected.append({'reason': '要求必须是对象'})
            continue
        kind, text = item.get('kind'), item.get('source_text')
        if (kind not in {'paper_download', 'rename', 'other'} or not isinstance(text, str)
                or not text.strip() or len(text) > 2000 or text not in task):
            rejected.append({'reason': '要求类型无效或 source_text 不是原始指令中的连续原文'})
            continue
        value = item.get('value')
        if kind != 'other' and (not isinstance(value, str) or not value.strip() or value not in text):
            rejected.append({'reason': '对象/文件名必须逐字出现在 source_text 中'})
            continue
        accepted.append({'kind': kind, 'source_text': text, 'source_start': task.index(text),
                         'source_end': task.index(text) + len(text),
                         'value': value if kind != 'other' else None, 'trusted': False})
    return {'accepted': accepted, 'rejected': rejected, 'coverage_verified': False, 'original_task': task}
