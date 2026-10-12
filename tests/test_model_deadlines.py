"""Network deadline checks use a fake SDK and never contact a model server."""
import copy
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osworld_agent.config import GroundingConfig, ModelConfig
from osworld_agent.model import decision_model
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.request_deadline import ModelRequestTimeout, call_with_deadline
from osworld_agent.adapters import uivenus2_grounding
from osworld_agent.adapters.uivenus2_grounding import UIVenus2Grounding


def response(text="hello"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                                    finish_reason="stop")],
                           id="test", model="test-model", usage=None)


@pytest.fixture
def sdk(monkeypatch):
    class APITimeoutError(Exception):
        pass

    class HTTPTimeout(Exception):
        pass

    create = Mock(return_value=response())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    constructor = Mock(return_value=client)
    http_client = Mock(return_value=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "openai",
                        SimpleNamespace(OpenAI=constructor, APITimeoutError=APITimeoutError))
    monkeypatch.setitem(sys.modules, "httpx",
                        SimpleNamespace(Client=http_client, TimeoutException=HTTPTimeout))
    return SimpleNamespace(create=create, constructor=constructor, http_client=http_client,
                           APITimeoutError=APITimeoutError, HTTPTimeout=HTTPTimeout)


def test_default_timeouts_and_retry_counts_make_one_sdk_attempt(sdk):
    assert ModelConfig().timeout == 60.0
    assert GroundingConfig().timeout == 60.0
    assert ModelConfig().request_retries == 0
    model = ChatModel(ModelConfig())
    assert model.chat([{"role": "user", "content": "test"}]) == "hello"
    assert sdk.create.call_count == 1
    assert sdk.constructor.call_args.kwargs["max_retries"] == 0
    assert 0 < sdk.create.call_args.kwargs["timeout"] <= 60


def test_grounding_disables_sdk_retries_and_returns_protocol_coordinates(sdk):
    sdk.create.return_value = response("[500,250]")
    model = UIVenus2Grounding(url="test", api_key="test", model="test")
    assert model.timeout == 60.0
    assert model.retries == 0
    point = model.locate(b"test image", "button")
    assert (point.x, point.y, point.confidence) == (960, 270, 1.0)
    assert sdk.create.call_count == 1
    assert sdk.constructor.call_args.kwargs["max_retries"] == 0
    assert model.last_call["response"] == "[500,250]"
    assert model.last_call["messages"]


@pytest.mark.parametrize("text", ["invalid coordinates", "[-1,-1]", "[1001,200]", "[200,1001]",
                                 "[-3,200]", "[0,0,1001,500]"])
def test_grounding_format_failures_do_not_repeat_by_default(sdk, text):
    sdk.create.return_value = response(text)
    model = UIVenus2Grounding(url="test", api_key="test", model="test")
    point = model.locate(b"test image", "button")
    assert point.confidence == 0.0
    assert sdk.create.call_count == 1
    assert model.last_call["attempts"][0]["status"] == "format_error"
    assert model.last_call["error"]


def test_grounding_normalized_right_bottom_edge_stays_inside_screenshot(sdk):
    sdk.create.return_value = response("[1000,1000]")
    model = UIVenus2Grounding(url="test", api_key="test", model="test", width=1920, height=1080)
    point = model.locate(b"test image", "button")
    assert (point.x, point.y, point.confidence) == (1919, 1079, 1.0)


def test_daemon_deadline_returns_even_when_network_never_returns():
    release = threading.Event()
    workers = []

    def stuck():
        workers.append(threading.current_thread())
        release.wait()

    started = time.monotonic()
    try:
        with pytest.raises(ModelRequestTimeout):
            call_with_deadline(stuck, 0.03)
        assert time.monotonic() - started < 1.0
        assert len(workers) == 1
        assert workers[0].daemon is True
    finally:
        release.set()


@pytest.mark.parametrize("kind", ["decision", "grounding"])
def test_wall_clock_timeout_never_retries_or_accepts_late_response(sdk, kind):
    release = threading.Event()

    def stuck(**kwargs):
        release.wait()
        return response("[500,500]")

    sdk.create.side_effect = stuck
    if kind == "decision":
        model = ChatModel(ModelConfig(timeout=0.03, request_retries=3))
        invoke = lambda: model.chat([{"role": "user", "content": "test"}], retries=3)
    else:
        model = UIVenus2Grounding(url="test", api_key="test", model="test", timeout=0.03, retries=3)
        invoke = lambda: model.locate(b"test image", "button")
    try:
        with pytest.raises(ModelRequestTimeout):
            invoke()
        assert sdk.create.call_count == 1
        assert model.last_call["attempts"][0]["status"] == "timeout"
        state = copy.deepcopy(model.last_call)
    finally:
        release.set()
    # A completed background request must have no path to model metadata.
    time.sleep(0.02)
    assert model.last_call == state


@pytest.mark.parametrize("kind", ["decision", "grounding"])
@pytest.mark.parametrize("error_name", ["APITimeoutError", "HTTPTimeout"])
def test_sdk_timeouts_are_normalized_and_never_retried(sdk, kind, error_name):
    sdk.create.side_effect = getattr(sdk, error_name)("HTTP stalled")
    if kind == "decision":
        model = ChatModel(ModelConfig(request_retries=2))
        invoke = lambda: model.chat([{"role": "user", "content": "test"}], retries=2)
    else:
        model = UIVenus2Grounding(url="test", api_key="test", model="test", retries=2)
        invoke = lambda: model.locate(b"test image", "button")
    with pytest.raises(ModelRequestTimeout):
        invoke()
    assert sdk.create.call_count == 1


def test_decision_explicit_retry_is_capped_by_configuration(sdk, monkeypatch):
    monkeypatch.setattr(decision_model, "sleep_before_retry", Mock())
    sdk.create.side_effect = [RuntimeError("temporary"), response("recovered")]
    model = ChatModel(ModelConfig(request_retries=1))
    assert model.chat([{"role": "user", "content": "test"}], retries=5) == "recovered"
    assert sdk.create.call_count == 2
    assert len(model.last_call["attempts"]) == 2


@pytest.mark.parametrize("requested,configured", [(5, 0), (0, 5)])
def test_decision_does_not_retry_without_both_explicit_request_and_budget(sdk, requested, configured):
    sdk.create.side_effect = RuntimeError("temporary")
    model = ChatModel(ModelConfig(request_retries=configured))
    with pytest.raises(RuntimeError, match="模型调用失败"):
        model.chat([{"role": "user", "content": "test"}], retries=requested)
    assert sdk.create.call_count == 1


def test_grounding_can_retry_format_failure_only_when_explicitly_configured(sdk, monkeypatch):
    monkeypatch.setattr(uivenus2_grounding, "sleep_before_retry", Mock())
    sdk.create.side_effect = [response("invalid"), response("[100,200]")]
    model = UIVenus2Grounding(url="test", api_key="test", model="test", retries=1)
    point = model.locate(b"test image", "button")
    assert point.confidence == 1.0
    assert sdk.create.call_count == 2
    assert len(model.last_call["attempts"]) == 2


@pytest.mark.parametrize("kind", ["decision", "grounding"])
def test_expired_step_deadline_prevents_starting_network_call(sdk, kind):
    if kind == "decision":
        model = ChatModel(ModelConfig())
        invoke = lambda: model.chat([{"role": "user", "content": "test"}])
    else:
        model = UIVenus2Grounding(url="test", api_key="test", model="test")
        invoke = lambda: model.locate(b"test image", "button")
    model.request_deadline = time.monotonic() - 1
    with pytest.raises(ModelRequestTimeout):
        invoke()
    sdk.create.assert_not_called()


@pytest.mark.parametrize("kind", ["decision", "grounding"])
def test_remaining_step_budget_is_passed_to_sdk_request(sdk, kind):
    if kind == "decision":
        model = ChatModel(ModelConfig(timeout=60))
        invoke = lambda: model.chat([{"role": "user", "content": "test"}])
    else:
        sdk.create.return_value = response("[100,200]")
        model = UIVenus2Grounding(url="test", api_key="test", model="test", timeout=60)
        invoke = lambda: model.locate(b"test image", "button")
    model.request_deadline = time.monotonic() + 0.5
    invoke()
    assert 0 < sdk.create.call_args.kwargs["timeout"] <= 0.5


def test_retry_backoff_cannot_extend_call_budget(sdk):
    sdk.create.side_effect = RuntimeError("temporary")
    model = ChatModel(ModelConfig(timeout=0.03, request_retries=1))
    started = time.monotonic()
    with pytest.raises(ModelRequestTimeout):
        model.chat([{"role": "user", "content": "test"}], retries=1)
    assert time.monotonic() - started < 1.0
    assert sdk.create.call_count == 1
