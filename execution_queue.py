"""Framework-owned sequential plan. Policy memory is never completion evidence."""
from __future__ import annotations

import copy
from urllib.parse import urlsplit

# Resource guard for pathological model output, not a requested plan length.
MAX_PLAN_STEPS = 32


def normalize_verification(value):
    """Normalize unambiguous planner aliases without weakening predicates.

    Scalar predicates may use ``value`` for their one operand. Element states
    have multiple operands, so guessing what ``value`` means is not allowed.
    The original sampled plan stays untouched for training and diagnosis.
    """
    if value is None:
        return [{"kind": "visual"}]
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("verification 必须含 1~8 项核查条件")
    allowed = {"files_equal", "file_exists", "file_absent", "file_content", "visual", "url_equals", "url_host", "text_present", "app_open", "app_foreground", "app_ready", "element_state", "download_complete", "download_valid", "artifact_download"}
    required = {"files_equal": "path", "file_exists": "path", "file_absent": "path", "file_content": "path", "url_equals": "url", "url_host": "host", "text_present": "text", "app_open": "app", "app_foreground": "app", "app_ready": "app", "element_state": "name", "download_complete": "check_id", "download_valid": "check_id"}
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in allowed:
            raise ValueError("CLI是执行动作，不是核查条件；请用 file_exists(path)/file_absent(path)/file_content(path,text)/files_equal(path,other_path) 或 visual" if isinstance(item, dict) and item.get("kind") == "cli" else "未知的步骤 verification.kind")
        kind = item["kind"]
        key = required.get(kind)
        if kind in {"url_equals", "url_host", "text_present", "app_open", "app_foreground", "app_ready"} and "value" in item:
            alias = item["value"]
            if not isinstance(alias, str) or not alias.strip() or len(alias) > 1000:
                raise ValueError(f"{kind}.value 别名需要非空字符串，对应规范字段 {key}")
            if key in item and (not isinstance(item[key], str) or item[key].strip() != alias.strip()):
                raise ValueError(f"{kind} 的 {key} 与 value 冲突，请只保留表达原意的 {key} 字段")
            item = {**item, key: alias}
        elif kind == "element_state" and "value" in item:
            raise ValueError("element_state 不支持通用 value；请明确 name 和 states，文本输入值不属于 states")
        if key and (not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 1000):
            raise ValueError(f"{kind} 需要非空 {key}")
        clean = {"kind": kind}
        if key:
            clean[key] = item[key].strip()
        if kind == "url_equals" and urlsplit(clean["url"]).scheme not in {"https", "http"}:
            raise ValueError("url_equals 需要完整 http(s) URL")
        if kind == "url_host" and ("/" in clean["host"] or ":" in clean["host"] or " " in clean["host"]):
            raise ValueError("url_host.host 只填写域名")
        if kind == "element_state":
            states = item.get("states")
            if (not isinstance(states, dict) or not states or
                    any(k not in {"checked", "selected", "expanded", "disabled", "focused"} or type(v) is not bool
                        for k, v in states.items())):
                raise ValueError("element_state.states 需要明确的布尔状态")
            clean["states"] = copy.deepcopy(states)
            if item.get("role"):
                clean["role"] = str(item["role"])[:80]
        if kind == 'files_equal':
            if not isinstance(item.get('other_path'), str) or not item['other_path'].strip() or len(item['other_path']) > 1000:
                raise ValueError('files_equal 需要 other_path')
            clean['other_path'] = item['other_path'].strip()
        if kind == 'file_content':
            if not isinstance(item.get('text'), str) or not item['text'] or len(item['text']) > 1000:
                raise ValueError('file_content 需要非空 text（最多1000字符）')
            clean['text'] = item['text']
        if kind == "artifact_download":
            fmt = str(item.get("format") or "any").lower()
            if fmt not in {"any", "pdf", "ppt"}:
                raise ValueError("artifact_download.format 仅支持 any/pdf/ppt")
            clean["format"] = fmt
            purpose = item.get("purpose", "new")
            if not isinstance(purpose, str) or purpose not in {"new", "recheck"}:
                raise ValueError("artifact_download.purpose 仅支持 new/recheck")
            if purpose == "recheck":
                clean["purpose"] = purpose
        result.append(clean)
    return result


def compile_download_milestones(steps, goals):
    """Separate durable download evidence from later filename acceptance.

    The sampled planner JSON remains in planning_calls; these are explicit
    framework-owned refinements of public paper requirements.
    """
    result, seen = [], set()
    by_id = {g['check_id']: g for g in goals}
    for original in steps:
        step = copy.deepcopy(original)
        if step.get('phase') != 'download':
            # A planner can accidentally attach the final paper artifact check
            # to search/identity discovery. That check cannot pass until after
            # download, so it must not block earlier milestones.
            if step.get('phase') in {'search', 'identify', 'locate_fulltext'}:
                step['check_ids'] = [cid for cid in step.get('check_ids', []) if cid not in by_id]
            if step.get('phase') == 'search' and len(by_id) == 1:
                # A submitted search changes the URL. A planner's exact
                # homepage URL or an echoed textbox value cannot verify it.
                # Require a query-bearing result page with an official paper
                # link whose visible title matches the requested query.
                query = str(next(iter(by_id.values())).get('query') or '').strip()
                if query:
                    host = next((urlsplit(v['url']).hostname for v in step.get('verification', [])
                                 if v.get('kind') == 'url_equals' and v.get('url')),
                                next((v.get('host') for v in step.get('verification', [])
                                      if v.get('kind') == 'url_host'), None))
                    step['verification'] = [{'kind': 'search_results', 'query': query,
                                             **({'host': host} if host else {})}]
                    step['framework_refinement'] = 'submitted_search_results'
            result.append(step)
            continue
        linked = [cid for cid in step.get('check_ids', []) if cid in by_id]
        selected = linked or list(by_id)
        if len(selected) != 1:
            raise ValueError('每个 download 子目标必须通过 check_ids 明确关联一个论文目标')
        cid = selected[0]
        if cid in seen:
            continue  # Multiple UI primitives for this transfer share one milestone.
        seen.add(cid)
        goal = by_id[cid]
        step.update(goal='下载目标论文 ' + str(goal.get('query') or goal.get('title')) + ' 的 PDF，等待框架确认文件落盘。',
                    expected_evidence='浏览器下载事件对应的文件已完整落盘；内容、身份与文件名在后续单独核查。',
                    verification=[{'kind': 'download_complete', 'check_id': cid}], check_ids=[],
                    framework_refinement='download_milestone')
        result.append(step)
        result.append({'phase': 'inspect_download', 'goal': '核对已收到文件是否为目标论文；若文件错误，按已核实官方链接重新通过界面获取正确文件。',
                       'expected_evidence': '框架核实 PDF 内容与目标论文身份；文件名暂不作为本步骤条件。',
                       'check_ids': [], 'verification': [{'kind': 'download_valid', 'check_id': cid}],
                       'actions': ['goto', 'click', 'press', 'hotkey', 'observe_browser', 'observe_desktop'],
                       'status': 'pending', 'framework_refinement': 'download_validation'})
        if goal.get('filename'):
            result.append({'phase': 'rename', 'goal': '将已核实的下载文件改名为 ' + goal['filename'] + '；不要再次下载。',
                           'expected_evidence': '同一已登记文件名称为 ' + goal['filename'] + '，内容及论文身份不变。',
                           'check_ids': [cid], 'verification': [], 'actions': ['cli', 'observe_desktop', 'hotkey', 'click', 'type', 'press'],
                           'status': 'pending', 'framework_refinement': 'filename_acceptance'})
    if len(result) > MAX_PLAN_STEPS:
        raise ValueError('下载与改名拆分后超出计划资源上限，请合并其他子目标')
    for index, step in enumerate(result, 1):
        step['idx'] = index
    return result


def refine_paper_plan(steps, goals):
    """Keep paper milestones grounded in the user's query, not model recall."""
    if len(goals) != 1:
        return steps
    query = str(goals[0].get('query') or goals[0].get('title') or '').strip()
    if not query:
        return steps
    replacements = {
        'search': (
            f'按用户给出的论文标题“{query}”检索并显示结果列表；优先使用页面的 Title/标题字段，结果不精确时根据已观察到的信息调整，不编造作者或论文 ID。',
            '已提交查询，页面显示论文结果列表；第一条无需是目标论文。'),
        'identify': (
            f'从搜索结果中寻找标题与“{query}”匹配的候选，打开官方论文页，读取页面实际给出的作者与论文标识；有歧义则继续检索。',
            '框架根据官方论文页元数据核实完整标题并绑定实际作者与标识，不预设这些值。'),
        'locate_fulltext': (
            '在已核实身份的官方论文页找到对应的 PDF 全文入口。',
            '页面提供与已核实论文身份一致的 PDF 链接。'),
        'verify': (
            '等待并核对框架对实际下载文件的最终验收结果。',
            '框架确认文件落盘、PDF 可解析且论文身份与原始任务一致。'),
    }
    result = []
    for original in steps:
        step = copy.deepcopy(original)
        if step.get('phase') in replacements:
            step['goal'], step['expected_evidence'] = replacements[step['phase']]
            # Search receives a structured result predicate later; identity,
            # full-text and final checks use observed metadata or artifacts.
            # None should depend on an invented planner predicate.
            step['verification'] = [{'kind': 'visual'}]
            step['framework_refinement'] = 'observed_paper_identity'
        elif step.get('phase') == 'download':
            # compile_download_milestones replaces this with a file event check.
            step['verification'] = [{'kind': 'visual'}]
        result.append(step)
    return result


class ExecutionQueue:
    def __init__(self, plan, revision=0, previous=None):
        self.plan = plan
        self.revision = revision
        self.cursor = 0
        for i, step in enumerate(plan["steps"]):
            step.update(id=f"p{revision}_{i + 1}", status="doing" if i == 0 else "pending")
        # Replanning describes the remaining work. Completed milestones keep
        # their stable identity and evidence; sampled planner output is retained
        # separately in planning_calls and is never rewritten for training.
        if previous is not None:
            completed = [copy.deepcopy(s) for s in previous.plan['steps'] if s['status'] == 'done']
            self.plan['steps'] = completed + self.plan['steps']
            self.cursor = len(completed)
            for i, step in enumerate(self.plan['steps'], 1):
                step['idx'] = i

    @property
    def current(self):
        steps = self.plan["steps"]
        return steps[self.cursor] if self.cursor < len(steps) else None

    def accept_review(self, report):
        step = self.current
        if step is None or report.get("plan_step_id") != step["id"]:
            return False
        step["last_review"] = copy.deepcopy(report)
        if report.get("status") != "passed":
            return False
        step["status"] = "done"
        self.cursor += 1
        if self.current:
            self.current["status"] = "doing"
        return True

    def window(self):
        steps = self.plan["steps"]
        def public(step):
            return {k: copy.deepcopy(step[k]) for k in (
                "id", "idx", "phase", "goal", "expected_evidence", "check_ids", "verification", "actions", "status") if k in step}
        return {"revision": self.revision, "cursor": self.cursor, "total": len(steps),
                "current": public(self.current) if self.current else None,
                "previous": public(steps[self.cursor - 1]) if self.cursor else None,
                "next": public(steps[self.cursor + 1]) if self.cursor + 1 < len(steps) else None,
                "all_steps_reviewed": self.current is None}

    def sync_memory(self, memory):
        memory.task_state["current_subgoal"] = self.current["goal"] if self.current else "核查整个任务的全部要求"
        memory.task_state["completed"] = [s["goal"] for s in self.plan["steps"] if s["status"] == "done"]
        memory.task_state["pending"] = [s["goal"] for s in self.plan["steps"][self.cursor:]]

    def snapshot(self):
        return {"revision": self.revision, "cursor": self.cursor, "total": len(self.plan["steps"]),
                "steps": [{k: copy.deepcopy(s[k]) for k in ("id", "idx", "goal", "status", "phase", "expected_evidence", "last_review") if k in s} for s in self.plan["steps"]]}
