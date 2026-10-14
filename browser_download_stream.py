"""Cancelable CDP response streaming for native incognito Chrome contexts."""
from __future__ import annotations

import asyncio
import base64
from email.message import Message
import os
import queue
import tempfile
import threading
import time

from .model.request_deadline import ModelRequestTimeout


async def _stream_response(settings, url, target):
    from playwright.async_api import async_playwright
    from .browser_download import DownloadConfigurationError, _filename, _pdf_url
    from .browser_session import BrowserActionError

    playwright = browser = cdp = navigation = None
    staging = staged_file = None
    frame_id = None
    state = {"stream": None, "request": None, "error": None, "dispatched": False,
             "url": None, "filename": None, "pdf": _pdf_url(url), "expected": None}
    phase = "configuration"

    def remaining():
        value = settings["deadline"] - time.monotonic()
        if value <= 0:
            if (settings["request_deadline"] is not None
                    and time.monotonic() >= settings["request_deadline"]):
                raise ModelRequestTimeout("浏览器下载已超过本步共享预算")
            raise BrowserActionError("Chrome 下载超过 90 秒等待预算", dispatched=state["dispatched"])
        return value

    async def bounded(awaitable):
        try:
            timeout = remaining()
        except BaseException:
            awaitable.close()
            raise
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except asyncio.TimeoutError:
            remaining()
            raise

    async def paused(event):
        request_id = event["requestId"]
        try:
            if event.get("frameId") != frame_id:
                await bounded(cdp.send("Fetch.continueRequest", {"requestId": request_id}))
                return
            headers = {item["name"].lower(): item["value"] for item in event.get("responseHeaders", [])}
            status = event.get("responseStatusCode", 0)
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                await bounded(cdp.send("Fetch.continueRequest", {"requestId": request_id}))
                return
            state["request"] = request_id
            if event.get("responseErrorReason"):
                raise BrowserActionError("Chrome 网络错误：" + event["responseErrorReason"])
            if status != 200:
                raise BrowserActionError(f"HTTP {status}")
            content_type = headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/xhtml+xml" in content_type:
                raise BrowserActionError("下载地址返回 HTML 页面，未取得目标文件")
            state["url"] = event.get("request", {}).get("url") or url
            state["pdf"] = state["pdf"] or _pdf_url(state["url"]) or "pdf" in content_type
            disposition = Message()
            disposition["content-disposition"] = headers.get("content-disposition", "")
            state["filename"] = disposition.get_filename()
            if (headers.get("content-length", "").isdigit()
                    and headers.get("content-encoding", "identity").lower() == "identity"):
                state["expected"] = int(headers["content-length"])
            response = await bounded(cdp.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id}))
            state["stream"] = response["stream"]
        except BaseException as error:
            state["error"] = error

    try:
        playwright = await bounded(async_playwright().start())
        browser = await bounded(playwright.chromium.connect_over_cdp(
            settings["endpoint"], timeout=max(1, int(remaining() * 1000))))
        browser_cdp = await bounded(browser.new_browser_cdp_session())
        metadata = await bounded(browser_cdp.send("Target.getBrowserContexts"))
        targets = (await bounded(browser_cdp.send("Target.getTargets")))["targetInfos"]
        pages = [item for item in targets if item.get("type") == "page"]
        default_id = metadata.get("defaultBrowserContextId")
        if not default_id or not pages or any(not item.get("browserContextId")
                or item["browserContextId"] == default_id for item in pages):
            raise DownloadConfigurationError("下载连接无法确认是所选无痕浏览器", "download_context_unavailable")
        selected = next((item for item in pages if item.get("targetId") == target["targetId"]), None)
        if selected is None or selected.get("browserContextId") != target.get("browserContextId"):
            raise DownloadConfigurationError("下载页面不属于当前任务的绑定会话", "download_context_unavailable")
        page = None
        for context in browser.contexts:
            for candidate in context.pages:
                candidate_cdp = await bounded(context.new_cdp_session(candidate))
                info = (await bounded(candidate_cdp.send("Target.getTargetInfo")))["targetInfo"]
                if info.get("targetId") == target["targetId"]:
                    page, cdp = candidate, candidate_cdp
                    break
                await bounded(candidate_cdp.detach())
            if page is not None:
                break
        if page is None:
            raise DownloadConfigurationError("无法附加下载任务页面", "download_context_unavailable")
        # Frame identity can differ from the page target ID after renderer changes.
        frame_id = (await bounded(cdp.send("Page.getFrameTree")))["frameTree"]["frame"]["id"]
        root = settings["downloads_dir"]
        os.makedirs(root, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".browser-download-", dir=root)
        staged_file = os.path.join(staging, "response")
        cdp.on("Fetch.requestPaused", paused)
        await bounded(cdp.send("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Response"}]}))
        phase = "transfer"
        state["dispatched"] = True
        navigation = asyncio.create_task(page.goto(url, wait_until="commit",
            timeout=max(1, int(remaining() * 1000))))
        while state["stream"] is None:
            remaining()
            if state["error"] is not None:
                raise state["error"]
            if navigation.done():
                await navigation
                raise BrowserActionError("下载未取得响应流", dispatched=True)
            await asyncio.sleep(min(0.02, remaining()))
        size = 0
        with open(staged_file, "wb") as output:
            while True:
                chunk = await bounded(cdp.send("IO.read", {"handle": state["stream"], "size": 65536}))
                data = chunk.get("data", "")
                payload = base64.b64decode(data) if chunk.get("base64Encoded") else data.encode("utf-8")
                output.write(payload)
                size += len(payload)
                if chunk.get("eof"):
                    break
                remaining()
        if size <= 0 or (state["expected"] is not None and size != state["expected"]):
            raise BrowserActionError("Chrome 响应流的本地文件大小未通过核实", dispatched=True)
        pdf = state["pdf"] or str(state["filename"] or "").lower().endswith(".pdf")
        if pdf:
            with open(staged_file, "rb") as handle:
                if not handle.read(1024).lstrip().startswith(b"%PDF-"):
                    raise BrowserActionError("下载内容不是有效的 PDF 文件头", dispatched=True)
        remaining()
        filename = _filename(state["filename"], state["url"], pdf)
        base, extension = os.path.splitext(filename)
        index = 0
        while True:
            path = os.path.join(root, filename if index == 0 else f"{base}_{index}{extension}")
            if not os.path.exists(path):
                try:
                    os.rename(staged_file, path)
                    break
                except FileExistsError:
                    pass
            index += 1
            remaining()
        return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                "action": "download", "path": path, "bytes": size, "verified": True,
                "url": url, "resolved_url": state["url"], "download_transport": "chromium",
                "download_method": "cdp_response_stream", "verification_source": "browser_download",
                "message": f"✅ 已成功下载到 {path}（{size} 字节），已核实此文件落盘"}
    except ModelRequestTimeout:
        raise
    except Exception as error:
        details = {}
        if isinstance(error, DownloadConfigurationError) or phase == "configuration":
            details = {"error_code": getattr(error, "code", "download_configuration_failed"),
                       "retryable": False, "error_phase": "download_configuration"}
        return {"status": "rejected", "dispatched": state["dispatched"], "channel": "browser_dom",
                "action": "download", "verified": False, "download_transport": "chromium",
                "error": str(error)[:300], **details}
    finally:
        cleanup_deadline = time.monotonic() + 10

        async def cleanup(awaitable):
            timeout = min(2, cleanup_deadline - time.monotonic())
            if timeout <= 0:
                if asyncio.isfuture(awaitable):
                    awaitable.cancel()
                else:
                    awaitable.close()
                return
            try:
                await asyncio.wait_for(awaitable, timeout)
            except BaseException:
                pass

        if cdp is not None:
            cancellation = []
            if state["stream"] is not None:
                cancellation.append(cleanup(cdp.send("IO.close", {"handle": state["stream"]})))
            if state["request"] is not None:
                cancellation.append(cleanup(cdp.send("Fetch.failRequest", {"requestId": state["request"], "errorReason": "Aborted"})))
            if cancellation:
                await asyncio.gather(*cancellation)
            if state["dispatched"]:
                await cleanup(cdp.send("Page.stopLoading"))
            await cleanup(cdp.send("Fetch.disable"))
            await cleanup(cdp.detach())
        if navigation is not None:
            if not navigation.done():
                navigation.cancel()
            await cleanup(navigation)
        if browser is not None:
            await cleanup(browser.close())
        if playwright is not None:
            await cleanup(playwright.stop())
        if staging is not None:
            if staged_file is not None:
                try:
                    os.remove(staged_file)
                except OSError:
                    pass
            try:
                os.rmdir(staging)
            except OSError:
                pass


def download_native_incognito(session, url, page):
    """Use the same browser/page with async IO so a stalled body honors the budget."""
    from .browser_download import DownloadConfigurationError

    started = time.perf_counter()
    session._check_deadline()
    cdp = page.context.new_cdp_session(page)
    try:
        target = cdp.send("Target.getTargetInfo")["targetInfo"]
    finally:
        cdp.detach()
    if session.options.bind_page and (target.get("targetId") != session.page_id
            or target.get("browserContextId") != session.context_id):
        raise DownloadConfigurationError("下载页面不属于当前任务的绑定会话", "download_context_unavailable")
    settings = {"endpoint": session.options.endpoint, "downloads_dir": os.path.abspath(session.downloads_dir),
                "request_deadline": session.request_deadline,
                "deadline": time.monotonic() + session._timeout_seconds(90)}
    result_queue = queue.Queue(maxsize=1)
    active = {}

    def transfer():
        async def run():
            active["loop"] = asyncio.get_running_loop()
            active["task"] = asyncio.current_task()
            return await _stream_response(settings, url, target)
        try:
            result_queue.put((True, asyncio.run(run())))
        except BaseException as error:
            result_queue.put((False, error))

    worker = threading.Thread(target=transfer, name="incognito-cdp-download", daemon=True)
    worker.start()

    def cancel_worker():
        if active.get("loop") is not None and active.get("task") is not None:
            try:
                active["loop"].call_soon_threadsafe(active["task"].cancel)
            except RuntimeError:
                pass

    # 网络命令和清理都有各自的 asyncio 超时；外层也限制异常驱动退出。
    try:
        worker.join(timeout=max(0.0, settings["deadline"] - time.monotonic()) + 12)
    except BaseException:
        cancel_worker()
        worker.join(timeout=12)
        raise
    if worker.is_alive():
        cancel_worker()
        worker.join(timeout=2)
        raise ModelRequestTimeout("浏览器下载清理未能在截止时间内结束")
    succeeded, result = result_queue.get_nowait()
    if not succeeded:
        raise result
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    if result.get("verified"):
        session._downloaded[url] = result["path"]
        session._downloaded_sizes[url] = result["bytes"]
    else:
        # Playwright 自带 timeout 可能比 asyncio 截止时间早不到一毫秒触发；
        # 清理完成后按同一共享预算归因，避免把预算耗尽当作配置失败。
        session._check_deadline()
    return result
