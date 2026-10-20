"""Pure compilation of planner proposals into the existing execution-plan schema.

Model calls and retry feedback live in planning_service; Pipeline retains
queue installation and event ordering.
This module only applies the same validation and refinements to a sampled plan;
planner annotations cannot create or weaken a trusted completion contract.
"""
from __future__ import annotations

import copy
import re

from .actions import BASIC_ACTIONS
from .execution_queue import (
    MAX_PLAN_STEPS,
    compile_download_milestones,
    normalize_verification,
    refine_paper_plan,
)
from .task_specs import audit_requirement_proposals


def normalize_plan_actions(names):
    aliases = {"open_url": "goto", "navigate": "goto", "key_press": "press", "press_key": "press",
               "input_text": "type", "type_text": "type"}
    if isinstance(names, list):
        names = [aliases.get(n, n) if isinstance(n, str) else n for n in names]
    if not isinstance(names, list) or any(not isinstance(n, str) or n not in BASIC_ACTIONS for n in names):
        raise ValueError("actions 只能包含已注册的 GUI/DOM/CLI 动作")
    return list(dict.fromkeys(names))


def _download_recheck(goal):
    """Recognize inspection of a received file, never another acquisition.

    The predicate certifies transport/integrity only. It does not establish
    business identity, and its evaluator still requires a previously accepted
    artifact from this run. Sampled predicate IDs/purposes are not authority.
    """
    text = str(goal).strip()
    checking = re.match(r"^(?:请\s*)?(?:检查|核验|核查|核对|确认|验证|验收|"
                        r"check\b|verify\b|validate\b|confirm\b|inspect\b)", text, re.I)
    downloading = re.search(r"下载|download|已收到.*文件|已保存.*文件|(?:文件|PDF|PPTX?).*已(?:保存|收到)", text, re.I)
    acquiring = re.search(r"重新下载|再次下载|继续下载|下载\s*(?:另|第[二三四五六七八九十\d]|下一个)|"
                          r"(?:另一个|第二个|下一个).*下载|re-?download|"
                          r"download\s+(?:another|a\s+second|the\s+next|a\s+new)", text, re.I)
    return bool(checking and downloading and not acquiring)


def compile_plan(obj, task, public_spec, task_profile, paper_goals, *, repair=False):
    """Return a validated plan without model calls, events or state installation.

    All inputs come from the caller's current run. In particular, public_spec
    and paper_goals are framework-owned evidence, never planner-created values.
    Existing execution_queue refinements and error wording are preserved.
    """
    check_ids = {c["id"] for c in public_spec.get("checks", [])}
    steps = obj.get("steps") if isinstance(obj, dict) else None
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_PLAN_STEPS:
        raise ValueError(f"steps 必须是非空子目标列表；资源上限为 {MAX_PLAN_STEPS} 项，请合并同一目标的零碎动作")
    # Refine before predicate validation, just as in the existing planner path.
    if task_profile.get("kind") == "paper_download" and len(paper_goals) == 1:
        steps = refine_paper_plan(steps, paper_goals)
    normalized = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("goal"), str) or not step["goal"].strip():
            raise ValueError("每一步需要非空 goal")
        if not isinstance(step.get("expected_evidence"), str) or not step["expected_evidence"].strip():
            raise ValueError("每一步必须给出非空 expected_evidence，说明实际可以观察到的结果")
        ids = step.get("check_ids", [])
        if not check_ids and isinstance(ids, list):
            # Hallucinated IDs on generic tasks do not create a contract.
            ids = []
        if not isinstance(ids, list) or any(not isinstance(x, str) or x not in check_ids for x in ids):
            raise ValueError("check_ids 只能引用给定的系统验收条件；没有时用 []")
        try:
            proposed = step.get('verification')
            if isinstance(proposed, list):
                # The planner sometimes copies a public unresolved requirement
                # into step predicates. It is a semantic obligation, never a
                # code predicate. Accept only exact framework-owned references.
                unresolved = {c['id']: c for c in public_spec.get('checks', [])
                              if c.get('kind') == 'unresolved_requirement'}
                proposed = [({'kind': 'visual'} if isinstance(p, dict)
                             and p.get('kind') == 'unresolved_requirement'
                             and p.get('id') in unresolved
                             and p.get('text') == unresolved[p['id']].get('text') else p)
                            for p in proposed]
            if not check_ids or all(c.get('kind') == 'unresolved_requirement' for c in public_spec.get('checks', [])):
                fmt = 'ppt' if re.search(r'\b(?:pptx?|powerpoint)\b|幻灯片|演示文稿', task, re.I) else (
                    'pdf' if re.search(r'\bpdf\b', task, re.I) else 'any')
                downloading = bool(re.search(r'下载|download|(?:保存|save).*(?:PPT|PDF|文件|file)',
                                             step['goal'], re.I))
                # Locating an entry is not acquiring a file. Treating both as
                # acquisitions consumes the same receipt twice and strands the
                # queue even though the actual download has completed.
                locating = re.search(r'(?:定位|查找|寻找|找到|识别|locate\b|find\b).{0,50}(?:下载链接|下载按钮|下载入口|download\s+(?:link|button))', step['goal'], re.I)
                acquiring = re.search(r'(?:点击|触发|开始|完成|执行).{0,15}下载|(?:并|然后|再)下载|(?:and|then)\s+download', step['goal'], re.I)
                if locating and not acquiring:
                    downloading = False
                artifact_predicate = {'kind': 'artifact_download', 'format': fmt}
                if _download_recheck(step['goal']):
                    artifact_predicate['purpose'] = 'recheck'
                if isinstance(proposed, list):
                    proposed = [(copy.deepcopy(artifact_predicate) if downloading else
                                 {'kind': 'visual'})
                                if isinstance(p, dict) and p.get('kind') in {'download_complete', 'download_valid'}
                                else p for p in proposed]
                    # A planner cannot turn an acquisition into an inspection
                    # by supplying purpose=recheck or inventing an artifact ID.
                    proposed = [({'kind': 'visual'} if locating and not downloading else copy.deepcopy(artifact_predicate))
                                if isinstance(p, dict) and p.get('kind') == 'artifact_download'
                                else p for p in proposed]
                if downloading:
                    # File evidence remains mandatory for a generic download.
                    proposed = [p for p in (proposed or [])
                                if not (isinstance(p, dict) and p.get('kind') in {'visual', 'artifact_download'})]
                    proposed.append(artifact_predicate)
            # Browser response filenames are unknown until the receipt exists.
            # A guessed filename must not become an extra user obligation.
            if isinstance(proposed, list) and re.search(r'下载|download', task, re.I):
                from pathlib import PurePosixPath
                adjusted = []
                for predicate in proposed:
                    path = str(predicate.get('path', '')) if isinstance(predicate, dict) else ''
                    filename = PurePosixPath(path.replace('\\', '/')).name
                    renamed = bool(filename and re.search(r'(?:改名|重命名|命名|另存|rename).{0,10}' + re.escape(filename.rsplit('.', 1)[0]), task, re.I))
                    if (isinstance(predicate, dict) and predicate.get('kind') == 'file_exists'
                            and str(step.get('phase', '')) in {'download', 'verify', 'final', 'inspect_download'}
                            and 'cli' not in step.get('actions', []) and filename and filename not in task and not renamed):
                        replacement = {'kind': 'artifact_download', 'format': 'pdf' if re.search(r'pdf|论文', task, re.I) else 'any'}
                        if _download_recheck(step['goal']):
                            replacement['purpose'] = 'recheck'
                        if replacement not in adjusted:
                            adjusted.append(replacement)
                    else:
                        adjusted.append(predicate)
                proposed = [p for i, p in enumerate(adjusted) if p not in adjusted[:i]]
            verification = [] if ids and "verification" not in step else normalize_verification(proposed)
        except ValueError as exc:
            raise ValueError(f"第 {i} 个子目标 verification：{exc}") from exc
        for predicate in verification:
            if predicate['kind'] in {'download_complete', 'download_valid'} and predicate['check_id'] not in check_ids:
                raise ValueError('下载谓词 check_id 必须引用已有系统条件')
        phase = str(step.get('phase') or '')
        if phase == 'report' and ('cli' in step.get('actions', []) or any(
                p.get('kind') in {'app_open', 'file_exists', 'file_content', 'files_equal', 'file_absent'}
                for p in verification)):
            # Reporting is framework-generated; a combined operational step
            # must still be executed and checked before report finalization.
            phase = 'deliverable_operation'
        normalized.append({"idx": i, "goal": step["goal"].strip(),
                           "expected_evidence": str(step.get("expected_evidence") or ""),
                           "check_ids": ids, "phase": phase, "status": "pending",
                           "verification": verification,
                           "actions": normalize_plan_actions(step.get("actions", []))})
    if task_profile.get("kind") == "paper_download":
        phases = {step["phase"] for step in normalized}
        allowed = {phase["phase"] for phase in task_profile["workflow"]}
        required = {"verify"} if repair else allowed
        if not phases.issubset(allowed):
            raise ValueError("论文计划包含未知流程阶段")
        if not required.issubset(phases):
            raise ValueError("论文下载规划缺少流程阶段：" + ",".join(sorted(required - phases)))
        ordered = [item["phase"] for item in task_profile["workflow"]]
        indexes = [next(i for i, step in enumerate(normalized) if step["phase"] == phase)
                   for phase in ordered if phase in phases]
        if indexes != sorted(indexes):
            raise ValueError("流程阶段顺序必须为 " + " → ".join(ordered))
        if not isinstance(obj.get("keywords"), list) or not any(isinstance(k, str) and k.strip() for k in obj["keywords"]):
            raise ValueError("论文搜索必须提供非空 keywords")
    if paper_goals:
        normalized = compile_download_milestones(normalized, paper_goals)
    paper_query = str(paper_goals[0].get('query') or paper_goals[0].get('title') or '').strip() if len(paper_goals) == 1 else ''
    return {"steps": normalized,
            "requirements": audit_requirement_proposals(task, obj.get("requirements")),
            "keywords": [paper_query] if paper_query else obj.get("keywords") if isinstance(obj.get("keywords"), list) else [],
            "final_check": ('框架确认目标论文 PDF 已保存到默认工程目录且内容身份匹配' if paper_query
                            else str(obj.get("final_check") or "")),
            "completion_spec": copy.deepcopy(public_spec)}
