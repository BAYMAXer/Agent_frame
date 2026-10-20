"""框架工具集。

集中放置框架复用的工具函数。当前主要内容是「浏览器工具」（browser use 场景）：
- 焦点检测：判断当前活动窗口是否是浏览器（跨平台，零 token 系统调用）；
- CDP 抓取：抓「当前页面」的 AX-Tree / HTML / URL（Chrome/Edge 走 CDP，Firefox 走 CDP 子集，
  Safari 留 adapter 位；抓不到就降级为空，不抛异常）。

以后要添加浏览器操作工具（click_element / fill / navigate / evaluate 等）也加到这个文件，
保持「浏览器相关工具都集中在这里」。
"""
from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import requests

# 常见浏览器进程名 / 窗口类名关键词（用于焦点检测）
_BROWSER_KEYWORDS = (
    "chrome", "chromium", "msedge", "edge", "firefox", "safari",
    "brave", "opera", "vivaldi", "browser",
)

# 浏览器内部/非网页 scheme，抓页面信息时要排除
_INTERNAL_SCHEMES = (
    "chrome://", "chrome-extension://", "devtools://", "about:", "edge://",
    "view-source:", "file://", "data:",
)


def _is_web_page(url: str) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))


@dataclass
class BrowserState:
    """一次浏览器探测的结果。"""
    is_browser: bool = False          # 当前焦点是否在浏览器
    url: str = ""                      # 当前页面 URL
    page_source: str = ""              # 当前页面 HTML 源码（可能截断）
    a11y_tree: str = ""                # 当前页面 AX-Tree（可能截断）
    error: str = ""                    # 探测/抓取失败时的说明

    def describe(self) -> str:
        """给决策模型看的显式状态文本。"""
        if self.error and not self.is_browser:
            return f"【浏览器状态】无（{self.error}）"
        if not self.is_browser:
            return "【浏览器状态】无浏览器焦点，走 GUI"
        parts = [f"【浏览器状态】浏览器焦点，当前 URL: {self.url}"]
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 焦点检测（跨平台）
# --------------------------------------------------------------------------- #
def detect_foreground_browser(os_name: Optional[str] = None) -> bool:
    """检测当前活动窗口是否是浏览器。返回 True/False（探测失败返回 False）。"""
    os_name = (os_name or platform.system()).lower()
    try:
        if os_name == "windows":
            return _foreground_windows()
        if os_name in ("linux", "mac", "darwin"):
            return _foreground_linux_mac()
    except Exception:  # noqa: BLE001
        return False
    return False


def _foreground_windows() -> bool:
    """Windows：用 ctypes 读前台窗口标题/进程名。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value.lower()
    return any(k in title for k in _BROWSER_KEYWORDS)


def _foreground_linux_mac() -> bool:
    """Linux：用 xdotool 读活动窗口名；macOS：用 AppleScript 读前台 app 名。"""
    os_name = platform.system().lower()
    try:
        if os_name == "darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=5,
            ).stdout
        else:
            # Linux：优先 xdotool，回退 wmctrl
            out = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            if not out.strip():
                out = subprocess.run(
                    ["wmctrl", "-a", ":ACTIVE:"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
        return any(k in out.lower() for k in _BROWSER_KEYWORDS)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# CDP 探测 + 抓取
# --------------------------------------------------------------------------- #
def _cdp_json(host: str, port: int, path: str, timeout: float = 4.0):
    """CDP is a direct local/LAN control channel, independent of web proxy settings."""
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(f"http://{host}:{port}{path}", timeout=timeout)
        response.raise_for_status()
        return response.json()


def cdp_websocket_endpoint(host: str, port: int = 9222, timeout: float = 4.0) -> str:
    """Resolve the CDP WebSocket without making the Node driver use HTTP proxies."""
    endpoint = _cdp_json(host, port, "/json/version/", timeout).get("webSocketDebuggerUrl", "")
    if not isinstance(endpoint, str) or not endpoint.startswith(("ws://", "wss://")):
        raise ValueError("CDP discovery did not provide a WebSocket debugger URL")
    return endpoint


def list_cdp_pages(host: str, port: int = 9222, timeout: float = 4.0) -> List[dict]:
    """CDP /json 端点返回打开的页面列表。失败返回 []。"""
    try:
        result = _cdp_json(host, port, "/json", timeout)
        if isinstance(result, list):
            return [page for page in result if isinstance(page, dict)]
    except Exception:  # noqa: BLE001
        pass
    return []


def _current_page(pages: List[dict]) -> Optional[dict]:
    """从页面列表里选「当前页面」：优先最后一个 http/https 网页；没有则选最后一个非内部 scheme 的页面。"""
    web = [p for p in pages if _is_web_page(p.get("url", ""))]
    if web:
        return web[-1]
    normal = [p for p in pages if p.get("url") and not p["url"].startswith(_INTERNAL_SCHEMES)]
    if normal:
        return normal[-1]
    return pages[-1] if pages else None


def _connect_browser(host: str, port: int):
    """用 playwright connect_over_cdp 连接浏览器。返回 (playwright, browser) 或抛异常。"""
    from playwright.sync_api import sync_playwright
    endpoint = cdp_websocket_endpoint(host, port)
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(endpoint, timeout=4000)
        return p, browser
    except Exception:
        try:
            p.stop()
        except Exception:
            pass
        raise


def cdp_pages_are_incognito(host: str, port: int = 9222) -> bool:
    """仅在 CDP 能确认所有页面都属于无痕上下文时，允许复用浏览器。

    普通页面属于 defaultBrowserContextId；无痕页面使用独立上下文。
    接口失败或旧版浏览器未返回默认上下文 ID 时，不接入未知实例。
    """
    try:
        p, browser = _connect_browser(host, port)
    except Exception:  # noqa: BLE001
        return False
    try:
        cdp = browser.new_browser_cdp_session()
        default_id = cdp.send("Target.getBrowserContexts").get("defaultBrowserContextId")
        pages = [target for target in cdp.send("Target.getTargets")["targetInfos"]
                 if target.get("type") == "page"]
        return bool(default_id and pages) and all(
            target.get("browserContextId") and target["browserContextId"] != default_id
            for target in pages)
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.stop()
        except Exception:  # noqa: BLE001
            pass


def fetch_page_source(host: str, port: int = 9222, max_chars: int = 20000) -> str:
    """CDP 抓当前页面 HTML 源码。失败返回空串。"""
    try:
        p, browser = _connect_browser(host, port)
    except Exception:  # noqa: BLE001
        return ""
    try:
        pages = browser.contexts[0].pages
        if not pages:
            return ""
        # 选当前页面：优先最后一个 http/https 网页
        page = None
        for pg in reversed(pages):
            if _is_web_page(pg.url):
                page = pg
                break
        if page is None:
            page = pages[-1]
        html = page.content()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.stop()
        except Exception:  # noqa: BLE001
            pass
    return html[:max_chars] + ("\n...(HTML 过长已截断)" if len(html) > max_chars else "")


def fetch_accessibility_tree(host: str, port: int = 9222, max_chars: int = 12000) -> str:
    """CDP 抓当前页面 AX-Tree（Accessibility.getFullAXTree），序列化成可读文本。失败返回空串。"""
    try:
        p, browser = _connect_browser(host, port)
    except Exception:  # noqa: BLE001
        return ""
    try:
        pages = browser.contexts[0].pages
        if not pages:
            return ""
        page = None
        for pg in reversed(pages):
            if _is_web_page(pg.url):
                page = pg
                break
        if page is None:
            page = pages[-1]
        cdp = browser.contexts[0].new_cdp_session(page)
        result = cdp.send("Accessibility.getFullAXTree")
        nodes = result.get("nodes") or []
        text = _format_ax_nodes(nodes)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.stop()
        except Exception:  # noqa: BLE001
            pass
    return text[:max_chars] + ("\n...(AX-Tree 过长已截断)" if len(text) > max_chars else "")


def _format_ax_nodes(nodes: List[dict]) -> str:
    """把 CDP AXTree 的 nodes 列表格式化成「role "name" value」的可读文本。

    nodes 里每个节点有 role/name/value/ignored/childIds 等。只保留非 ignored、
    有 name 或有 role 的语义节点，按缩进层级输出。
    """
    lines: List[str] = []
    # childIds 构建索引，从 root 开始 DFS
    by_id = {n.get("nodeId"): n for n in nodes}
    children = {n.get("nodeId"): n.get("childIds") or [] for n in nodes}
    has_parent = set()
    for n in nodes:
        for c in (n.get("childIds") or []):
            has_parent.add(c)
    roots = [n.get("nodeId") for n in nodes if n.get("nodeId") not in has_parent]

    # 逐字符的 InlineTextBox / LineBreak / generic 是噪音，跳过
    _SKIP_ROLES = {"InlineTextBox", "LineBreak", "generic"}

    def render_children(child_ids, depth):
        """渲染一组子节点；把相邻的 StaticText 合并成一个（CDP 会把一段文本拆成逐字符 StaticText）。"""
        i = 0
        while i < len(child_ids):
            cid = child_ids[i]
            cnode = by_id.get(cid)
            if cnode is None:
                i += 1
                continue
            crole = (cnode.get("role") or {}).get("value", "")
            if crole == "StaticText":
                texts = []
                j = i
                while j < len(child_ids):
                    jn = by_id.get(child_ids[j])
                    jrole = (jn.get("role") or {}).get("value", "") if jn else ""
                    if jrole != "StaticText":
                        break
                    texts.append((jn.get("name") or {}).get("value", ""))
                    j += 1
                merged = "".join(texts)
                if merged.strip():
                    lines.append("  " * depth + f'StaticText "{merged}"')
                i = j
            else:
                walk(cid, depth)
                i += 1

    def walk(node_id, depth):
        node = by_id.get(node_id)
        if node is None:
            return
        role = (node.get("role") or {}).get("value", "")
        if role in _SKIP_ROLES:
            render_children(children.get(node_id, []), depth)
            return
        ignored = node.get("ignored", False)
        if ignored:
            render_children(children.get(node_id, []), depth)
            return
        name = (node.get("name") or {}).get("value", "")
        value = (node.get("value") or {}).get("value", "")
        parts = [role]
        if name:
            parts.append(f'"{name}"')
        if value and value != name:
            parts.append(f"={value}")
        lines.append("  " * depth + " ".join(parts))
        render_children(children.get(node_id, []), depth + 1)

    for r in roots:
        walk(r, 0)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 一次性探测（焦点检测 + 抓取）
# --------------------------------------------------------------------------- #
def probe_browser(host: Optional[str], port: int = 9222,
                  os_name: Optional[str] = None,
                  force: bool = False) -> BrowserState:
    """焦点检测 + CDP 抓取，返回 BrowserState。

    - force=True：跳过焦点检测，直接尝试抓取（本机测试用）。
    - host 为空时无法连 CDP，只做焦点检测。
    """
    st = BrowserState()
    if not force and not detect_foreground_browser(os_name):
        return st
    if not host:
        st.is_browser = True
        st.error = "未提供 host，无法连 CDP"
        return st
    pages = list_cdp_pages(host, port)
    page = _current_page(pages)
    if page is None:
        st.is_browser = True
        st.error = "Chrome 没有打开的网页"
        return st
    st.is_browser = True
    st.url = page.get("url", "")
    st.page_source = fetch_page_source(host, port)
    st.a11y_tree = fetch_accessibility_tree(host, port)
    return st


# --------------------------------------------------------------------------- #
# 浏览器操作（DOM 定位，跳过 grounding 模型）
# --------------------------------------------------------------------------- #
# 用 Runtime.evaluate 在前端生成「可交互元素 + selector + role/name + 坐标」。
# 这是 browser use 的核心：模型看到 selector 直接操作，不需要视觉 grounding 猜坐标。
_INTERACTIVE_JS = """
() => {
  const sel = 'a,button,input,textarea,select,option,label,[role="button"],[role="link"],[role="textbox"],[role="checkbox"],[role="combobox"],[role="radio"],[role="menuitem"],[role="tab"],[contenteditable="true"],[onclick]';
  const out = [];
  document.querySelectorAll(sel).forEach(el => {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 60);
    let selector;
    if (el.id) selector = `#${el.id}`;
    else if (el.getAttribute('name')) selector = `[name="${el.getAttribute('name')}"]`;
    else if (el.getAttribute('aria-label')) selector = `[aria-label="${el.getAttribute('aria-label')}"]`;
    else if (text) selector = `${tag}:has-text("${text.replace(/"/g, '\\\\"')}")`;
    else selector = tag;
    out.push({
      role, name: text, selector, tag,
      x: Math.round(rect.x), y: Math.round(rect.y),
      w: Math.round(rect.width), h: Math.round(rect.height),
    });
  });
  const seen = new Set();
  return out.filter(o => { if (seen.has(o.selector)) return false; seen.add(o.selector); return true; });
}
"""


def fetch_interactive_elements(host: str, port: int = 9222, max_items: int = 200) -> List[dict]:
    """用 Runtime.evaluate 生成「可交互元素 + selector + role/name + 坐标」列表。失败返回 []。"""
    try:
        p, browser = _connect_browser(host, port)
    except Exception:  # noqa: BLE001
        return []
    try:
        pages = browser.contexts[0].pages
        if not pages:
            return []
        page = None
        for pg in reversed(pages):
            if _is_web_page(pg.url):
                page = pg
                break
        if page is None:
            page = pages[-1]
        items = page.evaluate(_INTERACTIVE_JS)
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.stop()
        except Exception:  # noqa: BLE001
            pass
    return (items or [])[:max_items]


def interactive_elements_to_text(items: List[dict], max_chars: int = 8000) -> str:
    """把可交互元素列表格式化成「role "name" [selector]」的可读文本（给决策模型看）。"""
    lines = []
    for it in items:
        role = it.get("role", "")
        name = it.get("name", "")
        selector = it.get("selector", "")
        parts = [role]
        if name:
            parts.append(f'"{name}"')
        parts.append(f"[{selector}]")
        lines.append("  ".join(parts))
    text = "\n".join(lines)
    return text[:max_chars] + ("\n...(可交互元素过多已截断)" if len(text) > max_chars else "")


def click_element(host: str, port: int, selector: str) -> str:
    """CDP 按 selector 点击元素（DOM 定位，不走 grounding）。返回结果说明。"""
    return _browser_action(host, port, lambda page: page.click(selector, timeout=5000))


def fill_element(host: str, port: int, selector: str, text: str) -> str:
    """CDP 按 selector 填表（先清空再输入）。返回结果说明。"""
    def _fill(page):
        page.click(selector, timeout=5000)
        page.fill(selector, text, timeout=5000)
    return _browser_action(host, port, _fill)


def navigate(host: str, port: int, url: str) -> str:
    """CDP 导航到 url。返回结果说明。"""
    return _browser_action(host, port, lambda page: page.goto(url, wait_until="commit", timeout=30000))


def evaluate_js(host: str, port: int, js: str) -> str:
    """CDP 执行 JS，返回执行结果（JSON 序列化）。"""
    def _eval(page):
        result = page.evaluate(js)
        import json as _json
        return _json.dumps(result, ensure_ascii=False)
    return _browser_action(host, port, _eval)


def _browser_action(host: str, port: int, fn) -> str:
    """连接浏览器 → 执行 fn(page) → 返回结果说明。统一异常处理。"""
    try:
        p, browser = _connect_browser(host, port)
    except Exception as exc:  # noqa: BLE001
        return f"浏览器操作失败（连接不上 CDP）: {exc}"
    try:
        pages = browser.contexts[0].pages
        if not pages:
            return "浏览器操作失败：没有打开的标签页"
        page = None
        for pg in reversed(pages):
            if _is_web_page(pg.url):
                page = pg
                break
        if page is None:
            page = pages[-1]
        result = fn(page)
    except Exception as exc:  # noqa: BLE001
        return f"浏览器操作失败: {exc}"
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.stop()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(result, str):
        return result or "已执行"
    return "已执行"
