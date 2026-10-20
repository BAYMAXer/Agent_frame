"""Run wall time and provider-reported usage, without double-counting reviews."""
import math


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def summarize_events(events):
    result = dict(input_tokens=0, output_tokens=0, total_tokens=0, calls=0,
                  reported_calls=0, missing_usage_calls=0, input_reported_calls=0, output_reported_calls=0, elapsed_s=None,
                  timing_source='unavailable', complete=False, by_role={})
    seen = set()
    start = end = last = None
    for event in events:
        stamp = _number(event.get('timestamp'))
        if stamp is not None:
            last = stamp
        kind = event.get('event_type')
        if kind == 'episode_start' and start is None:
            start = stamp
        if kind in {'episode_end', 'episode_error'}:
            end = stamp
            result['complete'] = True
            elapsed = _number(event.get('elapsed_s', (event.get('outcome') or {}).get('elapsed_s')))
            if elapsed is not None:
                result.update(elapsed_s=elapsed, timing_source='monotonic')
        if kind != 'model_call' or not isinstance(event.get('call'), dict):
            continue
        call = event['call']
        identity = call.get('call_id')
        if identity is not None:
            identity = str(identity)
            if identity in seen:
                continue
            seen.add(identity)
        result['calls'] += 1
        role = str(event.get('actor_role') or call.get('actor_role') or 'unknown')
        group = result['by_role'].setdefault(role, dict(input_tokens=0, output_tokens=0, calls=0, missing_usage_calls=0))
        group['calls'] += 1
        usage = call.get('usage')
        if not isinstance(usage, dict):
            response = call.get('response')
            usage = response.get('usage') if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        inp = _number(usage.get('prompt_tokens', usage.get('input_tokens')))
        out = _number(usage.get('completion_tokens', usage.get('output_tokens')))
        if inp is None or out is None:
            result['missing_usage_calls'] += 1
            group['missing_usage_calls'] += 1
        else:
            result['reported_calls'] += 1
        for key, value in [('input_tokens', inp), ('output_tokens', out)]:
            if value is not None:
                result[key] += int(value)
                result[key.replace('_tokens', '_reported_calls')] += 1
                group[key] += int(value)
    result['total_tokens'] = result['input_tokens'] + result['output_tokens']
    if result['elapsed_s'] is None and start is not None and (end or last) is not None:
        result['elapsed_s'] = round(max(0, (end if end is not None else last) - start), 3)
        result['timing_source'] = 'event_span' if end is not None else 'observed_span'
    result['usage_complete'] = result['calls'] > 0 and result['missing_usage_calls'] == 0
    return result
