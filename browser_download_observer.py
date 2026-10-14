"""Verify downloads initiated by ordinary browser clicks and keyboard input.

A new, page-correlated Chromium event is required. The observer never scans a
user's existing Downloads folder or treats a model's completion claim as proof.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile

from .browser_download import DownloadConfigurationError, _filename, _pdf_url
from .browser_session import BrowserActionError
from .model.request_deadline import ModelRequestTimeout


class BrowserDownloadObserver:
    """Start before dispatch, finish after dispatch, and always close in finally."""

    start_timeout = 2.0
    completion_grace_seconds = 0.5
    stable_seconds = 0.2

    def __init__(self, session, page, browser):
        self.session, self.page, self.browser = session, page, browser
        self.page_cdp = self.browser_cdp = self.event_cdp = None
        self.staging = None
        self.root = os.path.abspath(session.downloads_dir)
        self.native = bool(session._adopted_launcher_context or session.options.browser_mode is not None)
        self.domain = "Page" if self.native else "Browser"
        self.scope = {}
        self.frames = set()
        self.event = None
        self.progress = None
        self.completed_at = None
        self.error = None
        self.configured = False
        self.persisted = False
        self.closed = False
        self.started = time.perf_counter()
        self.deadline = None
        self._stable_file = None
        self._before_event = {}
        self._foreign_names = set()
        self._pending_requests = set()
        self._request_finished_at = None
        self._watch_requests = False
        self._network_enabled = False

    def _check(self):
        self.session._check_deadline()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise BrowserActionError("Chrome 下载超过 90 秒等待预算", dispatched=bool(self.event))

    def _will_begin(self, event):
        if self.event is not None or event.get("frameId") not in self.frames:
            # The browser setting is context-scoped. If a foreign download
            # shares a suggested filename, a filename alone cannot identify
            # our artifact; a GUID-correlated filePath can still establish it.
            guid = str(event.get("guid", ""))
            if self.event is None or guid != self.event["guid"]:
                if re.fullmatch(r"[\w-]+", guid):
                    self._foreign_names.add(guid)
                if event.get("suggestedFilename"):
                    self._foreign_names.add(_filename(event["suggestedFilename"], event.get("url", ""), False))
            return
        if not re.fullmatch(r"[\w-]+", str(event.get("guid", ""))):
            self.error = BrowserActionError("Chrome 返回无效下载标识", dispatched=True)
            return
        self.event = dict(event)
        self.event["guid"] = str(self.event["guid"])
        # Native contexts may choose 'name (1).ext' when another tab wrote the
        # suggested name. Never mistake that earlier file for the new event.
        if self.staging is not None:
            try:
                for entry in os.scandir(self.staging):
                    if entry.is_file():
                        stat = entry.stat()
                        self._before_event[os.path.normcase(entry.path)] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                pass

    def _progress(self, event):
        if self.event is not None and event.get("guid") == self.event["guid"]:
            self.progress = dict(event)
            if event.get("state") == "completed" and self.completed_at is None:
                self.completed_at = time.monotonic()

    def _frame_attached(self, event):
        if event.get("parentFrameId") in self.frames:
            self.frames.add(event.get("frameId"))

    def _request_started(self, event):
        if (self._watch_requests and event.get("frameId") in self.frames
                and event.get("type") in {"Document", "XHR", "Fetch"}
                and event.get("requestId")):
            self._pending_requests.add(event["requestId"])

    def _request_finished(self, event):
        request_id = event.get("requestId")
        if request_id in self._pending_requests:
            self._pending_requests.remove(request_id)
            self._request_finished_at = time.monotonic()

    def start(self):
        """Prepare a private destination before input can initiate a download."""
        try:
            self._check()
            self.deadline = time.monotonic() + self.session._timeout_seconds(90)
            self.page_cdp = self.page.context.new_cdp_session(self.page)
            target = self.page_cdp.send("Target.getTargetInfo")["targetInfo"]
            if self.session.options.bind_page and (
                    target.get("targetId") != self.session.page_id
                    or target.get("browserContextId") != self.session.context_id):
                raise DownloadConfigurationError("下载页面不属于当前任务的绑定会话", "download_context_unavailable")
            if target.get("browserContextId"):
                self.scope = {"browserContextId": target["browserContextId"]}
            self.page_cdp.on("Page.frameAttached", self._frame_attached)
            self.page_cdp.send("Page.enable")

            def collect(tree):
                self.frames.add(tree["frame"]["id"])
                for child in tree.get("childFrames", []):
                    collect(child)

            collect(self.page_cdp.send("Page.getFrameTree")["frameTree"])
            self._check()
            self.browser_cdp = self.browser.new_browser_cdp_session()
            if self.session.options.bind_page and not self.native:
                contexts = self.browser_cdp.send("Target.getBrowserContexts").get("browserContextIds", [])
                if not self.scope or self.scope["browserContextId"] not in contexts:
                    raise DownloadConfigurationError("下载上下文未注册到 CDP", "download_context_unavailable")
            os.makedirs(self.root, exist_ok=True)
            self.staging = tempfile.mkdtemp(prefix=".browser-download-", dir=self.root)
            self.event_cdp = self.page_cdp if self.native else self.browser_cdp
            self.event_cdp.on(self.domain + ".downloadWillBegin", self._will_begin)
            self.event_cdp.on(self.domain + ".downloadProgress", self._progress)
            if self.native:
                # Page.setDownloadBehavior supports allow, not allowAndName.
                # Native contexts save under Chromium's suggested filename.
                self.page_cdp.send("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": self.staging})
            else:
                self.browser_cdp.send("Browser.setDownloadBehavior", {
                    "behavior": "allowAndName", "downloadPath": self.staging, "eventsEnabled": True, **self.scope})
            self.configured = True
            self.page_cdp.on("Network.requestWillBeSent", self._request_started)
            self.page_cdp.on("Network.loadingFinished", self._request_finished)
            self.page_cdp.on("Network.loadingFailed", self._request_finished)
            self.page_cdp.send("Network.enable")
            self._network_enabled = True
            self._check()
            self._watch_requests = True
            return self
        except ModelRequestTimeout:
            self.close()
            raise
        except Exception as error:
            self.close()
            if isinstance(error, DownloadConfigurationError):
                raise
            raise DownloadConfigurationError("无法配置点击下载验证：" + str(error)[:240]) from error

    def _candidate_paths(self):
        if self.event is None or self.staging is None:
            return []
        names = [self.event["guid"]]
        suggested = self.event.get("suggestedFilename")
        if suggested:
            names.append(_filename(suggested, self.event.get("url", ""), False))
        paths = [os.path.join(self.staging, name) for name in dict.fromkeys(names) if name not in self._foreign_names]
        file_path = (self.progress or {}).get("filePath")
        if file_path:
            candidate = os.path.abspath(file_path)
            if os.path.normcase(os.path.dirname(candidate)) == os.path.normcase(os.path.abspath(self.staging)):
                paths.insert(0, candidate)
        return list(dict.fromkeys(paths))

    def _exact_file_path(self):
        value = (self.progress or {}).get("filePath")
        if not isinstance(value, str):
            return None
        path = os.path.abspath(value)
        if os.path.normcase(os.path.dirname(path)) != os.path.normcase(os.path.abspath(self.staging)):
            return None
        return os.path.normcase(path)

    @staticmethod
    def _count(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None

    def _completed_file(self, terminal):
        update = self.progress or {}
        received = self._count(update.get("receivedBytes"))
        total = self._count(update.get("totalBytes"))
        for path in self._candidate_paths():
            if (os.path.normcase(os.path.abspath(path)) in self._before_event
                    and os.path.normcase(os.path.abspath(path)) != self._exact_file_path()):
                continue
            if not os.path.isfile(path) or any(os.path.exists(path + suffix) for suffix in (".crdownload", ".part", ".tmp")):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_size <= 0:
                continue
            if received is not None and stat.st_size != received:
                continue
            if total is not None and stat.st_size != total:
                continue
            if terminal:
                return path, stat.st_size
            # A missed terminal event is recoverable only for the exact new
            # download, after its final file has stabilized and bytes agree.
            if received is None:
                continue
            fingerprint = (path, stat.st_size, stat.st_mtime_ns)
            now = time.monotonic()
            if self._stable_file is None or self._stable_file[0] != fingerprint:
                self._stable_file = (fingerprint, now)
            elif now - self._stable_file[1] >= self.stable_seconds:
                return path, stat.st_size
        return None

    def _verify_format(self, path):
        name = str(self.event.get("suggestedFilename") or path).lower()
        allowed = getattr(self.session, "download_extensions", None)
        extension = os.path.splitext(name)[1]
        if allowed and extension not in allowed:
            raise BrowserActionError("下载文件格式与用户要求不一致：" + (extension or "未提供扩展名"), dispatched=True)
        pdf = name.endswith(".pdf") or _pdf_url(self.event.get("url", ""))
        with open(path, "rb") as source:
            header = source.read(1024)
        if pdf and not header.lstrip().startswith(b"%PDF-"):
            raise BrowserActionError("下载内容不是有效的 PDF 文件头", dispatched=True)
        if name.endswith((".pptx", ".pptm")):
            try:
                with zipfile.ZipFile(path) as archive:
                    if not {"[Content_Types].xml", "ppt/presentation.xml"}.issubset(archive.namelist()):
                        raise ValueError("缺少演示文稿结构")
                    if archive.testzip() is not None:
                        raise ValueError("压缩内容校验失败")
            except (OSError, ValueError, zipfile.BadZipFile) as error:
                raise BrowserActionError("下载内容不是完整的 PowerPoint 文件", dispatched=True) from error
        elif name.endswith(".ppt") and not header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise BrowserActionError("下载内容不是有效的 PPT 文件头", dispatched=True)
        return pdf

    def _persist(self, path, size, *, event_completed):
        pdf = self._verify_format(path)
        self._check()
        url = self.event.get("url", "")
        filename = _filename(self.event.get("suggestedFilename"), url, pdf)
        base, extension = os.path.splitext(filename)
        index = 0
        while True:
            destination = os.path.join(self.root, filename if index == 0 else f"{base}_{index}{extension}")
            if not os.path.exists(destination):
                try:
                    os.rename(path, destination)
                    break
                except FileExistsError:
                    pass
            index += 1
            self._check()
        self.persisted = True
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            self.session._downloaded[url] = destination
            self.session._downloaded_sizes[url] = size
        return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                "action": "download", "path": destination, "bytes": size, "verified": True,
                "url": url, "resolved_url": url, "download_transport": "chromium",
                "download_method": "native_click", "verification_source": "browser_download",
                "download_event_completed": event_completed,
                "message": f"✅ 已成功下载到 {destination}（{size} 字节），已核实此文件落盘",
                "latency_ms": round((time.perf_counter() - self.started) * 1000, 2)}

    def finish(self):
        """Return verified download evidence, or {} when input did not download."""
        onset = time.monotonic() + self.start_timeout
        try:
            while True:
                self._check()
                if self.error is not None:
                    raise self.error
                if self.event is not None:
                    state = (self.progress or {}).get("state")
                    if state == "canceled":
                        raise BrowserActionError("Chrome 下载已取消或被浏览器阻止", dispatched=True)
                    found = self._completed_file(state == "completed")
                    if found is not None:
                        return self._persist(*found, event_completed=state == "completed")
                    if state == "completed" and time.monotonic() - self.completed_at >= self.completion_grace_seconds:
                        raise BrowserActionError("Chrome 报告下载完成，但本地文件大小未通过核实", dispatched=True)
                elif time.monotonic() >= onset:
                    # An unlabeled export button can wait on server-side work
                    # before returning an attachment or creating a blob URL.
                    # Keep the private destination configured until the action's
                    # document/fetch requests end, within the same step budget.
                    callback_grace = (self._request_finished_at is not None and
                        time.monotonic() - self._request_finished_at < self.completion_grace_seconds)
                    if not self._pending_requests and not callback_grace:
                        return {}
                self.page.wait_for_timeout(max(1, min(50, int((self.deadline - time.monotonic()) * 1000))))
        except ModelRequestTimeout:
            raise
        except Exception as error:
            return {"status": "rejected", "dispatched": True, "channel": "browser_dom",
                    "action": "download", "verified": False, "download_transport": "chromium",
                    "download_method": "native_click", "error": str(error)[:300],
                    "latency_ms": round((time.perf_counter() - self.started) * 1000, 2)}

    def close(self):
        """Restore download behavior and remove only this operation's artifacts."""
        if self.closed:
            return
        self.closed = True
        self._watch_requests = False
        if self.event is not None and not self.persisted and (self.progress or {}).get("state") != "completed":
            try:
                self.browser_cdp.send("Browser.cancelDownload", {"guid": self.event["guid"], **self.scope})
            except Exception:
                pass
            try:
                self.page_cdp.send("Page.stopLoading")
            except Exception:
                pass
        if self.event_cdp is not None:
            for event, handler in ((self.domain + ".downloadWillBegin", self._will_begin),
                                   (self.domain + ".downloadProgress", self._progress)):
                try:
                    self.event_cdp.remove_listener(event, handler)
                except Exception:
                    pass
        if self.page_cdp is not None:
            try:
                self.page_cdp.remove_listener("Page.frameAttached", self._frame_attached)
            except Exception:
                pass
            for event, handler in (("Network.requestWillBeSent", self._request_started),
                                   ("Network.loadingFinished", self._request_finished),
                                   ("Network.loadingFailed", self._request_finished)):
                try:
                    self.page_cdp.remove_listener(event, handler)
                except Exception:
                    pass
            if self._network_enabled:
                try:
                    self.page_cdp.send("Network.disable")
                except Exception:
                    pass
        if self.configured:
            try:
                if self.native:
                    self.page_cdp.send("Page.setDownloadBehavior", {"behavior": "default"})
                else:
                    self.browser_cdp.send("Browser.setDownloadBehavior", {
                        "behavior": "default", "eventsEnabled": False, **self.scope})
            except Exception:
                pass
        for cdp in (self.browser_cdp, self.page_cdp):
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass
        if self.staging is not None:
            try:
                candidates = self._candidate_paths()
            except Exception:
                candidates = []
            for path in candidates:
                # Completed files survive failed verification/late observations;
                # only abandoned owned temporary files are safe to remove here.
                # Native incognito has no reliable cancel API once DownloadManager
                # owns the transfer. Preserve active files until cancellation is
                # proven by a terminal event, even if cancel/stopLoading returned.
                if self.native and not self.persisted and (self.progress or {}).get("state") not in {"completed", "canceled"}:
                    continue
                if (os.path.normcase(os.path.abspath(path)) in self._before_event
                        and os.path.normcase(os.path.abspath(path)) != self._exact_file_path()):
                    continue
                for suffix in (".crdownload", ".part", ".tmp"):
                    try:
                        os.remove(path + suffix)
                    except OSError:
                        pass
            try:
                os.rmdir(self.staging)
            except OSError:
                pass
