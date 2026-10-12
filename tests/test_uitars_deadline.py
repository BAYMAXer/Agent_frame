"""UI-TARS uses a strict coordinate protocol and one bounded request budget."""
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest

from osworld_agent.adapters.uitars_grounding import RealUItarsGrounding
from osworld_agent.model.request_deadline import ModelRequestTimeout


IMAGE = "data:image/png;base64,aW1hZ2U="


def _response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _grounder(text="(200, 300)", **kwargs):
    grounder = RealUItarsGrounding("http://unused", "EMPTY", "ui-tars", **kwargs)
    create = Mock(return_value=_response(text))
    grounder._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return grounder, create


def test_defaults_and_sdk_disable_hidden_retries(monkeypatch):
    client = Mock()
    constructor = Mock(return_value=client)
    transport = Mock()
    monkeypatch.setattr(openai, "OpenAI", constructor)
    monkeypatch.setattr(httpx, "Client", transport)
    grounder = RealUItarsGrounding("http://unused", "EMPTY", "ui-tars")
    assert grounder.timeout == 60
    assert grounder.retries == 0
    assert grounder.request_deadline is None
    assert grounder._get_client() is client
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert transport.call_args.kwargs == {"trust_env": False, "timeout": 60}


def test_stalled_request_obeys_wall_clock_and_late_response_cannot_mutate_state():
    grounder, create = _grounder(timeout=0.03, retries=3)
    release = threading.Event()
    finished = threading.Event()
    def hanging(**kwargs):
        try:
            release.wait(1)
            return _response("(100, 100)")
        finally:
            finished.set()
    create.side_effect = hanging
    started = time.monotonic()
    try:
        with pytest.raises(ModelRequestTimeout):
            grounder.locate(IMAGE, "button")
        assert time.monotonic() - started < 0.5
        assert create.call_count == 1
        assert grounder.last_call["attempts"][0]["status"] == "timeout"
        assert grounder.last_call["confidence"] == 0
        snapshot = copy.deepcopy(grounder.last_call)
    finally:
        release.set()
        assert finished.wait(1)
    assert grounder.last_call == snapshot


@pytest.mark.parametrize("error", [openai.APITimeoutError(request=httpx.Request("POST", "http://unused")),
                                  httpx.ReadTimeout("request timed out")])
def test_http_timeout_raises_model_timeout_without_retry(error):
    grounder, create = _grounder(timeout=1, retries=3)
    create.side_effect = error
    with pytest.raises(ModelRequestTimeout):
        grounder.locate(IMAGE, "button")
    assert create.call_count == 1
    assert grounder.last_call["attempts"][0]["status"] == "timeout"
    assert grounder.last_call["error"]


def test_existing_step_deadline_limits_the_sdk_timeout():
    grounder, create = _grounder(timeout=60)
    grounder.request_deadline = time.monotonic() + 0.1
    result = grounder.locate(IMAGE, "button")
    assert result.confidence == 1
    assert 0 < create.call_args.kwargs["timeout"] <= 0.1


def test_expired_step_deadline_never_sends_request():
    grounder, create = _grounder()
    grounder.request_deadline = time.monotonic() - 1
    with pytest.raises(ModelRequestTimeout):
        grounder.locate(IMAGE, "button")
    create.assert_not_called()
    assert grounder.last_call["attempts"][0]["status"] == "timeout"


@pytest.mark.parametrize("text", ["The screen is 1920 by 1080; button 3 is near pixel 200 300.",
                                 "[-1, -1]", "[-12, 40]", "[1920, 0]", "[0, 1080]",
                                 "[10,20,30,40]", "point: [10,20]; point: [30,40]", "no target"])
def test_format_or_bounds_failure_is_fast_and_not_retried(text):
    grounder, create = _grounder(text, retries=3)
    result = grounder.locate(IMAGE, "button")
    assert (result.x, result.y, result.confidence) == (-1, -1, 0)
    assert create.call_count == 1
    assert grounder.last_call["response"] == text
    assert grounder.last_call["attempts"][0]["status"] == "format_error"
    assert grounder.last_call["error"]
    assert grounder.last_call["messages"]


@pytest.mark.parametrize("text", ["(100, 200)", "[100,200]", "100,200", "```text\n(100,200)\n```",
                                 '{"x":100,"y":200}', '{"point":[100,200]}',
                                 "The screen is 1920 by 1080. point: [100,200]", "<point>100 200</point>"])
def test_valid_coordinate_protocols_and_call_metadata(text):
    grounder, create = _grounder(text)
    result = grounder.locate(IMAGE, "button")
    assert (result.x, result.y, result.confidence) == (100, 200, 1)
    assert grounder.last_call["x"] == 100
    assert grounder.last_call["y"] == 200
    assert grounder.last_call["confidence"] == 1
    assert grounder.last_call["error"] is None
    assert grounder.last_call["attempts"][0]["status"] == "ok"
    assert grounder.last_call["latency_ms"] >= 0


def test_default_reference_tracks_updated_screenshot_dimensions():
    grounder, create = _grounder("(900, 600)", width=1920, height=1080)
    grounder.width, grounder.height = 1000, 800
    result = grounder.locate(IMAGE, "button")
    assert (result.x, result.y) == (900, 600)


def test_explicit_reference_dimensions_scale_to_current_screenshot():
    grounder, create = _grounder("(500, 250)", grounding_width=1000, grounding_height=1000)
    result = grounder.locate(IMAGE, "button")
    assert (result.x, result.y) == (960, 270)
    grounder.width, grounder.height = 800, 600
    result = grounder.locate(IMAGE, "button")
    assert (result.x, result.y) == (400, 150)


def test_retry_backoff_cannot_exceed_total_request_budget():
    grounder, create = _grounder(timeout=0.05, retries=1)
    create.side_effect = RuntimeError("temporary failure")
    started = time.monotonic()
    with pytest.raises(ModelRequestTimeout):
        grounder.locate(IMAGE, "button")
    assert time.monotonic() - started < 0.5
    assert create.call_count == 1
    assert "重试" in grounder.last_call["error"]


def test_unencodable_screenshot_records_failure_without_request():
    grounder, create = _grounder()
    result = grounder.locate(None, "button")
    assert result.confidence == 0
    assert grounder.last_call["error"] == "截图无法编码"
    assert grounder.last_call["x"] == grounder.last_call["y"] == -1
    create.assert_not_called()
