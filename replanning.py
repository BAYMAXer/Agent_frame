"""Evidence brief and exact repeat guard for recovery plans."""
import copy
import re


def _signature(steps):
    def text(value):
        value = str(value)
        pattern = r'https?://[^\s，。；、<>"\x27]+'
        urls = tuple(re.findall(pattern, value))
        return re.sub(r'\W+', '', re.sub(pattern, 'URL', value)).casefold(), urls
    return [(text(s.get('goal')), tuple(sorted(s.get('actions') or []))) for s in steps]


def recovery_brief(state):
    steps = (state.memory.plan or {}).get('steps', [])
    queue = getattr(state, 'queue', None)
    cursor = getattr(queue, 'cursor', 0)
    def step_brief(step):
        return {k: copy.deepcopy(step[k]) for k in ('goal', 'phase', 'actions', 'expected_evidence') if k in step}
    review = {k: state.step_review[k] for k in ('status', 'reason', 'method', 'evidence') if k in state.step_review}
    from .report_delivery import observed_navigation_hints
    return {'observed_navigation_hints': observed_navigation_hints(state), 'failure_class': getattr(state, 'failure_class', ''), 'failure_key': getattr(state, 'failure_key', ''),
            'blocked_step': step_brief(queue.current) if queue and queue.current else None,
            'completed_steps': [s.get('goal') for s in steps[:cursor]],
            'remaining_steps': [step_brief(s) for s in steps[cursor:]],
            'recent_failures': getattr(state.memory, 'failure_memory', [])[-5:],
            'failed_strategies': getattr(state.memory, 'failed_strategies', [])[-5:],
            'recent_actions': state.memory.action_history[-5:],
            'review': review, 'progress': state.step_progress,
            'recovery': state.recovery}


def validate_repair(plan, proposal, brief):
    explanation = proposal.get('strategy_change')
    if not isinstance(explanation, str) or not 8 <= len(explanation.strip()) <= 800:
        raise ValueError('重规划必须提供 strategy_change：说明失败证据、改用的方法和预期新证据（8–800 字符）')
    if _signature(plan['steps']) == _signature(brief['remaining_steps']):
        raise ValueError('重规划重复了受阻的剩余计划；仅修改编号、验收描述或 strategy_change 不算改变路径')
    if brief['remaining_steps'] and _signature(plan['steps'][:1]) == _signature(brief['remaining_steps'][:1]):
        raise ValueError('首个子任务仍是受阻的原方法；请先修复阻碍或在首个子目标中明确改变的操作路径')
    # A wording change is not proof of a better strategy. It remains a proposal
    # and all actions must still pass runtime progress and completion checks.
    plan['strategy_change'] = explanation.strip()
    plan['recovery_evidence'] = copy.deepcopy(brief)
