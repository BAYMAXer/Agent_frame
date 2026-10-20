"""Record each grounding request, including coarse/fine passes and retries."""
import time


def completion(owner, **kwargs):
    started = time.perf_counter()
    call = {'model': owner.model, 'usage': None}
    try:
        response = owner._get_client().chat.completions.create(**kwargs)
        usage = getattr(response, 'usage', None)
        call['usage'] = usage.model_dump() if hasattr(usage, 'model_dump') else usage if isinstance(usage, dict) else None
        call['response_id'] = getattr(response, 'id', None)
        return response
    except Exception as exc:
        call['error_type'] = type(exc).__name__
        raise
    finally:
        call['latency_ms'] = round((time.perf_counter() - started) * 1000, 2)
        if not hasattr(owner, 'last_calls'):
            owner.last_calls = []
        owner.last_calls.append(call)
