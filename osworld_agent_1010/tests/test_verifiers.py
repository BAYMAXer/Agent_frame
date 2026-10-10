"""验证器模块单元测试：下载验证 + 导航验证。"""
from osworld_agent.verifiers import check_completion


def test_download_verified():
    info = {"action": "download", "verified": True, "path": "xxx.pdf", "bytes": 100}
    assert check_completion("下载这篇论文", info) == "download"
    assert check_completion("download this paper", info) == "download"


def test_download_not_verified():
    info = {"action": "download", "verified": False}
    assert check_completion("下载这篇论文", info) == ""


def test_navigation_executed():
    info = {"action": "goto", "status": "executed", "url": "https://arxiv.org"}
    assert check_completion("打开 arxiv.org", info) == "navigation"
    assert check_completion("访问这个页面", info) == "navigation"


def test_no_match():
    info = {"action": "click", "status": "executed"}
    assert check_completion("下载这篇论文", info) == ""
    assert check_completion("打开页面", info) == ""
