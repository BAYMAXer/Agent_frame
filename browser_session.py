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
        self._downloaded_sizes = {}  # 保存原始大小，拒绝非空但已被截断的缓存

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
        """统一创建可管理下载的任务 context，绑定成功后才清理已证明所有权的启动页。"""
        self._check_deadline()
        p, browser = self._connect()
        try:
            if self.page_id:
                if self._pick_page(browser) is None:
                    raise BrowserActionError("本任务的浏览器页面已关闭，不能切换到其他页面")
                return self.page_id
            cdp = browser.new_browser_cdp_session()
            try:
                starter = None
                profile = port = owners = None
                if launch_profile:
                    # profile/PID 证明仅允许清理本次 fresh launcher 的唯一空白页。
                    # 原生 --incognito context 不能配置下载，不能把它用作任务 context。
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
                        starter = dict(pages[0])
                context_id = cdp.send("Target.createBrowserContext", {
                    "disposeOnDetach": False,
                })["browserContextId"]
                try:
                    page_id = cdp.send("Target.createTarget", {
                        "url": "about:blank", "browserContextId": context_id,
                    })["targetId"]
                    target = cdp.send("Target.getTargetInfo", {"targetId": page_id})["targetInfo"]
                    self._check_deadline()
                    if (target.get("targetId") != page_id or target.get("type") != "page"
                            or target.get("browserContextId") != context_id):
                        raise BrowserActionError("新建任务页面不属于预期的专用浏览器 context")
                    # 新 target 的 Playwright page 附加事件可能稍晚到达。通过 CDP 往返
                    # 继续处理事件，所有验证完成前不发布任务 page/context ID。
                    bind_deadline = time.monotonic() + self._timeout_seconds(self.options.timeout_ms / 1000)
                    bound = False
                    while not bound:
                        for ctx in browser.contexts:
                            for page in ctx.pages:
                                self._check_deadline()
                                try:
                                    bound = self._page_id(page) == page_id
                                except ModelRequestTimeout:
                                    raise
                                except Exception:
                                    continue
                                if bound:
                                    break
                            if bound:
                                break
                        if bound:
                            break
                        if time.monotonic() >= bind_deadline:
                            raise BrowserActionError("无法绑定新建的任务浏览器页面")
                        cdp.send("Target.getTargetInfo", {"targetId": page_id})
                        time.sleep(0.01)
                    if launch_profile:
                        if (_validated_profile(port, launch_profile) != profile
                                or _owned_endpoint_pids(port, profile) != owners):
                            raise BrowserActionError("绑定任务页面时调试端口所有权发生变化")
                    self._check_deadline()
                except Exception:
                    try:
                        cdp.send("Target.disposeBrowserContext", {"browserContextId": context_id})
                    except Exception:
                        pass
                    raise
                self.page_id, self.context_id = page_id, context_id
                self._adopted_launcher_context = False
                self._owned_page_ids.add(page_id)
                if starter:
                    # 不按 URL 搜索任何新页，也不取得后来出现的页面的所有权。
                    # 原启动页已导航、出现 opener 或 PID/profile 改变时保留它。
                    try:
                        current = cdp.send("Target.getTargetInfo", {
                            "targetId": starter["targetId"],
                        })["targetInfo"]
                        if (current.get("targetId") == starter["targetId"]
                                and current.get("type") == "page"
                                and current.get("browserContextId") == starter["browserContextId"]
                                and current.get("url") == "about:blank" and not current.get("openerId")
                                and _validated_profile(port, launch_profile) == profile
                                and _owned_endpoint_pids(port, profile) == owners):
                            cdp.send("Target.closeTarget", {"targetId": starter["targetId"]})
                    except Exception:
                        pass  # 清理启动页失败不撤销已经绑定的任务页面。
            finally:
                cdp.detach()
            self.invalidate()
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
    _ACTIONABILITY_INSPECT = """function() {
      const target=this;
      if (!target.isConnected || !(target instanceof Element)) return {error:'detached'};
      if (target.matches(':disabled') || target.getAttribute('aria-disabled')==='true' || target.closest('[inert]')) return {error:'disabled'};
      const rect=target.getBoundingClientRect(), style=getComputedStyle(target);
      if (rect.width<=0 || rect.height<=0 || style.visibility!=='visible' || style.display==='none') return {error:'hidden'};
      const parent=el=>el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
      const within=(root,el)=>{for(let i=0;el && i<64;i++,el=parent(el))if(el===root)return true;return false};
      const independent=el=>el.hasAttribute('onclick') || typeof el.onclick==='function' || el.matches('a[href],button,input,select,textarea,summary,label[for],[contenteditable]:not([contenteditable="false"]),[tabindex]:not([tabindex="-1"]),[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="textbox"],[role="searchbox"],[role="menuitem"],[role="option"],[role="tab"]');
      const nestedControl=el=>{for(let i=0;el && el!==target && i<64;i++,el=parent(el))if(independent(el))return el;return null};
      const role=el=>el.getAttribute('role') || ({BUTTON:'button',A:'link',SELECT:'combobox',TEXTAREA:'textbox',INPUT:el.type==='submit' || el.type==='button'?'button':el.type==='search'?'searchbox':'textbox'}[el.tagName] || '');
      const describe=el=>{
        if(!el)return {role:'',tag:'',name:'',id:''};
        const labelled=(el.getAttribute('aria-labelledby') || '').split(/\\s+/).slice(0,4).map(id=>document.getElementById(id)?.textContent || '').join(' ');
        return {role:role(el),tag:el.tagName.toLowerCase(),name:String(el.getAttribute('aria-label') || labelled || el.getAttribute('alt') || el.getAttribute('title') || el.textContent || '').replace(/\\s+/g,' ').trim().slice(0,160),id:(el.id || '').slice(0,160)};
      };
      const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0 && r.height>0 && s.display!=='none' && s.visibility==='visible'};
      const candidates=[],seen=new Set();
      const addRect=(r,source,grid=false)=>{
        const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
        if(right-left<1 || bottom-top<1)return;
        for(const [fx,fy] of grid?[[.5,.5],[.2,.5],[.8,.5],[.5,.2],[.5,.8]]:[[.5,.5]]){
          if(candidates.length>=24)return;
          const x=left+(right-left)*fx,y=top+(bottom-top)*fy,key=x.toFixed(2)+','+y.toFixed(2);
          if(!seen.has(key)){seen.add(key);candidates.push({x,y,source})}
        }
      };
      // Titles are a better point than the center of an entire result/card link.
      for(const heading of Array.from(target.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')).slice(0,3)){
        if(!nestedControl(heading))for(const r of Array.from(heading.getClientRects()).slice(0,2))addRect(r,'heading');
      }
      const walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT);
      for(let n=walker.nextNode(),visited=0;n && visited<64 && candidates.length<10;n=walker.nextNode(),visited++){
        if(!n.textContent.trim() || nestedControl(n.parentElement))continue;
        const range=document.createRange();range.selectNodeContents(n);
        for(const r of Array.from(range.getClientRects()).slice(0,2))addRect(r,'text');
      }
      for(const r of Array.from(target.getClientRects()).slice(0,3))addRect(r,'client_rect',true);
      addRect(rect,'bounding_rect',true);
      let blocker=null,nested=null;
      for(const point of candidates){
        let hit=document.elementFromPoint(point.x,point.y);
        for(let i=0;hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint && i<16;i++){
          const next=hit.shadowRoot.elementFromPoint(point.x,point.y);if(!next || next===hit)break;hit=next;
        }
        const own=hit && within(target,hit),child=own?nestedControl(hit):null;
        if(own && !child)return {x:point.x,y:point.y,w:rect.width,h:rect.height,tag:target.tagName.toLowerCase(),readOnly:!!target.readOnly,
          editable:target.isContentEditable || target.tagName==='TEXTAREA' || (target.tagName==='INPUT' && !['button','submit','checkbox','radio','file'].includes(target.type)),point_source:point.source,candidates_checked:candidates.indexOf(point)+1};
        if(!blocker || (nested && !child)){blocker=child || hit;nested=child}
      }
      let active=document.activeElement;
      for(let i=0;active?.shadowRoot?.activeElement && i<16;i++)active=active.shadowRoot.activeElement;
      const modalSelector='[aria-modal="true"],dialog[open],[role="dialog"],[role="alertdialog"]';
      let modal=null,modalScanComplete=true;
      const roots=[document],knownRoots=new Set(roots);
      const addRoot=root=>{if(root && !knownRoots.has(root)){knownRoots.add(root);roots.push(root)}};
      // Modal ownership may cross a shadow boundary. Check relevant composed
      // ancestors first, including a root reached by a resolved closed-shadow node.
      for(const seed of [target,active,blocker]){
        let el=seed,i=0;
        for(;el && i<64;i++,el=parent(el)){
          addRoot(el.getRootNode());
          if(el.matches(modalSelector) && visible(el)){modal=el;break}
        }
        if(el && i>=64)modalScanComplete=false;
        if(modal)break;
      }
      // Scan all accessible open roots, without declaring the first 16 hidden
      // dialogs to be exhaustive. An unfinished bounded scan cannot authorize Esc.
      let inspected=0;
      for(let rootIndex=0;!modal && rootIndex<roots.length;rootIndex++){
        if(rootIndex>=64){modalScanComplete=false;break}
        const elements=document.createTreeWalker(roots[rootIndex],NodeFilter.SHOW_ELEMENT);
        for(let el=elements.nextNode();el;el=elements.nextNode()){
          if(++inspected>8192){modalScanComplete=false;break}
          if(el.matches(modalSelector) && visible(el)){modal=el;break}
          if(el.shadowRoot)addRoot(el.shadowRoot);
        }
        if(!modalScanComplete)break;
      }
      let popup=null,association=null;
      const input=active && (active.matches('textarea') || active.isContentEditable ||
        (active.tagName==='INPUT' && ['text','search','email','url','tel','number'].includes(active.type)));
      if(input && active.getAttribute('aria-expanded')!=='false' && !modal && modalScanComplete && !nested){
        const ids=((active.getAttribute('aria-controls') || '')+' '+(active.getAttribute('aria-owns') || '')).trim().split(/\\s+/).slice(0,8);
        for(const id of ids){
          const root=active.getRootNode().getElementById?.(id) || document.getElementById(id);
          if(!root || !visible(root) || !within(root,blocker))continue;
          const list=root.matches('[role="listbox"]')?root:root.querySelector('[role="listbox"]');
          if(list && visible(list) && within(list,blocker)){popup=root;break}
        }
        const form=target.form || target.closest('form'),inputForm=active.form || active.closest('form');
        const search=target.closest('[role="search"]'),inputSearch=active.closest('[role="search"]');
        const button=target.matches('button,input[type="submit"],input[type="button"],[role="button"]');
        const submit=target.matches('button,input') && target.type==='submit';
        const searchButton=button && /search|搜索|搜尋/i.test(describe(target).name);
        const menu=target.hasAttribute('aria-haspopup') && target.getAttribute('aria-haspopup')!=='false';
        if(target===active)association='focused_input';
        else if(button && !menu && (submit || searchButton) && ((form && form===inputForm) || (search && search===inputSearch)))association=submit?'same_form_submit':'same_search_button';
      }
      const allowed=!!(popup && association);
      const relationship=modal?'modal':!modalScanComplete?'unverified_modal_state':allowed?'focused_autocomplete':nested?'nested_control':'unrelated';
      return {error:'occluded',error_code:'target_occluded',occlusion:{blocker:describe(blocker),relationship,recovery_allowed:allowed,
        modal_check:modal?'visible_modal':modalScanComplete?'clear':'incomplete',
        autocomplete:allowed?{input:describe(active),popup_id:popup.id,target_relation:association}:null,candidates_checked:candidates.length,
        suggestion:allowed?'已确认当前搜索输入关联的自动补全列表，可发送一次 Escape 后重新定位。':modal?'先明确操作弹窗的关闭或同意控件，再重新观察。':!modalScanComplete?'弹窗检查未完成，不能自动发送 Escape；请重新观察并明确处理遮挡。':'重新观察遮挡控件或选择可见标题节点，不要原样重复点击此目标。'}};
    }"""

    def probe_actionability(self, action, route):
        """只读核验当前精确节点是否可点；不滚动、点击或发送辅助键。"""
        self._check_deadline()
        ref = route.get("target_ref")
        node = self.nodes.get(ref)
        base = {"actionable": False, "dispatched": False, "channel": "browser_dom", "target_ref": ref}
        if node is None:
            return {**base, "error_code": "target_unknown", "error": "Unknown target_ref"}
        if self.options.bind_page and ref not in route.get("exposed_refs", []):
            return {**base, "error_code": "target_not_exposed", "error": "Target was not exposed"}
        try:
            self.validate_action_snapshot(action)
            self._validate_snapshot(route)
        except BrowserActionError as exc:
            return {**base, "error_code": "stale_snapshot", "error": str(exc)}
        if self.foreground_check and not self.foreground_check():
            return {**base, "error_code": "browser_lost_focus", "error": "Browser lost focus"}
        try:
            runtime, browser = self._connect()
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {**base, "error_code": "browser_unavailable", "error": str(exc)[:300]}
        cdp = None
        try:
            page = self._pick_page(browser)
            if page is None:
                raise BrowserActionError("本任务的浏览器页面不可用")
            cdp = page.context.new_cdp_session(page)
            self._validate_snapshot(route, page, cdp)
            obj = cdp.send("DOM.resolveNode", {"backendNodeId": node["backend_id"]})["object"].get("objectId")
            self._check_deadline()
            if not obj:
                return {**base, "error_code": "target_detached", "error": "Target detached"}
            inspected = cdp.send("Runtime.callFunctionOn", {"objectId": obj,
                "functionDeclaration": self._ACTIONABILITY_INSPECT, "arguments": [], "returnByValue": True})
            self._check_deadline()
            if inspected.get("exceptionDetails"):
                return {**base, "error_code": "target_inspection_failed", "error": "DOM inspection failed"}
            point = inspected.get("result", {}).get("value") or {}
            self._validate_snapshot(route, page, cdp)
            if point.get("error") or "x" not in point or "y" not in point:
                return {**base, "error_code": point.get("error_code") or
                        "target_" + str(point.get("error", "not_actionable")),
                        "error": point.get("error", "Target not actionable"),
                        **({"occlusion": point["occlusion"]} if point.get("occlusion") else {})}
            return {**base, "actionable": True, "error_code": "", "coord": [point["x"], point["y"]],
                    "actionability": {"point_source": point.get("point_source", "center"),
                                      "candidates_checked": point.get("candidates_checked", 1)}}
        except ModelRequestTimeout:
            raise
        except BrowserActionError as exc:
            self._check_deadline()
            return {**base, "error_code": "stale_snapshot", "error": str(exc)[:300]}
        except Exception as exc:
            self._check_deadline()
            return {**base, "error_code": "target_inspection_failed", "error": str(exc)[:300]}
        finally:
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass
            self._disconnect(runtime, browser)

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
        failure_info, recovery_info, scroll_info = {}, {}, {}
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
            inspect = self._ACTIONABILITY_INSPECT
            def call(obj, fn, args=None):
                self._check_deadline()
                r = cdp.send("Runtime.callFunctionOn", {"objectId": obj, "functionDeclaration": fn,
                    "arguments": [{"value": a} for a in (args or [])], "returnByValue": True})
                if r.get("exceptionDetails"):
                    raise BrowserActionError("DOM inspection failed")
                self._check_deadline()
                return r.get("result", {}).get("value")
            def stable_pair():
                first = call(obj, inspect)
                page.wait_for_timeout(min(35, self._timeout_ms(35)))
                self._check_deadline()
                return first, call(obj, inspect)

            first, point = stable_pair()
            first_occlusion = (first or {}).get("occlusion") or {}
            occlusion = (point or {}).get("occlusion") or {}
            if (action.action in ("click", "double_click", "type")
                    and (first or {}).get("error_code") == "target_occluded"
                    and (point or {}).get("error_code") == "target_occluded"
                    and first_occlusion.get("recovery_allowed") and occlusion.get("recovery_allowed")
                    and first_occlusion.get("autocomplete") == occlusion.get("autocomplete")):
                # One Escape is allowed only for the focused input's proven, associated
                # listbox. Never auto-dismiss a modal/cookie panel or send Enter.
                self._validate_snapshot(route, page, cdp)
                self._check_deadline()
                recovery_info = {"recovery": {"kind": "dismiss_autocomplete", "attempted": True,
                                             "succeeded": False},
                                 "auxiliary_action": {"action": "press", "key": "Escape", "dispatched": True}}
                page.keyboard.press("Escape")
                self._check_deadline()
                self._validate_snapshot(route, page, cdp)
                obj = cdp.send("DOM.resolveNode", {"backendNodeId": backend_id})["object"].get("objectId")
                self._check_deadline()
                if not obj:
                    raise BrowserActionError("Target detached after autocomplete recovery")
                cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
                page.wait_for_timeout(min(75, self._timeout_ms(75)))
                self._check_deadline()
                first, point = stable_pair()
            if not point or point.get("error"):
                failure_info = {"error_code": (point or {}).get("error_code") or
                                "target_" + str((point or {}).get("error", "not_actionable"))}
                if (point or {}).get("occlusion"):
                    failure_info["occlusion"] = point["occlusion"]
                raise BrowserActionError((point or {}).get("error", "Target not actionable"))
            if first != point:
                failure_info = {"error_code": "target_moved"}
                raise BrowserActionError("Target moved during actionability check")
            if recovery_info:
                recovery_info["recovery"]["succeeded"] = True
            self._validate_snapshot(route, page, cdp)
            name, args = action.action, action.args
            self._check_deadline()
            # 点击 PDF 链接 → 用当前 Chrome 网络通道下载并核实落盘
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
                    return {**self.download(full, page=page, browser=browser), **recovery_info, "target_ref": ref,
                            "actionability": {"point_source": point.get("point_source", "center"),
                                              "candidates_checked": point.get("candidates_checked", 1)}}
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
                delta = int(args["amount"]) * 120 * (-1 if args["direction"] == "up" else 1)
                chain = cdp.send("Runtime.callFunctionOn", {"objectId": obj,
                    "functionDeclaration": self._SCROLL_CHAIN, "returnByValue": False})
                if chain.get("exceptionDetails"):
                    raise BrowserActionError("Scroll container inspection failed")
                chain_id = chain.get("result", {}).get("objectId")
                if not chain_id:
                    raise BrowserActionError("Scroll container is unavailable")
                before = call(chain_id, self._SCROLL_CHAIN_METRICS)
                dispatched = True
                page.mouse.move(point["x"], point["y"])
                page.mouse.wheel(0, delta)
                # Wheel dispatch is asynchronous. Observe the same container chain,
                # including its page ancestor if scrolling chains at a boundary.
                after = before
                for _ in range(3):
                    page.wait_for_timeout(min(50, self._timeout_ms(50)))
                    after = call(chain_id, self._SCROLL_CHAIN_METRICS)
                    if any(abs(new["scrollTop"] - old["scrollTop"]) > 0.5
                           for old, new in zip(before, after)):
                        break
                scroll_info = self._scroll_progress(before, after, delta)
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
                    **recovery_info,
                    **scroll_info,
                    "actionability": {"point_source": point.get("point_source", "center"),
                                      "candidates_checked": point.get("candidates_checked", 1)},
                    "coordinate_space": "css_viewport", "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "error": str(exc)[:500],
                    "target_ref": ref, **failure_info, **recovery_info,
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

    def download(self, url, *, page=None, browser=None):
        """通过受管 Chrome 下载，复用浏览器代理、证书和会话。"""
        self._check_deadline()
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "action": "download", "error": f"无效下载地址: {url[:120]}"}
        # 幂等：本次会话已下载过这个 URL → 直接返回，不重复下载（防模型重复点击同一链接）
        if url in self._downloaded:
            path = self._downloaded[url]
            try:
                size = os.path.getsize(path) if os.path.isfile(path) else 0
                if (os.path.normcase(os.path.dirname(os.path.abspath(path)))
                        != os.path.normcase(os.path.abspath(self.downloads_dir))):
                    size = 0  # The same URL cached in another destination cannot satisfy this task.
                expected = self._downloaded_sizes.get(url)
                if expected is not None and size != expected:
                    size = 0
                from .browser_download import _pdf_url
                if size > 0 and (_pdf_url(url) or path.lower().endswith(".pdf")):
                    with open(path, "rb") as handle:
                        if not handle.read(1024).lstrip().startswith(b"%PDF-"):
                            size = 0
            except OSError:
                size = 0
            if size > 0:
                return {"status": "already_downloaded", "dispatched": False, "channel": "browser_dom",
                        "action": "download", "path": path, "bytes": size, "url": url,
                        "verified": True, "verification_source": "cached_file",
                        "message": f"已重新核实本地文件：{path}（{size} 字节），无需重复下载"}
            # 文件被删除、变成目录或空文件时，缓存不能阻止重新下载。
            self._downloaded.pop(url, None)
            self._downloaded_sizes.pop(url, None)
        from .browser_download import download_through_chromium
        if page is not None and browser is not None:
            return download_through_chromium(self, url, page, browser)
        p = attached = None
        try:
            p, attached = self._connect()
            page = self._pick_page(attached)
            if page is None:
                raise BrowserActionError("No focused or bound page for download")
            return download_through_chromium(self, url, page, attached)
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "rejected", "dispatched": False, "channel": "browser_dom",
                    "action": "download", "verified": False, "error": str(exc)[:300]}
        finally:
            if p is not None and attached is not None:
                self._disconnect(p, attached)

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

    # Metrics use CSS pixels, the same space as viewport/DOM actions. Keep the
    # actual nodes in a temporary CDP object so the before/after samples cannot
    # silently switch to a different ancestor after scrolling.
    _SCROLL_CHAIN = """function(){
        const root=document.scrollingElement || document.documentElement;
        const chain=[];
        for(let el=this;el;el=el.parentElement || (el.getRootNode && el.getRootNode().host)){
            if(el===root)break;
            if(el.nodeType!==1)continue;
            const style=getComputedStyle(el);
            if(/^(auto|scroll|overlay)$/.test(style.overflowY) && el.scrollHeight>el.clientHeight+0.5)
                chain.push(el);
        }
        if(root)chain.push(root);
        return chain;
    }"""
    _SCROLL_CHAIN_METRICS = """function(){
        return this.map(el=>{
            if(!el.isConnected)throw new Error('Scroll container detached');
            const root=document.scrollingElement || document.documentElement;
            const top=el.scrollTop,height=el.clientHeight,total=el.scrollHeight;
            const maximum=Math.max(0,total-height);
            return {scrollTop:top,clientHeight:height,scrollHeight:total,
                can_scroll_up:top>0.5,can_scroll_down:top<maximum-0.5,
                at_top:top<=0.5,at_bottom:top>=maximum-0.5,
                container:{kind:el===root?'page':'element',tag:el.tagName.toLowerCase(),id:el.id || ''}};
        });
    }"""
    _PAGE_SCROLL_METRICS = """() => {
        const el=document.scrollingElement || document.documentElement;
        const top=el ? el.scrollTop : scrollY;
        const height=el ? el.clientHeight : innerHeight;
        const total=el ? el.scrollHeight : innerHeight;
        const maximum=Math.max(0,total-height);
        return {scrollTop:top,clientHeight:height,scrollHeight:total,
            can_scroll_up:top>0.5,can_scroll_down:top<maximum-0.5,
            at_top:top<=0.5,at_bottom:top>=maximum-0.5,
            container:{kind:'page',tag:el ? el.tagName.toLowerCase() : '',id:el ? el.id || '' : ''}};
    }"""

    @staticmethod
    def _scroll_progress(before, after, requested_delta):
        if not before or len(before) != len(after):
            raise BrowserActionError("Scroll metrics are unavailable", dispatched=True)
        # Report the nearest ancestor that actually moved; an inner scroller can
        # hand the wheel to an outer one when its own boundary has been reached.
        index = next((i for i, (old, new) in enumerate(zip(before, after))
                      if abs(new["scrollTop"] - old["scrollTop"]) > 0.5), 0)
        old, new = before[index], after[index]
        actual_delta = new["scrollTop"] - old["scrollTop"]
        moved = abs(actual_delta) > 0.5
        return {"action": "scroll", "delta": requested_delta, "requested_delta": requested_delta,
                "actual_delta": actual_delta, "moved": moved, "noop": not moved,
                "scroll_container": new.get("container", {"kind": "page"}),
                "scroll_before": {k: v for k, v in old.items() if k != "container"},
                "scroll_after": {k: v for k, v in new.items() if k != "container"}}

    def _scroll(self, action):
        """页面级滚动，报告真实位移和滚动边界，而非仅报告输入已发送。"""
        self._check_deadline()
        amount = int(action.args.get("amount", 1))
        delta = amount * 300 * (-1 if action.args.get("direction", "up") == "up" else 1)
        started, dispatched = time.perf_counter(), False
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
            before = page.evaluate(self._PAGE_SCROLL_METRICS)
            self._check_deadline()
            dispatched = True
            # Instant behavior avoids mistaking an animation's first frame for
            # a no-op, even when the page applies CSS scroll-behavior:smooth.
            page.evaluate("delta => window.scrollBy({top:delta,left:0,behavior:'instant'})", delta)
            self._check_deadline()
            after = page.evaluate(self._PAGE_SCROLL_METRICS)
            self._check_deadline()
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    **self._scroll_progress([before], [after], delta),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except ModelRequestTimeout:
            raise
        except Exception as exc:
            self._check_deadline()
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "action": "scroll",
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
                        targets = cdp.send("Target.getTargets")["targetInfos"]
                        children = {target.get("targetId"): target for target in targets
                                    if target.get("type") == "page"
                                    and target.get("browserContextId") == self.context_id}
                        if self._adopted_launcher_context or not set(children) <= self._owned_page_ids:
                            # 即便显式创建的 context 也可能出现用户后来打开的 tabs。
                            # 只关已登记的任务页，保留含外部页面的整个 context。
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
