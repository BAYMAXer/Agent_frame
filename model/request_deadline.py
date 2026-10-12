"""Bound model network calls by wall-clock time, including a stalled HTTP body."""
from __future__ import annotations

import math
import queue
import threading
import time
from typing import Any, Callable, Optional


class ModelRequestTimeout(TimeoutError):
    """A model request exhausted its HTTP or wall-clock budget."""


def make_request_deadline(timeout: float, request_deadline: Optional[float] = None) -> float:
    """Combine a per-call timeout with an optional monotonic step deadline."""
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("模型请求 timeout 必须是有限正数")
    deadline = time.monotonic() + timeout
    if request_deadline is not None:
        deadline = min(deadline, float(request_deadline))
    return deadline


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelRequestTimeout("模型请求已超过墙钟截止时间")
    return remaining


def sleep_before_retry(delay: float, deadline: float) -> None:
    """Retry backoff shares the same budget as the requests themselves."""
    remaining = remaining_seconds(deadline)
    if delay >= remaining:
        raise ModelRequestTimeout("模型请求剩余预算不足以重试")
    time.sleep(delay)
    remaining_seconds(deadline)


def call_with_deadline(call: Callable[..., Any], timeout: float, **kwargs) -> Any:
    """Run only the network call in a daemon; late results cannot update model state.

    HTTP read timeouts limit inactivity, so a response that trickles forever still
    needs this total deadline. The worker owns no GUI or trajectory operations.
    """
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ModelRequestTimeout("模型请求已超过墙钟截止时间")
    deadline = time.monotonic() + timeout
    output: queue.Queue = queue.Queue(maxsize=1)

    def network_worker():
        try:
            result = call(**kwargs)
        except BaseException as exc:
            output.put((False, exc))
        else:
            output.put((True, result))

    thread = threading.Thread(target=network_worker, name="model-network-request", daemon=True)
    thread.start()
    try:
        ok, result = output.get(timeout=remaining_seconds(deadline))
    except queue.Empty as exc:
        raise ModelRequestTimeout(f"模型请求超过墙钟预算（{timeout:.2f} 秒）") from exc
    remaining_seconds(deadline)
    if not ok:
        raise result
    return result
