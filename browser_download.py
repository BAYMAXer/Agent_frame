"""Download through the attached Chromium network stack, including its proxy."""
from __future__ import annotations

import os
import re
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .model.request_deadline import ModelRequestTimeout


class DownloadConfigurationError(RuntimeError):
    """A download cannot start until the browser/session configuration changes."""

    def __init__(self, message, code="download_configuration_failed"):
        super().__init__(message)
        self.code = code


def _pdf_url(url):
    path = urlsplit(url).path.lower()
    return path.endswith((".pdf", "/pdf")) or "/pdf/" in path


def _filename(suggested, url, pdf):
    # Chromium normally sanitizes suggestedFilename; also guard Windows paths
    # because protocol/file names must never escape the configured directory.
    name = str(suggested or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "download"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name):
        name = "_" + name
    if pdf and not name.lower().endswith(".pdf"):
        name += ".pdf"
    base, ext = os.path.splitext(name)
    return base[:180] + ext[:20]


def download_through_chromium(session, url, page, browser):
    """Force a successful document response into a native Chrome download.

    Fetch changes only response headers; the body stays in Chrome. Browser
    events identify the exact frame and download GUID. Neither requests nor
    Playwright's APIRequestContext is used for the external transfer.
    """
    from .browser_session import BrowserActionError

    started = time.perf_counter()
    deadline = time.monotonic() + session._timeout_seconds(90)
    page_cdp = browser_cdp = None
    staging = None
    configured = False
    phase = "configuration"
    frame_id = None
    scope = {}
    state = {"guid": None, "progress": None, "response_url": None, "error": None,
             "suggested": None, "pdf": _pdf_url(url), "dispatched": False}
    accepted_urls = set()

    def check():
        session._check_deadline()
        if time.monotonic() >= deadline:
            raise BrowserActionError("Chrome 下载超过 90 秒等待预算", dispatched=state["dispatched"])

    def paused(event):
        request_id = event["requestId"]
        try:
            check()
            if event.get("frameId") != frame_id:
                page_cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            response_url = event.get("request", {}).get("url", "")
            status = event.get("responseStatusCode", 0)
            headers = event.get("responseHeaders", [])
            lookup = {header["name"].lower(): header["value"] for header in headers}
            if status in (301, 302, 303, 307, 308) and lookup.get("location"):
                page_cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
            if event.get("responseErrorReason"):
                raise BrowserActionError("Chrome 网络错误：" + event["responseErrorReason"])
            if status != 200:
                raise BrowserActionError(f"HTTP {status}")
            content_type = lookup.get("content-type", "").lower()
            if "text/html" in content_type or "application/xhtml+xml" in content_type:
                raise BrowserActionError("下载地址返回 HTML 页面，未取得目标文件")
            state["pdf"] = state["pdf"] or _pdf_url(response_url) or "pdf" in content_type
            # Keep the server's filename and replace only inline/attachment mode.
            disposition = lookup.get("content-disposition", "")
            parameters = disposition.partition(";")[2]
            replacement = "attachment" + (";" + parameters if parameters else "")
            headers = [header for header in headers if header["name"].lower() != "content-disposition"]
            headers.append({"name": "Content-Disposition", "value": replacement})
            accepted_urls.add(response_url)
            state["response_url"] = response_url
            # At the response stage, an omitted body preserves the original body.
            page_cdp.send("Fetch.fulfillRequest", {"requestId": request_id,
                          "responseCode": status, "responseHeaders": headers})
        except Exception as exc:
            state["error"] = exc
            try:
                page_cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
            except Exception:
                pass

    def will_begin(event):
        if (event.get("frameId") == frame_id and event.get("url") in accepted_urls
                and state["guid"] is None):
            guid = event.get("guid", "")
            if not re.fullmatch(r"[\w-]+", guid):
                state["error"] = BrowserActionError("Chrome 返回无效下载标识")
                return
            state["guid"] = guid
            state["suggested"] = event.get("suggestedFilename")

    def progress(event):
        if state["guid"] and event.get("guid") == state["guid"]:
            state["progress"] = event

    try:
        check()
        page_cdp = page.context.new_cdp_session(page)
        target = page_cdp.send("Target.getTargetInfo")["targetInfo"]
        if session.options.bind_page and (target.get("targetId") != session.page_id
                or target.get("browserContextId") != session.context_id):
            raise DownloadConfigurationError("下载页面不属于当前任务的绑定会话", "download_context_unavailable")
        if target.get("browserContextId"):
            scope = {"browserContextId": target["browserContextId"]}
        frame_id = page_cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        check()
        browser_cdp = browser.new_browser_cdp_session()
        if session.options.bind_page:
            contexts = browser_cdp.send("Target.getBrowserContexts").get("browserContextIds", [])
            if not scope or scope["browserContextId"] not in contexts:
                raise DownloadConfigurationError(
                    "下载上下文未注册到 CDP，需重新初始化任务浏览器", "download_context_unavailable")
        check()
        root = os.path.abspath(session.downloads_dir)
        os.makedirs(root, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".browser-download-", dir=root)
        browser_cdp.on("Browser.downloadWillBegin", will_begin)
        browser_cdp.on("Browser.downloadProgress", progress)
        page_cdp.on("Fetch.requestPaused", paused)
        browser_cdp.send("Browser.setDownloadBehavior", {"behavior": "allowAndName",
                         "downloadPath": staging, "eventsEnabled": True, **scope})
        configured = True
        page_cdp.send("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Response"}]})
        check()
        phase = "transfer"
        state["dispatched"] = True
        try:
            # Navigation timeout also bounds a server that never sends headers.
            page.goto(url, wait_until="commit", timeout=max(1, int((deadline - time.monotonic()) * 1000)))
        except Exception as exc:
            check()
            if state["error"]:
                raise state["error"]
            # Chrome aborts the page navigation when it hands off to a download.
            if "ERR_ABORTED" not in str(exc) and "Download is starting" not in str(exc):
                raise
        while True:
            check()
            if state["error"]:
                raise state["error"]
            update = state["progress"] or {}
            if update.get("state") == "canceled":
                raise BrowserActionError("Chrome 下载已取消或被浏览器阻止", dispatched=True)
            if update.get("state") == "completed":
                break
            page.wait_for_timeout(max(1, min(50, int((deadline - time.monotonic()) * 1000))))
        # filePath in the protocol is optional and may not exist. Only inspect
        # this operation's allowAndName GUID within its private staging folder.
        staged_file = os.path.join(staging, state["guid"])
        size = os.path.getsize(staged_file) if os.path.isfile(staged_file) else 0
        if size <= 0 or size != update.get("receivedBytes"):
            raise BrowserActionError("Chrome 报告下载完成，但本地文件大小未通过核实", dispatched=True)
        pdf = state["pdf"] or str(state["suggested"] or "").lower().endswith(".pdf")
        if pdf:
            with open(staged_file, "rb") as handle:
                if not handle.read(1024).lstrip().startswith(b"%PDF-"):
                    raise BrowserActionError("下载内容不是有效的 PDF 文件头", dispatched=True)
        check()
        filename = _filename(state["suggested"], state["response_url"] or url, pdf)
        base, ext = os.path.splitext(filename)
        index = 0
        while True:
            path = os.path.join(root, filename if index == 0 else f"{base}_{index}{ext}")
            if not os.path.exists(path):
                try:
                    # Windows rename refuses to replace a concurrently created file.
                    os.rename(staged_file, path)
                    break
                except FileExistsError:
                    pass
            index += 1
            check()
        session._downloaded[url] = path
        session._downloaded_sizes[url] = size
        return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                "action": "download", "path": path, "bytes": size, "verified": True,
                "url": url, "resolved_url": state["response_url"],
                "download_transport": "chromium", "verification_source": "browser_download",
                "message": f"✅ 已成功下载到 {path}（{size} 字节），已核实此文件落盘",
                "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    except ModelRequestTimeout:
        raise
    except Exception as exc:
        session._check_deadline()
        details = {}
        if isinstance(exc, DownloadConfigurationError) or phase == "configuration":
            details = {"error_code": getattr(exc, "code", "download_configuration_failed"),
                       "retryable": False, "error_phase": "download_configuration"}
        return {"status": "rejected", "dispatched": state["dispatched"], "channel": "browser_dom",
                "action": "download", "verified": False, "download_transport": "chromium",
                "error": str(exc)[:300], **details,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    finally:
        if browser_cdp is not None:
            if state["guid"] and (state["progress"] or {}).get("state") != "completed":
                try:
                    browser_cdp.send("Browser.cancelDownload", {"guid": state["guid"], **scope})
                except Exception:
                    pass
            for event, handler in (("Browser.downloadWillBegin", will_begin), ("Browser.downloadProgress", progress)):
                try:
                    browser_cdp.remove_listener(event, handler)
                except Exception:
                    pass
            if configured:
                try:
                    browser_cdp.send("Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": False, **scope})
                except Exception:
                    pass
            try:
                browser_cdp.detach()
            except Exception:
                pass
        if page_cdp is not None:
            try:
                page_cdp.remove_listener("Fetch.requestPaused", paused)
            except Exception:
                pass
            try:
                # Stop pending network work before releasing the interception.
                if state["dispatched"] and not state["guid"]:
                    page_cdp.send("Page.stopLoading")
            except Exception:
                pass
            try:
                page_cdp.send("Fetch.disable")
            except Exception:
                pass
            try:
                page_cdp.detach()
            except Exception:
                pass
        if staging is not None:
            # The setting is context-scoped; another tab could download during
            # this short window. Remove only our GUID, preserving foreign files.
            resolved = os.path.abspath(staging)
            if os.path.dirname(resolved) == os.path.abspath(session.downloads_dir) and os.path.basename(resolved).startswith(".browser-download-"):
                if state["guid"]:
                    for name in (state["guid"], state["guid"] + ".crdownload"):
                        try:
                            os.remove(os.path.join(resolved, name))
                        except OSError:
                            pass
                try:
                    os.rmdir(resolved)
                except OSError:
                    pass
