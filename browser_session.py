"""Chromium AX/DOM 观察 + 快照范围内的确定性动作（借鉴 osworld_agent_v2）。

与 v2 的差异（重要）：**每次 collect/execute 都用 with sync_playwright() 临时连接、
用完即关**，不长期持有 playwright 连接——否则会和 OSWorld 的 setup/evaluator
（同样用 playwright sync API）在同一个进程里产生 asyncio loop 冲突。

策略输出语义目标 target_ref（快照节点引用），执行器按 backend_id 精确定位。
DOM 坐标是 CSS viewport 坐标，不是桌面截图像素。
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass


class BrowserActionError(RuntimeError):
    def __init__(self, message, dispatched=False):
        super().__init__(message)
        self.dispatched = dispatched


INTERACTIVE = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
               "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
               "spinbutton", "listbox", "option", "treeitem"}
SKIP = {"none", "generic", "InlineTextBox", "LineBreak"}


@dataclass
class BrowserOptions:
    endpoint: str = "http://127.0.0.1:9222"
    timeout_ms: int = 4000
    max_nodes: int = 4000
    max_snapshot_age: float = 180.0
    require_focus: bool = True   # True=只选择聚焦页面；普通桌面/浏览器混合任务保持 True


class BrowserSession:
    """每次操作临时连接 CDP，用完即关（避免 playwright asyncio loop 跨调用冲突）。"""

    def __init__(self, options=None, foreground_check=None, downloads_dir=None):
        self.options = options or BrowserOptions()
        self.foreground_check = foreground_check
        self.downloads_dir = downloads_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "downloads")
        self.snapshot = None
        self.nodes = {}
        self._downloaded = {}  # url -> path，本次会话已下载的文件（幂等去重）

    # ---- 临时连接的 attach 帮助 ----
    def _connect(self):
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        try:
            browser = p.chromium.connect_over_cdp(self.options.endpoint, timeout=self.options.timeout_ms)
        except Exception:
            p.stop()
            raise
        return p, browser

    @staticmethod
    def _focused_page(browser):
        """找聚焦的网页或空白/新标签页；不唯一则返回 None。"""
        candidates = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                if not (page.url.startswith(("http://", "https://", "file://"))
                        or page.url in ("about:blank", "chrome://newtab/", "edge://newtab/")):
                    continue
                try:
                    if page.evaluate("document.hasFocus() && document.visibilityState === 'visible'"):
                        candidates.append(page)
                except Exception:
                    continue
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _any_page(browser):
        """随便挑一个页面：优先 http 网页，其次任意页面（含 about:blank，可导航）。"""
        for ctx in browser.contexts:
            for page in ctx.pages:
                if page.url.startswith(("http://", "https://", "file://")):
                    return page
            if ctx.pages:
                return ctx.pages[0]
        return None

    def _pick_page(self, browser):
        """按 require_focus 选页面：聚焦模式用 _focused_page，否则 _any_page。"""
        if self.foreground_check and not self.foreground_check():
            self.invalidate()
            return None
        if self.options.require_focus:
            return self._focused_page(browser)
        return self._any_page(browser)

    def activate(self):
        """显式访问网址时激活浏览器；普通观察不会调用或改变焦点。"""
        try:
            p, browser = self._connect()
        except Exception:
            return False
        try:
            page = self._focused_page(browser)
            if page is None:
                visible = []
                for ctx in browser.contexts:
                    for candidate in ctx.pages:
                        if candidate.url.startswith(("devtools://", "chrome-extension://")):
                            continue
                        try:
                            if candidate.evaluate("document.visibilityState === 'visible'"):
                                visible.append(candidate)
                        except Exception:
                            continue
                # 多个窗口都可见时无法确定目标，交给重新观察，不任意选页面。
                page = visible[0] if len(visible) == 1 else None
            if page is None:
                return False
            page.bring_to_front()
            return True
        except Exception:
            return False
        finally:
            try:
                browser.close()  # 断开 CDP 临时连接，保留浏览器进程和页面。
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    # ---- collect ----
    def collect(self):
        start = time.perf_counter()
        if self.foreground_check and not self.foreground_check():
            self.snapshot = None
            self.nodes = {}
            return None
        try:
            p, browser = self._connect()
        except Exception:
            return None
        try:
            page = self._pick_page(browser)
            if page is None:
                return None
            cdp = page.context.new_cdp_session(page)
            cdp.send("Accessibility.enable")
            doc_before = cdp.send("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]
            url = page.url
            viewport = page.evaluate("({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,dpr:devicePixelRatio})")
            dom = cdp.send("DOMSnapshot.captureSnapshot", {"computedStyles": []})
            strings, layout = dom["strings"], {}
            for doc in dom["documents"]:
                dn = doc["nodes"]
                li = doc.get("layout", {})
                bounds = dict(zip(li.get("nodeIndex", []), li.get("bounds", [])))
                for i, backend in enumerate(dn.get("backendNodeId", [])):
                    attrs = dn.get("attributes", [])[i]
                    at = {strings[attrs[k]]: strings[attrs[k + 1]] for k in range(0, len(attrs), 2)}
                    layout[backend] = {"bounds": bounds.get(i), "tag": strings[dn["nodeName"][i]].lower(),
                                       "attrs": at, "frame_id": strings[doc["frameId"]]}
            main_frame = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
            raw = cdp.send("Accessibility.getFullAXTree")["nodes"]
            snapshot_id = uuid.uuid4().hex[:12]
            retained = [n for n in raw if not n.get("ignored") and n.get("role", {}).get("value") not in SKIP]
            raw_map = {n["nodeId"]: n for n in raw}
            refs = {n["nodeId"]: str(i) for i, n in enumerate(retained)}  # 短数字 ref（ComponentBench 风格，模型易抄）
            nodes = []
            for n in retained[:self.options.max_nodes]:
                backend = n.get("backendDOMNodeId")
                detail = layout.get(backend, {})
                attrs = detail.get("attrs", {})
                role = n.get("role", {}).get("value", "")
                states = {p["name"]: p.get("value", {}).get("value") for p in n.get("properties", [])
                          if p["name"] in {"checked", "selected", "expanded", "disabled", "focused", "required", "readonly", "level"}}
                parent = n.get("parentId")
                seen = set()
                while parent and parent not in refs and parent not in seen:
                    seen.add(parent)
                    parent = raw_map.get(parent, {}).get("parentId")
                bounds = detail.get("bounds")
                visible = bool(bounds and bounds[2] > 0 and bounds[3] > 0 and
                    bounds[0] < viewport["x"] + viewport["width"] and bounds[0] + bounds[2] > viewport["x"] and
                    bounds[1] < viewport["y"] + viewport["height"] and bounds[1] + bounds[3] > viewport["y"])
                node = {"ref": refs[n["nodeId"]], "parent": refs.get(parent), "role": role,
                        "name": str(n.get("name", {}).get("value", ""))[:400], "states": states,
                        "backend_id": backend, "frame_id": detail.get("frame_id"),
                        "tag": detail.get("tag", ""), "bounds": bounds, "in_viewport": visible,
                        "direct": bool(backend and role in INTERACTIVE and detail.get("frame_id") == main_frame)}
                if "value" in n:
                    node["value"] = "[redacted]" if attrs.get("type") == "password" else str(n["value"].get("value", ""))[:400]
                if attrs.get("href"):
                    node["href"] = attrs["href"][:500]
                if attrs.get("id"):
                    node["id"] = attrs["id"][:200]
                nodes.append(node)
            warnings = []
            if len(retained) > self.options.max_nodes:
                warnings.append("node_limit: additional nodes omitted")
            if len(page.frames) > 1:
                warnings.append("iframe content may be incomplete; iframe actions use visual fallback")
            self.snapshot = {"kind": "browser_ax", "snapshot_id": snapshot_id, "url": url,
                "title": page.title(), "viewport": viewport, "nodes": nodes, "warnings": warnings,
                "captured_at": time.time(), "capture_ms": round((time.perf_counter() - start) * 1000, 2)}
            self.nodes = {n["ref"]: n for n in nodes}
            return self.snapshot
        except Exception:
            self.snapshot = None
            self.nodes = {}
            return None
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    # ---- route ----
    def _ref_hint(self, observation, limit=20):
        """生成"当前可用元素编号"提示，帮模型从编造/过期的 ref 里纠正过来。"""
        refs = observation.info.get("exposed_refs") or list(self.nodes.keys())
        parts = []
        for ref in refs:
            n = self.nodes.get(ref)
            if not n:
                continue
            name = (n.get("name") or "").strip()
            role = n.get("role") or ""
            if name:
                parts.append(f"[{ref}] {role} '{name}'")
            if len(parts) >= limit:
                break
        return "；".join(parts) if parts else "（无）"

    def route(self, action, observation):
        ref = action.args.get("target_ref")
        if not ref:
            # 模型输出 HTML id → 解析成 ref（比 12 位 hex ref 更短、更易抄）
            eid = action.args.get("element_id")
            if eid:
                ref = self.resolve_id(str(eid))
        if not ref:
            return None
        if ref not in observation.info.get("exposed_refs", self.nodes):
            hint = self._ref_hint(observation)
            return {"channel": "reobserve",
                    "reason": f"target_ref '{ref}' 不在当前页面里，可能是编造或过期的编号。"
                              f"当前页面可用的元素编号（部分）：{hint}。"
                              f"请从中选一个正确的 ref，或改用 element_id / 英文 target。"}
        if not self.snapshot or ref not in self.nodes:
            return {"channel": "reobserve", "reason": "Unknown or stale target_ref"}
        if action.action not in {"click", "double_click", "right_click", "type", "select", "scroll"}:
            return None
        node = self.nodes[ref]
        if not node["direct"] and action.action != "scroll":
            return None
        return {"channel": "browser_dom", "snapshot_id": self.snapshot["snapshot_id"],
                "target_ref": ref, "coordinate_space": "css_viewport"}

    def resolve_id(self, element_id):
        """按 HTML id 属性找节点，返回 ref 或 None。"""
        if not self.nodes or not element_id:
            return None
        for ref, n in self.nodes.items():
            if n.get("id") == element_id:
                return ref
        return None

    # ---- 语义回退：target 描述 → 节点 ref ----
    _STOPWORDS = {
        "the", "a", "an", "in", "of", "on", "at", "to", "and", "or", "with", "by",
        "for", "from", "is", "are", "this", "that", "it", "its", "left", "right",
        "top", "bottom", "center", "side", "menu", "dropdown", "option", "button",
        "icon", "tab", "link", "page", "section", "panel", "bar", "area", "field",
        "box", "item", "row", "list", "dialog", "window", "popup", "select",
    }

    def resolve_target(self, target, allowed=None):
        """把英文 target 描述确定性匹配到快照里的可交互节点，返回 best ref 或 None。

        供 base 模型兜底：它不抄 target_ref 时，用 target 描述在 AX 快照里找 ref，
        命中即走 DOM 定位（跳过视觉定位）；匹配不置信返回 None → 交给视觉定位。
        allowed：可选 set，只在这些 ref（= 已暴露给模型的节点）里匹配。
        """
        if not self.snapshot or not self.nodes:
            return None
        t = (target or "").lower()
        if not t:
            return None
        # 模型有时把 ref 直接写进 target 描述里（如 "[87]"）→ 直接提取用
        m = re.search(r"\[(\d+)\]", t)
        if m and m.group(1) in self.nodes:
            if allowed is None or m.group(1) in allowed:
                return m.group(1)
        # 引号里的精确短语（模型通常写成 "The 'X' option in ..."）
        quoted = re.findall(
            r"[\"'`\u2018\u2019\u201c\u201d]([^\"'`\u2018\u2019\u201c\u201d]{2,80})"
            r"[\"'`\u2018\u2019\u201c\u201d]", t)
        # 方括号里的短语（模型写成 "The [PDF] xxx link" 这种）也当精确短语提取
        quoted += re.findall(r"\[([^\[\]]{2,80})\]", t)
        tokens = set(re.findall(r"[a-z0-9]+", t)) - self._STOPWORDS
        allowed_set = set(allowed) if allowed else None
        best_ref, best_score = None, 0
        for ref, n in self.nodes.items():
            if allowed_set is not None and ref not in allowed_set:
                continue
            if not n.get("direct"):
                continue
            name = (n.get("name") or "").lower()
            value = (n.get("value") or "").lower()
            role = (n.get("role") or "").lower()
            hay = f"{role} {name} {value}"
            score = 0
            for q in quoted:
                q = q.strip()
                if not q:
                    continue
                if name and q in name:
                    score += 40
                elif q in hay:
                    score += 24
            if tokens:
                score += 6 * sum(1 for tk in tokens if tk and (tk in name or tk in value))
            if role and role in t:
                score += 4
            if score > best_score:
                best_score, best_ref = score, ref
        # 只信「引号精确命中」这一档（≥24）或足够强的 token 命中；宁可不匹配走视觉定位，
        # 也不要在 DOM 里点错节点。
        if best_ref and best_score >= 24:
            return best_ref
        return None

    # ---- execute ----
    def execute(self, action, route):
        ref = route.get("target_ref")
        node = self.nodes.get(ref)
        if node is None:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Unknown target_ref"}
        if (self.foreground_check and not self.foreground_check()):
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Browser lost focus"}
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "No focused page"}
            if page.url != self.snapshot.get("url"):
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Page navigated"}
            cdp = page.context.new_cdp_session(page)
            backend_id = node["backend_id"]
            obj = cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})["object"].get("objectId")
            if not obj:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Target detached"}
            cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
            inspect = """function() {
              if (!this.isConnected || !(this instanceof Element)) return {error:'detached'};
              if (this.matches(':disabled') || this.getAttribute('aria-disabled') === 'true' || this.closest('[inert]')) return {error:'disabled'};
              const r=this.getBoundingClientRect(), s=getComputedStyle(this);
              if (r.width<=0 || r.height<=0 || s.visibility!=='visible' || s.display==='none') return {error:'hidden'};
              const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2;
              const y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
              let hit=document.elementFromPoint(x,y);
              while(hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
                const next=hit.shadowRoot.elementFromPoint(x,y); if (!next || next===hit) break; hit=next;
              }
              if (!hit || !(this===hit || this.contains(hit))) return {error:'occluded'};
              return {x,y,w:r.width,h:r.height,tag:this.tagName.toLowerCase(),readOnly:!!this.readOnly,
                      editable:this.isContentEditable || this.tagName==='TEXTAREA' || (this.tagName==='INPUT' && !['button','submit','checkbox','radio','file'].includes(this.type))};
            }"""
            def call(obj, fn, args=None):
                r = cdp.send("Runtime.callFunctionOn", {"objectId": obj, "functionDeclaration": fn,
                    "arguments": [{"value": a} for a in (args or [])], "returnByValue": True})
                if r.get("exceptionDetails"):
                    raise BrowserActionError("DOM inspection failed")
                return r.get("result", {}).get("value")
            first = call(obj, inspect)
            page.wait_for_timeout(35)
            point = call(obj, inspect)
            if not point or point.get("error"):
                raise BrowserActionError((point or {}).get("error", "Target not actionable"))
            if first != point:
                raise BrowserActionError("Target moved during actionability check")
            name, args = action.action, action.args
            # 点击的是 PDF 链接 → 直接 HTTP 下载保存（而非浏览器打开 PDF 查看器）
            if name in ("click", "right_click", "double_click") and node.get("href"):
                try:
                    from urllib.parse import urljoin
                    full = urljoin(self.snapshot.get("url", ""), node["href"])
                except Exception:  # noqa: BLE001
                    full = node["href"]
                _u = full.lower()
                # PDF 链接判定：.pdf 后缀 / 查询/锚点参数 / 路径含 /pdf/（arXiv 的 /pdf/1706.03762 这类）
                if (_u.endswith(".pdf") or ".pdf?" in _u or ".pdf#" in _u
                        or "/pdf/" in _u or _u.endswith("/pdf")):
                    return self.download(full)
            if name == "type" and (not point["editable"] or point["readOnly"]):
                raise BrowserActionError("Target is not editable")
            if name == "select" and point["tag"] != "select":
                raise BrowserActionError("Custom dropdown: use click and then click its option")
            if name == "select":
                matches = call(obj, "function(label){return Array.from(this.options).filter(o=>o.label===label && !o.disabled).map(o=>o.index)}", [str(args["option"])])
                if len(matches) != 1:
                    raise BrowserActionError("Select option is absent or ambiguous")
                dispatched = True
                call(obj, """function(i){ this.selectedIndex=i; this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true})); }""", [matches[0]])
            elif name == "scroll":
                dispatched = True
                page.mouse.move(point["x"], point["y"])
                page.mouse.wheel(0, int(args["amount"]) * 120 * (-1 if args["direction"] == "up" else 1))
            else:
                dispatched = True
                page.mouse.click(point["x"], point["y"], button="right" if name == "right_click" else "left",
                                  click_count=2 if name == "double_click" else 1)
                if name == "type":
                    if not call(obj, "function(){let a=document.activeElement;while(a&&a.shadowRoot&&a.shadowRoot.activeElement)a=a.shadowRoot.activeElement;return a===this || this.contains(a)}"):
                        raise BrowserActionError("Focus changed after click", dispatched=True)
                    if args.get("overwrite"):
                        page.keyboard.press("ControlOrMeta+A")
                        page.keyboard.press("Backspace")
                    page.keyboard.insert_text(str(args["text"]))
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "target_ref": ref, "coord": [point["x"], point["y"]],
                    "coordinate_space": "css_viewport", "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "error": str(exc)[:500],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    # ---- 纯 CDP 浏览器动作（不动真实鼠标键盘）----
    _KEY_MAP = {
        "ctrl": "ControlOrMeta", "control": "ControlOrMeta", "cmd": "Meta", "win": "Meta",
        "esc": "Escape", "escape": "Escape", "return": "Enter", "enter": "Enter",
        "del": "Delete", "delete": "Delete", "backspace": "Backspace",
        "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
        "tab": "Tab", "space": " ", "pageup": "PageUp", "pagedown": "PageDown",
        "home": "Home", "end": "End", "shift": "Shift", "alt": "Alt",
    }

    @classmethod
    def _norm_key(cls, key):
        k = (key or "").strip().lower()
        return cls._KEY_MAP.get(k, key)

    def goto(self, url):
        """CDP 导航（虚拟，不动真实鼠标键盘）。"""
        url = (url or "").strip()
        if not url:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": "empty url"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        started = time.perf_counter()
        try:
            p, browser = self._connect()
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "error": "No page"}
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "action": "goto", "url": page.url[:300],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:
            return {"status": "uncertain", "dispatched": False, "channel": "browser_dom",
                    "error": str(exc)[:300]}
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    def screenshot(self):
        """截当前页面 viewport（返回 PIL Image），不受其他窗口遮挡。"""
        try:
            p, browser = self._connect()
        except Exception:
            return None
        try:
            page = self._pick_page(browser)
            if page is None:
                return None
            data = page.screenshot(full_page=False)
            import io
            from PIL import Image
            return Image.open(io.BytesIO(data))
        except Exception:
            return None
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    def viewport_size(self):
        """返回当前页面 viewport 尺寸 (width, height)。"""
        try:
            p, browser = self._connect()
        except Exception:
            return None
        try:
            page = self._pick_page(browser)
            if page is None:
                return None
            vp = page.evaluate("({w: innerWidth, h: innerHeight})")
            return int(vp["w"]), int(vp["h"])
        except Exception:
            return None
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    def click_at_viewport(self, action, vx, vy):
        """视觉定位回退（浏览器 viewport 截图坐标系）：直接在 viewport 坐标处 CDP 虚拟点击/输入。

        与 click_at_screen 的区别：坐标已经是 viewport 坐标（因为截图就是 viewport），
        不再做屏幕→viewport 换算。
        """
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "resolved": "visual", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": "No page"}
            name = action.action
            if name in ("click", "double_click", "right_click"):
                page.mouse.click(vx, vy, button="right" if name == "right_click" else "left",
                                 click_count=2 if name == "double_click" else 1)
                dispatched = True
            elif name == "type":
                page.mouse.click(vx, vy)
                if action.args.get("overwrite"):
                    page.keyboard.press("ControlOrMeta+A")
                    page.keyboard.press("Backspace")
                page.keyboard.insert_text(str(action.args.get("text", "")))
                dispatched = True
            else:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": f"不支持的视觉动作 {name}"}
            return {"status": "executed" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "resolved": "visual",
                    "coord": [vx, vy], "coordinate_space": "css_viewport",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    def download(self, url):
        """用 HTTP 下载文件（论文 PDF 等）到 downloads 目录，返回结果 dict。

        带浏览器 User-Agent、跟随重定向；从 Content-Disposition 或 URL 提取文件名。
        """
        import requests
        import urllib3
        from urllib.parse import urlparse, unquote
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "action": "download", "error": f"无效下载地址: {url[:120]}"}
        # 幂等：本次会话已下载过这个 URL → 直接返回，不重复下载（防模型重复点击同一链接）
        if url in self._downloaded:
            path = self._downloaded[url]
            return {"status": "already_downloaded", "dispatched": False, "channel": "browser_dom",
                    "action": "download", "path": path,
                    "message": f"该文件已下载过：{path}，无需重复下载"}
        headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
        started = time.perf_counter()
        try:
            # verify=False：公司网络走 MITM 代理，Python CA 库缺公司根证书（仅影响下载，模型走 HTTP 内网）
            r = requests.get(url, headers=headers, timeout=90, stream=True,
                             allow_redirects=True, verify=False)
            if r.status_code != 200:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "action": "download", "error": f"HTTP {r.status_code}"}
            # 文件名：优先 Content-Disposition，其次 URL 路径
            filename = None
            cd = r.headers.get("Content-Disposition", "")
            m = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd, re.IGNORECASE)
            if m:
                filename = unquote(m.group(1).strip())
            if not filename:
                filename = unquote(os.path.basename(urlparse(url).path)) or "download"
            # 无扩展名时按 content-type 补
            if "." not in filename:
                ct = r.headers.get("Content-Type", "")
                if "pdf" in ct:
                    filename += ".pdf"
                elif "html" in ct:
                    filename += ".html"
            os.makedirs(self.downloads_dir, exist_ok=True)
            path = os.path.join(self.downloads_dir, filename)
            # 文件已存在 → 加后缀改名（xxx_1.pdf、xxx_2.pdf…），不覆盖旧下载
            if os.path.exists(path):
                base, ext = os.path.splitext(filename)
                i = 1
                while os.path.exists(os.path.join(self.downloads_dir, f"{base}_{i}{ext}")):
                    i += 1
                filename = f"{base}_{i}{ext}"
                path = os.path.join(self.downloads_dir, filename)
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
                    size += len(chunk)
            self._downloaded[url] = path
            # 落盘校验：写完后读回，确认真实存在 + 大小匹配（不靠模型自评）
            verified = False
            try:
                verified = os.path.exists(path) and os.path.getsize(path) == size and size > 0
            except Exception:  # noqa: BLE001
                verified = False
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "action": "download", "path": path, "bytes": size,
                    "verified": verified, "url": url[:200],
                    "message": (f"✅ 已成功下载到 {path}（{size} 字节），已核实落盘，任务完成" if verified
                                else f"⚠️ 下载写入异常：{path} 落盘校验失败"),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "action": "download", "error": str(exc)[:300]}

    def execute_target(self, action, target, allowed=None):
        """纯 target 描述执行：先 resolve ref → DOM 点击；否则 Playwright locator 匹配。

        全程 CDP，不动真实鼠标。allowed = 已暴露给模型的 ref 集合。
        """
        if action.action == "scroll":
            return self._scroll(action)
        ref = self.resolve_target(target, allowed=allowed)
        if ref:
            action.args["target_ref"] = ref
            route = {"channel": "browser_dom", "target_ref": ref, "coordinate_space": "css_viewport"}
            return self.execute(action, route)
        return self._execute_locator(action, target)

    def _locator_for(self, page, target):
        """按 target 描述里的引号短语，构造 Playwright locator（role/placeholder/text 依次试）。"""
        if not target:
            return None
        quoted = re.findall(
            r"[\"'`\u2018\u2019\u201c\u201d]([^\"'`\u2018\u2019\u201c\u201d]{2,80})"
            r"[\"'`\u2018\u2019\u201c\u201d]", target)
        roles = ("button", "link", "textbox", "searchbox", "combobox", "menuitem", "tab",
                 "checkbox", "radio", "option", "listbox", "switch", "slider")
        for q in quoted:
            q = q.strip()
            if not q:
                continue
            try:
                ph = page.get_by_placeholder(q)
                if ph.count() > 0:
                    return ph
            except Exception:
                pass
            for role in roles:
                try:
                    r = page.get_by_role(role, name=q)
                    if r.count() > 0:
                        return r
                except Exception:
                    pass
            try:
                t = page.get_by_text(q, exact=True)
                if t.count() > 0:
                    return t
            except Exception:
                pass
        return None

    def _execute_locator(self, action, target):
        """CDP locator 执行浏览器动作（虚拟输入）。"""
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "resolved": "locator", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "locator", "error": "No page"}
            name = action.action
            if name in ("press", "hotkey"):
                # 键盘动作不需要 locator
                if name == "press":
                    page.keyboard.press(self._norm_key(str(action.args.get("key", ""))))
                else:
                    keys = [self._norm_key(k) for k in (action.args.get("keys") or [])]
                    if keys:
                        page.keyboard.press("+".join(keys))
                dispatched = True
                return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                        "resolved": "locator", "action": name,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
            loc = self._locator_for(page, target)
            if loc is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "locator",
                        "error": "Target not found in DOM（换一种 target 描述，或该目标在 canvas/iframe）"}
            if name in ("click", "double_click", "right_click"):
                dispatched = True
                loc.first.click(button="right" if name == "right_click" else "left",
                                click_count=2 if name == "double_click" else 1, timeout=5000)
            elif name == "type":
                l = loc.first
                l.click(timeout=5000)
                if action.args.get("overwrite"):
                    page.keyboard.press("ControlOrMeta+A")
                    page.keyboard.press("Backspace")
                page.keyboard.insert_text(str(action.args.get("text", "")))
                dispatched = True
            elif name == "select":
                loc.first.select_option(str(action.args.get("option", "")), timeout=5000)
                dispatched = True
            else:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "locator", "error": f"unsupported action {name}"}
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "resolved": "locator", "action": name,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "resolved": "locator", "error": str(exc)[:300]}
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    def _scroll(self, action):
        """页面级滚动（window.scrollBy），无需 target。"""
        amount = int(action.args.get("amount", 1))
        delta = amount * 300 * (-1 if action.args.get("direction", "up") == "up" else 1)
        started = time.perf_counter()
        try:
            p, browser = self._connect()
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "error": "No page"}
            page.evaluate(f"window.scrollBy(0, {delta})")
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "action": "scroll", "delta": delta,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": str(exc)[:200]}
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    # ---- 视觉回退：grounding 屏幕坐标 → CDP 虚拟坐标点击（不动真实鼠标）----
    @staticmethod
    def _screen_to_viewport(page, sx, sy):
        """把 grounding 的屏幕像素坐标（设备像素，匹配 pyautogui 截图）转成页面 viewport 坐标。"""
        try:
            geo = page.evaluate("({sx: window.screenX, sy: window.screenY, "
                                "iw: window.innerWidth, ih: window.innerHeight, "
                                "oh: window.outerHeight, dpr: window.devicePixelRatio})")
        except Exception:
            return None
        dpr = geo.get("dpr") or 1
        chrome_css = (geo.get("oh", 0) or 0) - (geo.get("ih", 0) or 0)  # 标题栏+标签+地址栏
        vx = sx / dpr - (geo.get("sx", 0) or 0)
        vy = sy / dpr - (geo.get("sy", 0) or 0) - max(0, chrome_css)
        return int(round(vx)), int(round(vy))

    def click_at_screen(self, action, sx, sy):
        """视觉定位回退：在屏幕坐标 (sx, sy) 处用 CDP 虚拟点击/输入，不动真实鼠标。"""
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except Exception as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "resolved": "visual", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": "No page"}
            v = self._screen_to_viewport(page, sx, sy)
            if v is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": "viewport geometry unavailable"}
            vx, vy = v
            name = action.action
            if name in ("click", "double_click", "right_click"):
                page.mouse.click(vx, vy, button="right" if name == "right_click" else "left",
                                 click_count=2 if name == "double_click" else 1)
                dispatched = True
            elif name == "type":
                page.mouse.click(vx, vy)
                if action.args.get("overwrite"):
                    page.keyboard.press("ControlOrMeta+A")
                    page.keyboard.press("Backspace")
                page.keyboard.insert_text(str(action.args.get("text", "")))
                dispatched = True
            else:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": f"unsupported visual action {name}"}
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "resolved": "visual", "action": name, "coord": [vx, vy],
                    "coordinate_space": "css_viewport",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as exc:
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "resolved": "visual", "error": str(exc)[:300]}
        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                p.stop()
            except Exception:
                pass

    def invalidate(self):
        self.snapshot = None
        self.nodes = {}

    def close(self):
        self.invalidate()
