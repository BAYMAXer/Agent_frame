"""Public, replayable workbench projection. No evaluator-private manifests."""
from __future__ import annotations
import copy
import re

_MAX_OBS_NODES = 200  # 观察里的 AX/DOM 树只保留前 N 个节点，避免 workbench 响应膨胀到几十 MB


def _bounded_observation(obs, max_nodes=_MAX_OBS_NODES):
    """观察对象里 context[].nodes 是完整无障碍树，可能数 MB；只保留头部节点。"""
    if not isinstance(obs, dict):
        return obs
    obs = copy.deepcopy(obs)
    ctx = obs.get('context')
    if isinstance(ctx, list):
        for block in ctx:
            if isinstance(block, dict):
                nodes = block.get('nodes')
                if isinstance(nodes, list) and len(nodes) > max_nodes:
                    block['nodes'] = nodes[:max_nodes]
                    block['nodes_truncated'] = len(nodes) - max_nodes
    return obs


def project_workbench(traj):
    keys = ('step', 'action_id', 'plan_revision', 'plan_step_id', 'plan_step', 'action',
            'policy_input', 'policy_output', 'prompt_metadata', 'decision_calls', 'observation', 'next_observation',
            'screenshot', 'routing', 'operation_mode', 'execution_info', 'execution', 'executed',
            'outcome', 'step_verification', 'step_reviews', 'execution_queue', 'timing_ms',
            'reasoning', 'summary', 'grounding', 'valid_action', 'framework_operations')
    actions = []
    for s in traj.get('steps', []):
        a = {}
        for k in keys:
            if k not in s:
                continue
            a[k] = _bounded_observation(s[k]) if k in ('observation', 'next_observation') else copy.deepcopy(s[k])
        actions.append(a)
    by_step = {str(a['step']): a for a in actions}
    revisions = []
    calls_seen = set()
    unassigned = []
    for e in traj.get('events', []):
        kind = e.get('event_type')
        if kind == 'plan_revision':
            revisions.append({k: copy.deepcopy(e[k]) for k in ('revision', 'plan', 'queue', 'previous', 'reason', 'timestamp') if k in e})
        if kind == 'execution_queue' and revisions:
            q = e.get('queue') or {}
            rev = next((r for r in revisions if r.get('revision') == q.get('revision')), None)
            if rev is not None:
                rev['queue'] = copy.deepcopy(q)
        if kind not in {'model_call', 'jev_call'}:
            continue
        c = e.get('call') or {}
        key = c.get('call_id') or e.get('event_id')
        if key and key in calls_seen:
            continue
        calls_seen.add(key)
        parent = re.fullmatch(r'a_(\d+)', str(e.get('parent_decision_id') or ''))
        step = str(c.get('decision_step') or (parent.group(1) if parent else ''))
        target = by_step.get(step)
        # Keep actual sent prompts and outputs; never include private evaluation data.
        if target is not None:
            target.setdefault('calls', []).append(copy.deepcopy(c))
        else:
            unassigned.append(copy.deepcopy(c))
    if not revisions and traj.get('plan'):
        revisions = [{'revision': None, 'plan': copy.deepcopy(traj['plan']), 'legacy': True}]
    for a in actions:
        decisions = [c for c in a.get('calls', []) if c.get('actor_role') == 'decision']
        if decisions:
            a.setdefault('decision_calls', decisions)
            if not a.get('policy_input'):
                a['policy_input'] = copy.deepcopy(decisions[-1].get('messages'))
            if not a.get('policy_output'):
                a['policy_output'] = decisions[-1].get('response')
        # Older runs did not persist action-to-plan assignment. Do not guess it.
        a.setdefault('plan_step_id', None)
        a.setdefault('plan_revision', None)
    manifest = traj.get('run_manifest') or {}
    return {'version': 1, 'instruction': (traj.get('task') or {}).get('instruction', ''),
            'actions': actions, 'revisions': revisions, 'outcome': copy.deepcopy(traj.get('outcome') or {}),
            'metrics': copy.deepcopy(manifest.get('run_metrics') or (traj.get('outcome') or {}).get('run_metrics') or {}),
            'report': copy.deepcopy(manifest.get('report_delivery') or {}),
            'knowledge': copy.deepcopy(manifest.get('knowledge_retrieval') or {}),
            'unassigned_calls': unassigned}


def project_event_workbench(events, instruction='', metrics=None):
    """Recover a partial run without inventing missing actions or screenshots."""
    actions = {}
    outcome, report, plan = {}, {}, None
    for e in events:
        kind = e.get('event_type')
        parent = re.fullmatch(r'a_(\d+)', str(e.get('parent_decision_id') or ''))
        number = e.get('step') or (int(parent.group(1)) if parent else None)
        if kind in {'action_context', 'transition', 'transition_finalized', 'execution', 'route', 'step_verification'}:
            item = e.get('transition', {}) if kind.startswith('transition') else e
            number = item.get('step') or number or (e.get('report') or {}).get('decision_step')
            if number:
                a = actions.setdefault(number, {'step': number, 'step_reviews': []})
                if kind == 'action_context':
                    a.update({k: item[k] for k in ('plan_revision', 'plan_step_id', 'plan_step', 'observation', 'policy_input') if k in item})
                elif kind.startswith('transition'):
                    a.update(item)
                elif kind == 'execution':
                    a.update({k: e[k] for k in ('action', 'execution_info', 'next_observation', 'outcome') if k in e})
                elif kind == 'route':
                    a.update(routing=e.get('routing', {}), operation_mode=e.get('operation_mode'))
                elif kind == 'step_verification':
                    a['step_verification'] = e.get('report', {})
                    a['step_reviews'].append(e.get('report', {}))
        elif kind == 'plan_revision':
            plan = e.get('plan')
        elif kind == 'episode_end':
            outcome = e.get('outcome', {})
        elif kind == 'episode_error':
            outcome = {'success': False, 'termination_reason': e.get('error_type', 'episode_error')}
        elif kind == 'report_start':
            report = {'version': e.get('version'), 'sources': e.get('sources', []), 'status': 'streaming', 'text': ''}
        elif kind == 'report_delta':
            report['text'] = e.get('text', report.get('text', '') + e.get('delta', ''))
        elif kind == 'report_complete':
            report = {k: e[k] for k in ('text', 'version', 'sources', 'status', 'verification_status', 'requirements') if k in e}
        elif kind == 'report_error':
            report.update(status='failed', error=e.get('reason'))
        elif kind == 'report_verification':
            report['verification_status'] = e.get('status')
    projected = project_workbench({'task': {'instruction': instruction}, 'steps': list(sorted(actions.values(), key=lambda a:a['step'])),
        'events': events, 'plan': plan, 'outcome': outcome, 'run_manifest': {'report_delivery': report, 'run_metrics': metrics or {}}})
    projected['incomplete'] = not outcome
    return projected
