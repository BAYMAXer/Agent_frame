"""Windows browser use 演示：焦点检测 + 抓取页面 AX-Tree / HTML 源码。

用法：
    1) 先装依赖：pip install playwright requests
    2) 运行：python demo_browser_windows.py

脚本会启动一个带 remote debugging 的无痕 Chrome，打开网页，然后演示：
    - 焦点检测（当前前台窗口是不是浏览器）
    - 抓取当前页面的 AX-Tree（可访问性树）
    - 抓取当前页面的 HTML 源码
"""
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")

from osworld_agent.utils import (
    detect_foreground_browser,
    fetch_accessibility_tree,
    fetch_page_source,
    list_cdp_pages,
)

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9222
URL = "https://www.baidu.com"  # 可换成任意网页


def main():
    print(f"[1] 启动无痕 Chrome（remote debugging 端口 {PORT}）...")
    proc = subprocess.Popen([
        CHROME,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={tempfile.gettempdir()}\\browser_use_demo_incognito_profile",
        "--incognito", "--new-window",
        "--no-first-run", "--no-default-browser-check",
        URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)  # 等页面加载

    print("\n[2] 焦点检测（当前前台窗口是否是浏览器）...")
    is_browser = detect_foreground_browser("windows")
    print(f"    detect_foreground_browser -> {is_browser}")

    print("\n[3] CDP 页面列表 ...")
    pages = list_cdp_pages("localhost", PORT)
    for p in pages:
        if p.get("url", "").startswith("http"):
            print(f"    {p.get('type')} | {p.get('url')[:60]} | {p.get('title')}")

    print("\n[4] 抓取当前页面 AX-Tree ...")
    a11y = fetch_accessibility_tree("localhost", PORT)
    print(f"    AX-Tree {len(a11y)} 字符，前 500 字：")
    print("    " + a11y[:500].replace("\n", "\n    ") if a11y else "    (空)")

    print("\n[5] 抓取当前页面 HTML 源码 ...")
    html = fetch_page_source("localhost", PORT)
    print(f"    HTML {len(html)} 字符，前 300 字：")
    print("    " + html[:300].replace("\n", "\n    ") if html else "    (空)")

    try:
        proc.terminate()
    except Exception:
        pass
    print("\n演示结束。注意：这是从「已打开的 Chrome」抓取；核心函数都在 osworld_agent/utils.py。")


if __name__ == "__main__":
    main()
