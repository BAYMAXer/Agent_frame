"""Windows Chromium AX/DOM 观察和快照范围内的确定性动作。

每次观察或执行使用临时 Playwright/CDP 连接，用完释放连接。
capture_observation 在同一连接中采集截图和结构，绑定本次运行的页面及文档。

策略输出语义目标 target_ref（快照节点引用），执行器按 backend_id 精确定位。
DOM 坐标是 CSS viewport 坐标，不是桌面截图像素。
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass

from .model.request_deadline import ModelRequestTimeout


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
    require_incognito: bool = False  # Windows 本地受管会话每次连接都核验无痕上下文
    bind_page: bool = False          # 专用浏览器任务按 target ID 操作，不依赖桌面焦点
    screenshot_max_edge: int = 1280


class BrowserSession:
    """每次操作临时连接 CDP，用完即关（避免 playwright asyncio loop 跨调用冲突）。"""

    def __init__(self, options=None, foreground_check=None, downloads_dir=None,
                 incognito_failure_callback=None):
        self.options = options or BrowserOptions()
        self.foreground_check = foreground_check
        self.incognito_failure_callback = incognito_failure_callback
        self.downloads_dir = downloads_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "downloads")
        self.snapshot = None
        self.nodes = {}
        self.page_id = None
        self.context_id = None
        self._adopted_launcher_context = False
        self._owned_page_ids = set()
        self.last_capture_info = {}
        self.request_deadline = None
        self._downloaded = {}  # url -> path，本次会话已下载的文件（幂等去重）

    # ---- 临时连接的 attach 帮助 ----
    def _check_deadline(self):
        if self.request_deadline is not None and self.request_deadline <= time.monotonic():
            raise ModelRequestTimeout("浏览器操作已超过本步共享预算")

    def _timeout_seconds(self, default):
        if self.request_deadline is None:
            return float(default)
        remaining = self.request_deadline - time.monotonic()
        if remaining <= 0:
            raise ModelRequestTimeout("浏览器操作已超过本步共享预算")
        return min(float(default), remaining)

    def _timeout_ms(self, default=30000):
        return max(1, int(self._timeout_seconds(default / 1000) * 1000))

    def _connect(self):
        self._check_deadline()
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        browser = None
        try:
            browser = p.chromium.connect_over_cdp(self.options.endpoint, timeout=self._timeout_ms(self.options.timeout_ms))
            self._check_deadline()
            if self.request_deadline is not None:
                for context in browser.contexts:
                    context.set_default_timeout(self._timeout_ms())
                    context.set_default_navigation_timeout(self._timeout_ms())
            if self.options.require_incognito:
                from .utils import _browser_pages_are_incognito
                try:
                    if not _browser_pages_are_incognito(browser):
                        raise BrowserActionError("无法确认调试浏览器仅包含无痕页面，拒绝接入")
                    self._check_deadline()
                except Exception:
                    self.invalidate()
                    if self.incognito_failure_callback is not None:
                        try:
                            self.incognito_failure_callback()
                        except Exception:
                            pass
                    raise
        except Exception:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                p.stop()
            except Exception:
                pass
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
        self._check_deadline()
        if self.options.bind_page:
            if not self.page_id:
                return None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    try:
                        self._check_deadline()
                        target_id = self._page_id(page)
                        self._check_deadline()
                        if target_id == self.page_id:
                            if self.request_deadline is not None:
                                page.set_default_timeout(self._timeout_ms())
                            return page
                    except ModelRequestTimeout:
                        raise
                    except Exception:
                        continue
            self.invalidate()
            return None
        if self.foreground_check and not self.foreground_check():
            self.invalidate()
            return None
        if self.options.require_focus:
            return self._focused_page(browser)
        return self._any_page(browser)

    @staticmethod
    def _page_id(page):
        cdp = page.context.new_cdp_session(page)
        try:
            return cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            cdp.detach()

    @staticmethod
    def _disconnect(playwright, browser):
        try:
            browser.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass

    def _follow_owned_popup(self, browser):
        self._check_deadline()
        if not self.options.bind_page or not self.context_id:
            return {}
        cdp = browser.new_browser_cdp_session()
        try:
            children = [target for target in cdp.send("Target.getTargets")["targetInfos"]
                        if target.get("type") == "page" and target.get("openerId") == self.page_id
                        and target.get("browserContextId") == self.context_id]
            self._owned_page_ids.update(target["targetId"] for target in children)
            self._check_deadline()
            if len(children) == 1:
                self.page_id = children[0]["targetId"]
                self.invalidate()
                return {"page_id": self.page_id, "followed_popup": True}
            if len(children) > 1:
                return {"warning": "操作打开多个页面，保留当前绑定页，请明确选择目标页"}
            return {}
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
            return {"warning": "无法确认操作是否打开新页面，请重新观察"}
        finally:
            cdp.detach()

    def bind_owned_page(self, launch_profile=None):
        """接管本次新启动的空白页，或在复用端点中创建隔离页；不另开启动窗口。"""
        self._check_deadline()
        p, browser = self._connect()
        try:
            if self.page_id:
                if self._pick_page(browser) is None:
                    raise BrowserActionError("本任务的浏览器页面已关闭，不能切换到其他页面")
                return self.page_id
            cdp = browser.new_browser_cdp_session()
            try:
                if launch_profile:
                    # 仅 fresh launcher 返回的专用 profile 可被接管，已有无痕端点
                    # 不提供 profile，也不能通过焦点或任取页面取得运行所有权。
                    from urllib.parse import urlsplit
                    from .run_windows_local import _validated_profile, _owned_endpoint_pids
                    port = urlsplit(self.options.endpoint).port
                    profile = _validated_profile(port, launch_profile)
                    owners = _owned_endpoint_pids(port, profile) if profile else set()
                    self._check_deadline()
                    if not owners:
                        raise BrowserActionError("不能确认启动页属于本任务的专用浏览器 profile")
                    pages = [target for target in cdp.send("Target.getTargets")["targetInfos"]
                             if target.get("type") == "page"]
                    self._check_deadline()
                    if (len(pages) == 1 and pages[0].get("url") == "about:blank"
                            and pages[0].get("browserContextId") and not pages[0].get("openerId")):
                        # Chrome --incognito 的原生 context 不支持 createTarget，但已存在的
                        # 精确 target 可直接操作；这避免再次 createContext 打开第二个窗口。
                        target = pages[0]
                        current_owners = _owned_endpoint_pids(port, profile)
                        self._check_deadline()
                        if current_owners != owners:
                            raise BrowserActionError("接管启动页时调试端口所有权发生变化")
                        self.page_id, self.context_id = target["targetId"], target["browserContextId"]
                        if self._pick_page(browser) is None:
                            self.page_id = self.context_id = None
                            raise BrowserActionError("本任务的浏览器启动页已关闭")
                        self._adopted_launcher_context = True
                        self._owned_page_ids.add(self.page_id)
                        self.invalidate()
                        return self.page_id
                # 复用端点或没有唯一可接管启动页时显式创建私有 context。
                # native --incognito context 不能用于 Target.createTarget。
                self.context_id = cdp.send("Target.createBrowserContext", {
                    "disposeOnDetach": False,
                })["browserContextId"]
                try:
                    self.page_id = cdp.send("Target.createTarget", {
                        "url": "about:blank", "browserContextId": self.context_id,
                    })["targetId"]
                    self._owned_page_ids.add(self.page_id)
                except Exception:
                    cdp.send("Target.disposeBrowserContext", {"browserContextId": self.context_id})
                    self.context_id = None
                    raise
            finally:
                cdp.detach()
            self.invalidate()
            self._check_deadline()
            return self.page_id
        finally:
            self._disconnect(p, browser)

    @staticmethod
    def _visible_page(browser):
        """唯一可见标签页；多个可见窗口时不任意选择。"""
        visible = []
        for ctx in browser.contexts:
            for page in ctx.pages:
                if page.url.startswith(("devtools://", "chrome-extension://")):
                    continue
                try:
                    if page.evaluate("document.visibilityState === 'visible'"):
                        visible.append(page)
                except Exception:
                    continue
        return visible[0] if len(visible) == 1 else None

    def activate(self):
        """显式访问网址时激活浏览器；普通观察不会调用或改变焦点。"""
        self._check_deadline()
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
            return False
        try:
            page = self._focused_page(browser)
            if page is None:
                page = self._visible_page(browser)
            self._check_deadline()
            if page is None:
                return False
            page.bring_to_front()
            self._check_deadline()
            return True
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
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
        return self._capture(include_image=False).get("snapshot")

    def capture_observation(self):
        """一次连接捕获同一页面/文档的截图和 AX；失败不混用旧结构。"""
        return self._capture(include_image=True)

    def _read_ax_tree(self, page, cdp):
        """新 CDP 会话的 AX 初始化可能晚于导航完成；只重读当前文档。"""
        started = time.perf_counter()
        delays = (75, 150, 250, 350)
        raw, meaningful, quality = [], [], "degraded"
        for attempt in range(len(delays) + 1):
            self._check_deadline()
            raw = cdp.send("Accessibility.getFullAXTree").get("nodes", [])
            self._check_deadline()
            meaningful = [node for node in raw if not node.get("ignored")
                and node.get("role", {}).get("value") not in SKIP | {"RootWebArea", "WebArea"}
                and (str(node.get("name", {}).get("value", "")).strip()
                     or node.get("role", {}).get("value") in INTERACTIVE
                     or str(node.get("value", {}).get("value", "")).strip())]
            if meaningful:
                quality = "ready"
                break
            if page.url == "about:blank":
                quality = "empty"
                break
            if attempt < len(delays):
                # Playwright 的等待会继续处理 CDP 事件，time.sleep 无法做到这一点。
                page.wait_for_timeout(self._timeout_ms(delays[attempt]))
                self._check_deadline()
        metadata = {"attempts": attempt + 1, "raw_node_count": len(raw),
            "meaningful_node_count": len(meaningful),
            "reason": "" if quality == "ready" else (
                "blank_document" if quality == "empty" else "ax_tree_not_ready"),
            "readiness_ms": round((time.perf_counter() - started) * 1000, 2)}
        return raw, quality, metadata

    def _capture(self, include_image):
        self._check_deadline()
        start = time.perf_counter()
        result = {"screenshot": None, "snapshot": None, "info": {}}
        if self.foreground_check and not self.foreground_check():
            self.invalidate()
            return result
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            self.invalidate()
            raise
        except Exception as exc:
            self._check_deadline()
            self.invalidate()
            result["info"]["capture_error"] = str(exc)[:300]
            return result
        try:
            page = self._pick_page(browser)
            if page is None:
                raise BrowserActionError("本任务的浏览器页面不可用")
            cdp = page.context.new_cdp_session(page)
            try:
                cdp.send("Accessibility.enable")
                for capture_attempt in range(2):
                    try:
                        result = self._snapshot_from_page(page, cdp, include_image, start)
                        result["info"]["capture"]["attempts"] = capture_attempt + 1
                        return result
                    except BrowserActionError as exc:
                        if "观察期间发生变化" not in str(exc) or capture_attempt == 1:
                            raise
                        self._check_deadline()
            finally:
                try:
                    cdp.detach()
                except Exception:
                    pass
        except ModelRequestTimeout:
            self.invalidate()
            raise
        except Exception as exc:
            self.invalidate()
            self._check_deadline()
            result["info"]["capture_error"] = str(exc)[:300]
            return result
        finally:
            self._disconnect(p, browser)

    def _snapshot_from_page(self, page, cdp, include_image, start):
        """仅在文档、target 和视口稳定后发布新节点映射。"""
        doc_before = cdp.send("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]
        page_id = cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        url = page.url
        viewport = page.evaluate("({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,dpr:devicePixelRatio})")
        raw, ax_quality, ax_capture = self._read_ax_tree(page, cdp)
        self._check_deadline()
        dom = cdp.send("DOMSnapshot.captureSnapshot", {"computedStyles": []})
        self._check_deadline()
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
        self._check_deadline()
        snapshot_id = uuid.uuid4().hex[:12]
        retained = [n for n in raw if not n.get("ignored") and n.get("role", {}).get("value") not in SKIP]
        raw_map = {n["nodeId"]: n for n in raw}
        refs = {n["nodeId"]: str(i) for i, n in enumerate(retained)}  # 短数字 ref（ComponentBench 风格，模型易抄）
        nodes = []
        # 保留完整本地缓存。区域筛选和上下文预算发生在读取侧，不提前截掉尾部结果。
        for n in retained:
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
        if ax_quality == "degraded":
            warnings.append("AX tree is not ready; use current viewport image or reobserve, never reuse old refs")
        if len(page.frames) > 1:
            warnings.append("iframe content may be incomplete; iframe actions use visual fallback")
        screenshot = None
        original_size = None
        if include_image:
            import io
            from PIL import Image
            self._check_deadline()
            screenshot = Image.open(io.BytesIO(page.screenshot(full_page=False, scale="css", timeout=self._timeout_ms()))).convert("RGB")
            original_size = list(screenshot.size)
            edge = self.options.screenshot_max_edge
            if edge > 0 and max(screenshot.size) > edge:
                ratio = edge / max(screenshot.size)
                screenshot = screenshot.resize((max(1, round(screenshot.width * ratio)),
                                                max(1, round(screenshot.height * ratio))),
                                               Image.Resampling.LANCZOS)
        title = page.title()
        doc_after = cdp.send("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]
        viewport_after = page.evaluate("({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,dpr:devicePixelRatio})")
        if (doc_before != doc_after or page.url != url or viewport_after != viewport
                or cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"] != page_id):
            raise BrowserActionError("页面在观察期间发生变化，请重新观察")
        self._check_deadline()
        self.snapshot = {"kind": "browser_ax", "snapshot_id": snapshot_id, "url": url,
            "page_id": page_id, "document_id": doc_before,
            "ax_quality": ax_quality, "ax_capture": ax_capture,
            "title": title, "viewport": viewport, "nodes": nodes, "warnings": warnings,
            "captured_at": time.time(), "capture_ms": round((time.perf_counter() - start) * 1000, 2)}
        self.nodes = {n["ref"]: n for n in nodes}
        image_size = list(screenshot.size) if screenshot is not None else None
        self.last_capture_info = {"page_id": page_id, "document_id": doc_before,
            "ax_quality": ax_quality, "ax_capture": dict(ax_capture),
            "snapshot_id": snapshot_id, "screenshot_coordinate_space": "css_viewport",
            "viewport_css_size": [viewport["width"], viewport["height"]],
            "screenshot_image_size": image_size,
            "screenshot_scale": {"x": image_size[0] / viewport["width"],
                                 "y": image_size[1] / viewport["height"]} if image_size else {"x": 1, "y": 1}}
        self.last_capture_info["capture"] = {
            "original_size": original_size, "input_size": image_size,
            "viewport_css_size": [viewport["width"], viewport["height"]],
            "scale": dict(self.last_capture_info["screenshot_scale"]),
            "page_id": page_id, "document_id": doc_before, "snapshot_id": snapshot_id,
            "timing_ms": self.snapshot["capture_ms"],
        }
        return {"screenshot": screenshot, "snapshot": self.snapshot, "info": dict(self.last_capture_info)}

    # ---- route ----
    def snapshot_route(self):
        snapshot = self.snapshot or {}
        return {key: snapshot.get(key) for key in ("snapshot_id", "page_id", "document_id")}

    def validate_action_snapshot(self, action):
        """模型可省略绑定字段；一旦显式提供，必须与当前快照严格一致。"""
        self._check_deadline()
        provided = {key: action.args[key] for key in ("snapshot_id", "page_id", "document_id")
                    if key in action.args}
        if not provided:
            return
        self._validate_snapshot()
        for key, value in provided.items():
            if value != self.snapshot.get(key):
                raise BrowserActionError(f"动作携带过期的 {key}，请重新观察")

    def validate_observation_snapshot(self, observation):
        self._check_deadline()
        if not self.snapshot:
            raise BrowserActionError("没有有效网页快照，请重新观察")
        info = observation.info or {}
        scene = info.get("scene") or {}
        for key in ("snapshot_id", "page_id", "document_id"):
            observed = info.get(key, scene.get(key))
            if observed is not None and observed != self.snapshot.get(key):
                raise BrowserActionError(f"Observation {key} changed")

    def _validate_snapshot(self, route=None, page=None, cdp=None):
        self._check_deadline()
        snapshot = self.snapshot
        if not snapshot:
            raise BrowserActionError("没有有效网页快照，请重新观察")
        for key in ("snapshot_id", "page_id", "document_id"):
            if route is not None and snapshot.get(key) is not None and route.get(key) != snapshot[key]:
                raise BrowserActionError(f"过期的 {key}，请重新观察")
        captured = snapshot.get("captured_at")
        if captured is not None and time.time() - captured > self.options.max_snapshot_age:
            raise BrowserActionError("网页快照已过期，请重新观察")
        if page is not None:
            if page.url != snapshot.get("url"):
                raise BrowserActionError("页面已导航，请重新观察")
            if snapshot.get("page_id") is not None:
                if cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"] != snapshot["page_id"]:
                    raise BrowserActionError("网页 target 已变化，请重新观察")
            if snapshot.get("document_id") is not None:
                document = cdp.send("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]
                if document != snapshot["document_id"]:
                    raise BrowserActionError("网页文档已刷新，请重新观察")
        self._check_deadline()

    def _ref_hint(self, observation, limit=20):
        """生成"当前可用元素编号"提示，帮模型从编造/过期的 ref 里纠正过来。"""
        refs = observation.info.get("exposed_refs", list(self.nodes.keys()))
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
        self._check_deadline()
        info = observation.info or {}
        try:
            self.validate_action_snapshot(action)
        except BrowserActionError as exc:
            return {"channel": "reobserve", "refresh_observation": True, "reason": str(exc)}
        if self.snapshot:
            try:
                self.validate_observation_snapshot(observation)
            except BrowserActionError as exc:
                return {"channel": "reobserve", "refresh_observation": True, "reason": str(exc)}
        allowed = info.get("exposed_refs", [] if self.options.bind_page else self.nodes)
        ref = action.args.get("target_ref")
        if not ref:
            # 模型输出 HTML id → 解析成 ref（比 12 位 hex ref 更短、更易抄）
            eid = action.args.get("element_id")
            if eid:
                ref = self.resolve_id(str(eid), allowed=allowed)
        if not ref:
            return None
        if ref not in allowed:
            hint = self._ref_hint(observation)
            return {"channel": "reobserve", "refresh_observation": True,
                    "reason": f"target_ref '{ref}' 不在当前页面里，可能是编造或过期的编号。"
                              f"当前页面可用的元素编号（部分）：{hint}。"
                              f"请从中选一个正确的 ref，或改用 element_id / 英文 target。"}
        if not self.snapshot or ref not in self.nodes:
            return {"channel": "reobserve", "refresh_observation": True, "reason": "Unknown or stale target_ref"}
        observed = info.get("snapshot_id") or (info.get("scene") or {}).get("snapshot_id")
        if observed and observed != self.snapshot.get("snapshot_id"):
            return {"channel": "reobserve", "refresh_observation": True, "reason": "Observation snapshot changed"}
        try:
            self._validate_snapshot()
        except BrowserActionError as exc:
            return {"channel": "reobserve", "refresh_observation": True, "reason": str(exc)}
        if action.action not in {"click", "double_click", "right_click", "type", "select", "scroll"}:
            return None
        node = self.nodes[ref]
        if not node["direct"] and action.action != "scroll":
            return None
        return {"channel": "browser_dom", **self.snapshot_route(), "exposed_refs": list(allowed),
                "target_ref": ref, "coordinate_space": "css_viewport"}

    def resolve_id(self, element_id, allowed=None):
        """按 HTML id 属性找节点，返回 ref 或 None。"""
        if not self.nodes or not element_id:
            return None
        matches = [ref for ref, n in self.nodes.items() if n.get("id") == element_id
                   and (allowed is None or ref in allowed)]
        return matches[0] if len(matches) == 1 else None

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
        allowed_set = set(allowed) if allowed is not None else None
        best_ref, best_score, tied = None, 0, False
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
                best_score, best_ref, tied = score, ref, False
            elif score == best_score and score > 0:
                tied = True
        # 只信「引号精确命中」这一档（≥24）或足够强的 token 命中；宁可不匹配走视觉定位，
        # 也不要在 DOM 里点错节点。
        if best_ref and best_score >= 24 and not tied:
            return best_ref
        return None

    # ---- execute ----
    def execute(self, action, route):
        self._check_deadline()
        ref = route.get("target_ref")
        node = self.nodes.get(ref)
        if node is None:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Unknown target_ref"}
        if self.options.bind_page and ref not in route.get("exposed_refs", []):
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Target was not exposed"}
        try:
            self.validate_action_snapshot(action)
            self._validate_snapshot(route)
        except BrowserActionError as exc:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": str(exc)}
        if (self.foreground_check and not self.foreground_check()):
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Browser lost focus"}
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "No focused page"}
            if page.url != self.snapshot.get("url"):
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Page navigated"}
            cdp = page.context.new_cdp_session(page)
            self._validate_snapshot(route, page, cdp)
            backend_id = node["backend_id"]
            obj = cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})["object"].get("objectId")
            self._check_deadline()
            if not obj:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": "Target detached"}
            cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
            self._check_deadline()
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
                self._check_deadline()
                r = cdp.send("Runtime.callFunctionOn", {"objectId": obj, "functionDeclaration": fn,
                    "arguments": [{"value": a} for a in (args or [])], "returnByValue": True})
                if r.get("exceptionDetails"):
                    raise BrowserActionError("DOM inspection failed")
                self._check_deadline()
                return r.get("result", {}).get("value")
            first = call(obj, inspect)
            self._check_deadline()
            page.wait_for_timeout(min(35, self._timeout_ms(35)))
            self._check_deadline()
            point = call(obj, inspect)
            if not point or point.get("error"):
                raise BrowserActionError((point or {}).get("error", "Target not actionable"))
            if first != point:
                raise BrowserActionError("Target moved during actionability check")
            name, args = action.action, action.args
            self._check_deadline()
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
                self._check_deadline()
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
                        self._check_deadline()
                        page.keyboard.press("ControlOrMeta+A")
                        self._check_deadline()
                        page.keyboard.press("Backspace")
                    self._check_deadline()
                    page.keyboard.insert_text(str(args["text"]))
            self._check_deadline()
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "target_ref": ref, "coord": [point["x"], point["y"]],
                    **self._follow_owned_popup(browser),
                    "coordinate_space": "css_viewport", "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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
        self._check_deadline()
        url = (url or "").strip()
        if not url:
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": "empty url"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        started = time.perf_counter()
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if (page is None and self.options.require_incognito and self.foreground_check
                    and self.foreground_check()):
                # 明确导航可在已核验无痕窗口的地址栏聚焦时，通过 CDP 操作唯一可见页。
                # 点击、输入及观察仍要求 document.hasFocus，不能混用 chrome UI 坐标。
                page = self._visible_page(browser)
                if not self.foreground_check():
                    page = None
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "error": "No page"}
            self._check_deadline()
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms())
            self._check_deadline()
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "action": "goto", "url": page.url[:300],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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

    def get_page_source(self, max_chars=20000):
        """读取当前焦点页面；使用同一连接的无痕核验，不回退到其他标签页。"""
        self._check_deadline()
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
            return ""
        try:
            page = self._pick_page(browser)
            if page is None:
                return ""
            cdp = None
            if self.options.bind_page:
                cdp = page.context.new_cdp_session(page)
                self._validate_snapshot(page=page, cdp=cdp)
            if self.foreground_check and not self.foreground_check():
                self.invalidate()
                return ""
            html = page.content()
            self._check_deadline()
            if cdp is not None:
                self._validate_snapshot(page=page, cdp=cdp)
            if self.foreground_check and not self.foreground_check():
                self.invalidate()
                return ""
            return html[:max(0, int(max_chars))]
        except ModelRequestTimeout:
            self.invalidate()
            raise
        except Exception:
            if self.options.bind_page:
                self.invalidate()
            self._check_deadline()
            return ""
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
        self._check_deadline()
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
            return None
        try:
            page = self._pick_page(browser)
            if page is None:
                return None
            data = page.screenshot(full_page=False, timeout=self._timeout_ms())
            self._check_deadline()
            import io
            from PIL import Image
            return Image.open(io.BytesIO(data))
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
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
        self._check_deadline()
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
            return None
        try:
            page = self._pick_page(browser)
            if page is None:
                return None
            vp = page.evaluate("({w: innerWidth, h: innerHeight})")
            self._check_deadline()
            return int(vp["w"]), int(vp["h"])
        except ModelRequestTimeout:
            raise
        except Exception:
            self._check_deadline()
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
        self._check_deadline()
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "resolved": "visual", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": "No page"}
            if self.options.bind_page:
                self.validate_action_snapshot(action)
                cdp = page.context.new_cdp_session(page)
                self._validate_snapshot(page=page, cdp=cdp)
                current = page.evaluate("({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,dpr:devicePixelRatio})")
                if current != self.snapshot.get("viewport"):
                    raise BrowserActionError("视口在视觉定位后发生变化，请重新观察")
                if not (0 <= vx < current["width"] and 0 <= vy < current["height"]):
                    raise BrowserActionError("视觉坐标超出网页视口")
            name = action.action
            self._check_deadline()
            if name in ("click", "double_click", "right_click"):
                dispatched = True
                page.mouse.click(vx, vy, button="right" if name == "right_click" else "left",
                                 click_count=2 if name == "double_click" else 1)
            elif name == "type":
                dispatched = True
                page.mouse.click(vx, vy)
                self._check_deadline()
                if action.args.get("overwrite"):
                    page.keyboard.press("ControlOrMeta+A")
                    self._check_deadline()
                    page.keyboard.press("Backspace")
                self._check_deadline()
                page.keyboard.insert_text(str(action.args.get("text", "")))
            else:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "visual", "error": f"不支持的视觉动作 {name}"}
            self._check_deadline()
            return {"status": "executed" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "resolved": "visual",
                    **self._follow_owned_popup(browser),
                    "coord": [vx, vy], "coordinate_space": "css_viewport",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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

    def image_to_viewport(self, x, y):
        """缩略图像素转换为原网页 CSS viewport 坐标。"""
        self._check_deadline()
        scale = self.last_capture_info.get("screenshot_scale") or {"x": 1, "y": 1}
        return float(x) / scale["x"], float(y) / scale["y"]

    def drag_at_viewport(self, start, end):
        self._check_deadline()
        dispatched = False
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": str(exc)[:300]}
        try:
            page = self._pick_page(browser)
            if page is None:
                raise BrowserActionError("本任务的浏览器页面不可用")
            cdp = page.context.new_cdp_session(page)
            self._validate_snapshot(page=page, cdp=cdp)
            current = page.evaluate("({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,dpr:devicePixelRatio})")
            if current != self.snapshot.get("viewport"):
                raise BrowserActionError("拖拽前视口已变化，请重新观察")
            for x, y in (start, end):
                if not (0 <= x < current["width"] and 0 <= y < current["height"]):
                    raise BrowserActionError("拖拽坐标超出网页视口")
            page.mouse.move(*start)
            self._check_deadline()
            dispatched = True
            page.mouse.down()
            try:
                page.mouse.move(*end, steps=12)
            finally:
                page.mouse.up()
            self._check_deadline()
            return {"status": "executed", "dispatched": True, "channel": "browser_dom", "resolved": "visual"}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "error": str(exc)[:300]}
        finally:
            self._disconnect(p, browser)

    def download(self, url):
        """用 HTTP 下载文件（论文 PDF 等）到 downloads 目录，返回结果 dict。

        带浏览器 User-Agent、跟随重定向；从 Content-Disposition 或 URL 提取文件名。
        """
        self._check_deadline()
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
            try:
                size = os.path.getsize(path) if os.path.isfile(path) else 0
            except OSError:
                size = 0
            if size > 0:
                return {"status": "already_downloaded", "dispatched": False, "channel": "browser_dom",
                        "action": "download", "path": path, "bytes": size, "url": url,
                        "verified": True, "verification_source": "cached_file",
                        "message": f"已重新核实本地文件：{path}（{size} 字节），无需重复下载"}
            # 文件被删除、变成目录或空文件时，缓存不能阻止重新下载。
            self._downloaded.pop(url, None)
        headers = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
        started = time.perf_counter()
        r = None
        partial_path = None
        try:
            # verify=False：公司网络走 MITM 代理，Python CA 库缺公司根证书（仅影响下载，模型走 HTTP 内网）
            r = requests.get(url, headers=headers, timeout=self._timeout_seconds(90), stream=True,
                             allow_redirects=True, verify=False)
            self._check_deadline()
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
            partial_path = path + "." + uuid.uuid4().hex + ".part"
            with open(partial_path, "xb") as f:
                for chunk in r.iter_content(65536):
                    self._check_deadline()
                    f.write(chunk)
                    size += len(chunk)
            self._check_deadline()
            os.replace(partial_path, path)
            partial_path = None
            self._downloaded[url] = path
            self._check_deadline()
            # 落盘校验：写完后读回，确认真实存在 + 大小匹配（不靠模型自评）
            verified = False
            try:
                verified = os.path.exists(path) and os.path.getsize(path) == size and size > 0
            except Exception:  # noqa: BLE001
                verified = False
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "action": "download", "path": path, "bytes": size,
                    "verified": verified, "url": url,
                    "message": (f"✅ 已成功下载到 {path}（{size} 字节），已核实此文件落盘" if verified
                                else f"⚠️ 下载写入异常：{path} 落盘校验失败"),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:  # noqa: BLE001
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "action": "download", "error": str(exc)[:300]}
        finally:
            if r is not None and hasattr(r, "close"):
                try:
                    r.close()
                except Exception:
                    pass
            if partial_path is not None:
                try:
                    os.remove(partial_path)
                except OSError:
                    pass

    def execute_target(self, action, target, allowed=None, route=None):
        """纯 target 描述执行：先 resolve ref → DOM 点击；否则 Playwright locator 匹配。

        全程 CDP，不动真实鼠标。allowed = 已暴露给模型的 ref 集合。
        """
        self._check_deadline()
        if self.options.bind_page:
            try:
                self.validate_action_snapshot(action)
                if route is not None:
                    self._validate_snapshot(route)
            except BrowserActionError as exc:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom", "error": str(exc)}
        if action.action == "scroll":
            return self._scroll(action)
        ref = self.resolve_target(target, allowed=allowed)
        if ref:
            action.args["target_ref"] = ref
            route = {"channel": "browser_dom", **self.snapshot_route(), "exposed_refs": list(allowed or []),
                     "target_ref": ref, "coordinate_space": "css_viewport"}
            return self.execute(action, route)
        if self.options.bind_page and action.action not in ("press", "hotkey"):
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": "目标未唯一匹配当前暴露节点，请重新观察或选择区域"}
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
        self._check_deadline()
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "resolved": "locator", "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "locator", "error": "No page"}
            if self.options.bind_page:
                self._validate_snapshot(page=page, cdp=page.context.new_cdp_session(page))
            name = action.action
            if name in ("press", "hotkey"):
                # 键盘动作不需要 locator
                self._check_deadline()
                dispatched = True
                if name == "press":
                    page.keyboard.press(self._norm_key(str(action.args.get("key", ""))))
                else:
                    keys = [self._norm_key(k) for k in (action.args.get("keys") or [])]
                    if keys:
                        page.keyboard.press("+".join(keys))
                self._check_deadline()
                return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                        "resolved": "locator", "action": name,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
            loc = self._locator_for(page, target)
            self._check_deadline()
            if loc is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "locator",
                        "error": "Target not found in DOM（换一种 target 描述，或该目标在 canvas/iframe）"}
            if loc.count() != 1:
                raise BrowserActionError("目标名称存在歧义，请指定唯一节点")
            if name in ("click", "double_click", "right_click"):
                dispatched = True
                loc.first.click(button="right" if name == "right_click" else "left",
                                click_count=2 if name == "double_click" else 1, timeout=self._timeout_ms(5000))
            elif name == "type":
                l = loc.first
                dispatched = True
                l.click(timeout=self._timeout_ms(5000))
                self._check_deadline()
                if action.args.get("overwrite"):
                    page.keyboard.press("ControlOrMeta+A")
                    self._check_deadline()
                    page.keyboard.press("Backspace")
                self._check_deadline()
                page.keyboard.insert_text(str(action.args.get("text", "")))
            elif name == "select":
                dispatched = True
                loc.first.select_option(str(action.args.get("option", "")), timeout=self._timeout_ms(5000))
            else:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "resolved": "locator", "error": f"unsupported action {name}"}
            self._check_deadline()
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "resolved": "locator", "action": name,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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
        self._check_deadline()
        amount = int(action.args.get("amount", 1))
        delta = amount * 300 * (-1 if action.args.get("direction", "up") == "up" else 1)
        started = time.perf_counter()
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "error": str(exc)[:200]}
        try:
            page = self._pick_page(browser)
            if page is None:
                return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                        "error": "No page"}
            if self.options.bind_page:
                self._validate_snapshot(page=page, cdp=page.context.new_cdp_session(page))
            self._check_deadline()
            page.evaluate(f"window.scrollBy(0, {delta})")
            self._check_deadline()
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "action": "scroll", "delta": delta,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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
        self._check_deadline()
        started, dispatched = time.perf_counter(), False
        try:
            p, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
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
        self.last_capture_info = {}

    def close(self):
        # 清理运行专属资源不受已经结束的步骤预算影响。
        self.request_deadline = None
        if self.context_id is not None:
            try:
                p, browser = self._connect()
            except Exception:
                pass
            else:
                try:
                    cdp = browser.new_browser_cdp_session()
                    try:
                        if self._adopted_launcher_context:
                            # native context 可能有用户后来打开的其它 tabs，不能整体 dispose。
                            # 只关执行期间已登记的启动页/弹出页；不能在关闭时把用户
                            # 后来打开但具有同一 opener 的页面补认作本任务所有。
                            targets = cdp.send("Target.getTargets")["targetInfos"]
                            children = {target["targetId"]: target for target in targets
                                        if target.get("type") == "page"
                                        and target.get("browserContextId") == self.context_id}
                            for target_id in self._owned_page_ids & children.keys():
                                try:
                                    cdp.send("Target.closeTarget", {"targetId": target_id})
                                except Exception:
                                    pass
                        else:
                            cdp.send("Target.disposeBrowserContext", {
                                "browserContextId": self.context_id,
                            })
                    finally:
                        cdp.detach()
                except Exception:
                    pass
                finally:
                    self._disconnect(p, browser)
        self.invalidate()
        self.page_id = None
        self.context_id = None
        self._adopted_launcher_context = False
        self._owned_page_ids.clear()
