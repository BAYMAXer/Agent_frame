"""演示完成或导航异常都保留浏览器，仅释放会话资源。"""
from unittest.mock import Mock

import pytest

from osworld_agent import demo_browser_windows as demo


@pytest.mark.parametrize("started", [True, False])
@pytest.mark.parametrize("navigation_ok", [True, False])
def test_demo_keeps_browser_and_releases_session(monkeypatch, started, navigation_ok):
    profile = "owned-profile" if started else None
    ensure = Mock(return_value=(started, profile))
    close = Mock()
    session = Mock()
    session.goto.return_value = ({"status": "executed"} if navigation_ok
                                 else {"status": "rejected", "error": "navigation failed"})
    monkeypatch.setattr(demo, "ensure_chrome", ensure)
    monkeypatch.setattr(demo, "close_test_chrome", close, raising=False)
    monkeypatch.setattr(demo, "BrowserSession", Mock(return_value=session))
    monkeypatch.setattr(demo.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(demo, "detect_foreground_browser", lambda _os: False)
    monkeypatch.setattr(demo, "list_cdp_pages", lambda *_args: [])
    monkeypatch.setattr(demo, "fetch_accessibility_tree", lambda *_args: "")
    monkeypatch.setattr(demo, "fetch_page_source", lambda *_args: "")

    if navigation_ok:
        demo.main()
    else:
        with pytest.raises(RuntimeError, match="navigation failed"):
            demo.main()

    ensure.assert_called_once_with(demo.PORT, demo.CHROME)
    session.goto.assert_called_once_with(demo.URL)
    session.close.assert_called_once_with()
    close.assert_not_called()
