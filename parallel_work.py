"""Measure independent work without publishing events from worker threads."""
from time import perf_counter


def measured_call(function, *args, **kwargs):
    started = perf_counter()
    result = function(*args, **kwargs)
    return result, round((perf_counter() - started) * 1000, 2)


def prepare_model_client(model):
    # Logging wrappers expose the same client via inner. This is local setup,
    # never a warm-up generation or a speculative request to the model server.
    target = getattr(model, 'inner', model)
    prepare = getattr(target, 'prepare_client', None)
    if not callable(prepare):
        return {'status': 'not_applicable'}
    try:
        prepare()
        return {'status': 'ready'}
    except Exception as exc:
        # Preserve the normal planner's error/retry reporting. Do not leak keys
        # from client configuration or exception messages into the UI.
        return {'status': 'deferred', 'error_type': type(exc).__name__}


def prepare_client_dependencies():
    """Hide lazy SDK imports behind browser startup; no network or credentials."""
    try:
        import openai  # noqa: F401
        import httpx  # noqa: F401
        return {'status': 'ready'}
    except Exception as exc:
        return {'status': 'deferred', 'error_type': type(exc).__name__}
