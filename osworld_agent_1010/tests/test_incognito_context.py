"""CDP 无痕上下文核验：未知实例拒绝复用，临时连接始终断开。"""
from unittest.mock import Mock

import pytest

from osworld_agent import utils


def _connected(monkeypatch, contexts, targets):
    playwright = Mock()
    cdp = Mock()
    cdp.send.side_effect = [contexts, {"targetInfos": targets}]
    browser = Mock()
    browser.new_browser_cdp_session.return_value = cdp
    connect = Mock(return_value=(playwright, browser))
    monkeypatch.setattr(utils, "_connect_browser", connect)
    return playwright, browser, cdp, connect


@pytest.mark.parametrize(
    "contexts,targets,expected",
    [
        ({"defaultBrowserContextId": "default"},
         [{"type": "page", "browserContextId": "private"}], True),
        ({"defaultBrowserContextId": "default"},
         [{"type": "page", "browserContextId": "private"},
          {"type": "page", "browserContextId": "another-private"},
          {"type": "service_worker", "browserContextId": "default"}], True),
        ({"defaultBrowserContextId": "default"},
         [{"type": "page", "browserContextId": "default"}], False),
        ({"defaultBrowserContextId": "default"},
         [{"type": "page", "browserContextId": "private"},
          {"type": "page", "browserContextId": "default"}], False),
        ({"defaultBrowserContextId": "default"}, [], False),
        ({"defaultBrowserContextId": "default"},
         [{"type": "service_worker", "browserContextId": "private"}], False),
        ({"defaultBrowserContextId": "default"}, [{"type": "page"}], False),
        ({"defaultBrowserContextId": "default"},
         [{"type": "page", "browserContextId": ""}], False),
        ({}, [{"type": "page", "browserContextId": "private"}], False),
        ({"defaultBrowserContextId": ""},
         [{"type": "page", "browserContextId": "private"}], False),
    ],
    ids=["incognito", "all-incognito-ignore-workers", "normal", "mixed",
         "empty", "no-pages", "missing-page-context", "empty-page-context",
         "missing-default-context", "empty-default-context"],
)
def test_only_confirmed_incognito_pages_are_accepted(monkeypatch, contexts, targets, expected):
    playwright, browser, cdp, connect = _connected(monkeypatch, contexts, targets)

    assert utils.cdp_pages_are_incognito("127.0.0.1", 9333) is expected

    connect.assert_called_once_with("127.0.0.1", 9333)
    assert [call.args[0] for call in cdp.send.call_args_list] == [
        "Target.getBrowserContexts", "Target.getTargets",
    ]
    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()


@pytest.mark.parametrize("stage", ["session", "contexts", "targets"])
def test_cdp_errors_fail_closed_and_disconnect(monkeypatch, stage):
    playwright, browser, cdp, _ = _connected(monkeypatch, {}, [])
    error = RuntimeError("CDP unavailable")
    if stage == "session":
        browser.new_browser_cdp_session.side_effect = error
    elif stage == "contexts":
        cdp.send.side_effect = error
    else:
        cdp.send.side_effect = [{"defaultBrowserContextId": "default"}, error]

    assert utils.cdp_pages_are_incognito("127.0.0.1") is False

    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()


def test_connect_error_returns_false(monkeypatch):
    connect = Mock(side_effect=RuntimeError("Connection refused"))
    monkeypatch.setattr(utils, "_connect_browser", connect)

    assert utils.cdp_pages_are_incognito("127.0.0.1") is False

    connect.assert_called_once_with("127.0.0.1", 9222)


@pytest.mark.parametrize("cleanup", ["browser-close", "playwright-stop"])
def test_cleanup_errors_do_not_skip_the_other_cleanup(monkeypatch, cleanup):
    playwright, browser, _, _ = _connected(
        monkeypatch,
        {"defaultBrowserContextId": "default"},
        [{"type": "page", "browserContextId": "private"}],
    )
    if cleanup == "browser-close":
        browser.close.side_effect = RuntimeError("Already disconnected")
    else:
        playwright.stop.side_effect = RuntimeError("Already stopped")

    assert utils.cdp_pages_are_incognito("127.0.0.1") is True

    browser.close.assert_called_once_with()
    playwright.stop.assert_called_once_with()
