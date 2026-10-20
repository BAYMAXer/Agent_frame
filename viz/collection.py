"""Human demonstrations through an observed browser viewport, without a model.

Only explicit, serialized commands change the task browser. The displayed
observation is the policy input; refreshing or resuming invalidates old clicks.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..actions import Action
from ..browser_session import BrowserOptions, BrowserSession
from ..env import Observation
from ..model.decision_model import encode_image


class CollectionError(ValueError):
    pass


def http_url(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise CollectionError("请输入完整的 http:// 或 https:// 网址。")
    return value


class OwnedBrowser:
    """Launch only a dedicated disposable browser; never kill a user's Chrome."""
    def __init__(self, *, headless=False):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            executable = runtime.chromium.executable_path
        if not Path(executable).is_file():
            raise CollectionError("未安装采集浏览器，请运行 python -m playwright install chromium；也可连接现有 CDP 浏览器。")
        self.profile = tempfile.TemporaryDirectory(prefix="agent-frame-human-")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}"
        args = [executable, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile.name}", "--no-first-run", "--no-default-browser-check"]
        if headless:
            args.append("--headless=new")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import httpx
            deadline = time.monotonic() + 15
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise CollectionError("采集浏览器未能启动；无桌面环境时请选择后台浏览器。")
                    try:
                        if client.get(self.endpoint + "/json/version").status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
            raise CollectionError("采集浏览器连接超时。")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        try:
            self.profile.cleanup()
        except OSError:
            pass


class CollectionController:
    def __init__(self, root, *, browser_factory=None, recorder_factory=None, agent_busy=None,
                 desktop_lease_factory=None):
        self.root = Path(root)
        self.browser_factory = browser_factory
        self.recorder_factory = recorder_factory
        self.agent_busy = agent_busy or (lambda: False)
        self.desktop_lease_factory = desktop_lease_factory
        self.desktop_lease = None
        self.lock = threading.RLock()
        self.status = "idle"
        self.session_id = None
        self.browser = self.recorder = self.owned = self.observation = None
        self.prepared = None
        self.requests = {}
        self.error = ""

    @property
    def active(self):
        return self.status in {"starting", "recording", "paused"}

    def _check_session(self, payload):
        if not self.session_id or payload.get("session_id") != self.session_id:
            raise CollectionError("采集会话已变化，请刷新采集状态。")

    def start(self, payload):
        with self.lock:
            if self.active or self.agent_busy():
                raise CollectionError("已有任务或采集正在运行，请先结束当前任务。")
            task = str(payload.get("task") or "").strip()
            if not task or len(task) > 8000:
                raise CollectionError("请填写任务描述（最多 8000 字）。")
            start_url = http_url(payload.get("start_url")) if payload.get("start_url") else None
            from ..verifiers import CompletionVerifier
            CompletionVerifier(task, payload.get("completion_spec"))  # Validate before launching anything.
            self._close_browser()
            if self.recorder is not None:
                self.recorder.close()
                self.recorder = None
            self.session_id = uuid.uuid4().hex
            self.directory = self.root / self.session_id
            self.directory.mkdir(parents=True, exist_ok=False)
            self.status, self.error, self.requests = "starting", "", {}
            self.observation = self.prepared = None
            try:
                if self.desktop_lease_factory:
                    self.desktop_lease = self.desktop_lease_factory('collection-' + self.session_id)
                if self.browser_factory:
                    self.browser = self.browser_factory(payload, self.directory)
                else:
                    endpoint = str(payload.get("endpoint") or "http://127.0.0.1:9222")
                    if payload.get("launch_browser", True):
                        self.owned = OwnedBrowser(headless=bool(payload.get("headless", False)))
                        endpoint = self.owned.endpoint
                    elif urlsplit(endpoint).scheme not in {"http", "https", "ws", "wss"}:
                        raise CollectionError("CDP 地址必须使用 http(s) 或 ws(s)。")
                    self.browser = BrowserSession(BrowserOptions(endpoint=endpoint, require_focus=False),
                        downloads_dir=str(self.directory / "downloads"))
                initial = self.browser.begin_episode(task_id=self.session_id, viewport={"width": 1280, "height": 800})
                if start_url:
                    result = self.browser.goto(start_url)
                    if result.get("status") != "executed":
                        raise CollectionError("起始页面未能打开：" + str(result.get("error", "请检查网址")))
                if self.recorder_factory is None:
                    from ..human_demonstration import HumanDemonstrationSession
                    factory = HumanDemonstrationSession
                else:
                    factory = self.recorder_factory
                self.recorder = factory(task, self.directory, completion_spec=payload.get("completion_spec"),
                                        session_id=self.session_id)
                self.initial_state = initial
                self._capture()
                self.status = "recording"
                return self.state()
            except BaseException as exc:
                self.status, self.error = "error", str(exc)
                try:
                    self._close_browser()
                    if self.recorder is not None:
                        self.recorder.close()
                finally:
                    self._release_desktop()
                raise

    def _capture(self, *, prepare=True):
        shot, ax, info = self.browser.collect_observation()
        if shot is None and not info.get("dialog"):
            self.observation = self.prepared = None
            raise CollectionError("当前页面截图不可用，请刷新画面后继续。")
        info = dict(info or {})
        info["observation_id"] = "human_" + uuid.uuid4().hex
        self.observation = Observation(screenshot=shot, context=[ax] if ax else [], info=info)
        self.prepared = self.recorder.prepare_observation(self.observation) if prepare else None
        if self.prepared:
            self.observation.info["exposed_refs"] = self.prepared["prompt_metadata"].get("exposed_refs", [])

    def state(self):
        with self.lock:
            frame = None
            if self.observation:
                info = self.observation.info
                width, height = info.get("screenshot_size") or (1280, 800)
                frame = {"id": info["observation_id"], "screenshot": encode_image(self.observation.screenshot),
                    "url": info.get("url", ""), "width": width, "height": height,
                    "tabs": copy.deepcopy(info.get("tabs", [])), "dialog": copy.deepcopy(info.get("dialog")),
                    "screenshot_stale": bool(info.get("screenshot_stale"))}
            summary = self.recorder.summary() if self.recorder else {}
            return {"ok": True, "session_id": self.session_id, "status": self.status, "error": self.error,
                    "task": self.recorder.task if self.recorder and hasattr(self.recorder, "task") else "",
                    "observation": frame, "steps": summary.get("steps", []), "verification": summary.get("verification", {})}

    def frame(self, payload):
        with self.lock:
            self._check_session(payload)
            if self.status not in {"recording", "paused"}:
                return self.state()
            self._capture()
            return self.state()

    @staticmethod
    def _number(payload, key):
        value = payload.get(key)
        if type(value) not in {float, int} or not math.isfinite(value):
            raise CollectionError("操作坐标无效，请刷新画面。")
        return value

    def action(self, payload):
        with self.lock:
            self._check_session(payload)
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise CollectionError("操作缺少唯一 request_id。")
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if request_id in self.requests:
                if self.requests[request_id] != fingerprint:
                    raise CollectionError("同一个操作编号不能用于不同操作。")
                return {**self.state(), "duplicate": True}
            if self.status != "recording":
                raise CollectionError("请先开始或继续采集。")
            if self.observation is None or payload.get("observation_id") != self.observation.info["observation_id"]:
                raise CollectionError("画面已更新，这次操作未执行；请在新画面上重试。")
            if len(self.requests) >= 2000:
                raise CollectionError("本次示范已达到 2000 步，请结束并创建新示范。")
            kind = payload.get("kind")
            allowed = {"click", "double_click", "right_click", "drag_and_drop", "scroll", "type", "press",
                       "hotkey", "goto", "switch_tab", "close_tab", "handle_dialog"}
            if kind not in allowed:
                raise CollectionError("不支持该人工采集动作。")
            before = self.observation
            prepared = self.prepared
            binding = before.info
            inspect = {}
            args, resolved = {}, {}
            sft_exclude_reason = None
            if binding.get("dialog") and kind != "handle_dialog":
                raise CollectionError("请先处理页面对话框。")
            if kind in {"click", "double_click", "right_click", "drag_and_drop"}:
                x, y = self._number(payload, "x"), self._number(payload, "y")
                inspect = self.browser.inspect_point(x, y, binding)
                resolved = {"x": x, "y": y}
            elif kind in {"type", "press", "hotkey"}:
                inspect = self.browser.inspect_focused(binding)
            elif kind == "scroll" and ("x" in payload or "y" in payload):
                x, y = self._number(payload, "x"), self._number(payload, "y")
                inspect = self.browser.inspect_scroll_target(x, y, binding)
                resolved = {"x": x, "y": y}
            if inspect.get("status") == "rejected":
                raise CollectionError("页面或元素已变化，请刷新画面后重试。")
            if inspect.get("sensitive"):
                raise CollectionError("该控件涉及密码或支付信息，不纳入示范采集。请先在独立登录流程中完成操作。")
            target = inspect.get("target_label") or str(payload.get("target") or "").strip()
            if target:
                args["target"] = target[:400]
            if inspect.get("target_ref"):
                args["target_ref"] = inspect["target_ref"]
            if kind in {"click", "double_click", "right_click", "drag_and_drop"} and (
                inspect.get("role") in {"slider", "scrollbar"} or inspect.get("tag") == "canvas"
                or inspect.get("input_type") == "range"
            ):
                sft_exclude_reason = "position_sensitive_target"
            if kind == "drag_and_drop":
                x2, y2 = self._number(payload, "x2"), self._number(payload, "y2")
                destination = self.browser.inspect_point(x2, y2, binding)
                if destination.get("status") == "rejected" or destination.get("sensitive"):
                    raise CollectionError("拖拽目标不可用，请刷新后重试。")
                if destination.get("role") in {"slider", "scrollbar"} or destination.get("tag") == "canvas" or destination.get("input_type") == "range":
                    sft_exclude_reason = "position_sensitive_target"
                args = {"from_target": target, "to_target": destination.get("target_label") or payload.get("to_target", "")}
                resolved = {"x1": resolved["x"], "y1": resolved["y"], "x2": x2, "y2": y2}
            elif kind == "type":
                text = payload.get("text")
                if not isinstance(text, str) or not text or len(text) > 20000:
                    raise CollectionError("输入内容应为 1～20000 字。")
                if not inspect.get("editable") or not inspect.get("bounds"):
                    raise CollectionError("请先在浏览器画面中点击要输入的文本框。")
                x, y, w, h = inspect["bounds"]
                args.update(text=text, overwrite=bool(payload.get("overwrite", False)))
                resolved.update(x=x + w / 2, y=y + h / 2)
                if not args["overwrite"] and inspect.get("editable_has_text") is not False:
                    # Decision type does not encode a caret/selection position.
                    # Keep real partial edits for review, never teach a label
                    # that may insert the same text at a different position.
                    sft_exclude_reason = "caret_sensitive_text_edit"
            elif kind == "scroll":
                direction, amount = payload.get("direction"), payload.get("amount", 1)
                if direction not in {"up", "down"} or type(amount) is not int or not 1 <= amount <= 30:
                    raise CollectionError("滚动方向或幅度无效。")
                args.update(direction=direction, amount=amount)
                if resolved and not inspect.get("root_scroll") and not inspect.get("target_ref"):
                    sft_exclude_reason = "nested_scroll_target_not_exposed"
            elif kind == "press":
                key = payload.get("key")
                if not isinstance(key, str) or not key.strip() or len(key) > 64:
                    raise CollectionError("按键无效。")
                args = {"key": key}
            elif kind == "hotkey":
                keys = payload.get("keys")
                if not isinstance(keys, list) or not 1 <= len(keys) <= 5 or any(not isinstance(k, str) or not 1 <= len(k) <= 32 for k in keys):
                    raise CollectionError("快捷键无效。")
                args = {"keys": keys}
            elif kind == "goto":
                args = {"url": http_url(payload.get("url"))}
            elif kind in {"switch_tab", "close_tab"}:
                tab = payload.get("tab_id")
                if tab not in {t.get("tab_id") for t in binding.get("tabs", [])}:
                    raise CollectionError("标签页已变化，请刷新画面。")
                args = {"tab_id": tab}
            elif kind == "handle_dialog":
                if type(payload.get("accept")) is not bool or not binding.get("dialog"):
                    raise CollectionError("没有待处理的对话框。")
                args = {"accept": payload["accept"], "dialog_id": binding["dialog"]["dialog_id"]}
                if "prompt_text" in payload:
                    args["prompt_text"] = str(payload["prompt_text"])[:4000]
            action = Action(kind, args)
            # Reserve before dispatch: a network retry must never repeat an input.
            self.requests[request_id] = fingerprint
            metadata = {"target_label": target, "resolved_action": {"action": kind, "args": {**args, **resolved}},
                        "request_id": request_id, "preparation_id": prepared["preparation_id"],
                        "frozen_policy_input": prepared["policy_input"], "frozen_prompt_metadata": prepared["prompt_metadata"]}
            if sft_exclude_reason:
                metadata["sft_exclude_reason"] = sft_exclude_reason
            try:
                if kind == "goto":
                    info = self.browser.goto(args["url"])
                elif kind in {"switch_tab", "close_tab", "handle_dialog"}:
                    info = getattr(self.browser, kind)(**args)
                elif kind == "type":
                    info = self.browser.insert_focused_text(args["text"], binding,
                        overwrite=args["overwrite"], expected_backend_id=inspect.get("focused_backend_id", inspect.get("backend_id")))
                elif kind == "scroll" and args.get("target_ref"):
                    # Use the same container and unit as inference, so the
                    # semantic label describes the operation actually executed.
                    route = self.browser.route(action, before)
                    if route and route.get("channel") == "browser_dom":
                        info = self.browser.execute(action, route)
                    else:
                        info = {"status": "rejected", "dispatched": False, "error": "Scroll target changed"}
                else:
                    info = self.browser.execute_viewport_action(Action(kind, {**args, **resolved}), binding)
            except Exception as exc:
                info = {"status": "uncertain", "dispatched": True, "error_type": type(exc).__name__}
            try:
                self._capture(prepare=False)
                after = self.observation
            except Exception:
                after = Observation(info={"observation_error": "post_action_capture_failed"})
                self.observation = self.prepared = None
                info = {**info, "observation_error": "post_action_capture_failed"}
            self.recorder.record_transition(action, before, after, execution_info=info, metadata=metadata)
            if self.observation is not None:
                self.prepared = self.recorder.prepare_observation(self.observation)
                self.observation.info["exposed_refs"] = self.prepared["prompt_metadata"].get("exposed_refs", [])
            return {**self.state(), "execution": info}

    def pause(self, payload):
        with self.lock:
            self._check_session(payload)
            if self.status != "recording":
                raise CollectionError("当前不在采集中。")
            self.status = "paused"
            return self.state()

    def resume(self, payload):
        with self.lock:
            self._check_session(payload)
            if self.status != "paused":
                raise CollectionError("当前未暂停。")
            self._capture()
            self.status = "recording"
            return self.state()

    def stop(self, payload):
        with self.lock:
            self._check_session(payload)
            if self.status == "stopped":
                return self.state()
            if self.status not in {"recording", "paused"}:
                raise CollectionError("没有进行中的采集。")
            try:
                self._capture()
                final_observation = self.observation
            except Exception:
                self.observation = None
                # An old page is not final-state evidence. The verifier still
                # rechecks known downloaded files independently of page state.
                final_observation = Observation(info={"observation_error": "final_capture_failed"})
            self.recorder.stop(claimed_complete=bool(payload.get("claimed_complete", False)),
                               final_observation=final_observation)
            self.status = "stopped"
            try:
                self._close_browser()
            finally:
                self._release_desktop()
            return self.state()

    def review(self, payload):
        with self.lock:
            self._check_session(payload)
            if self.recorder is None or type(payload.get("keep")) is not bool:
                raise CollectionError("请选择保留或丢弃该步骤。")
            self.recorder.review_step(payload.get("step"), keep=payload["keep"],
                target=payload.get("target"), intent=payload.get("intent"), expected_effect=payload.get("expected_effect"))
            return self.state()

    def export(self, payload, format="zip"):
        with self.lock:
            self._check_session(payload)
            if self.status != "stopped":
                raise CollectionError("请先结束采集，再导出已审阅的示范。")
            result = self.recorder.export_sft()
            if not result.get("count"):
                raise CollectionError("没有可导出的步骤。请保留有效步骤，并为未识别的点击填写目标描述。")
            source = Path(result["path"])
            if format == "jsonl":
                return source
            if format != "zip":
                raise CollectionError("仅支持 zip 或 jsonl。")
            bundle = self.directory / "sft-dataset.zip"
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(source, "sft.jsonl")
                for relative in result.get("assets", []):
                    image = (source.parent / relative).resolve()
                    if not relative.startswith("assets/") or not image.is_relative_to(source.parent.resolve()) or not image.is_file():
                        raise CollectionError("导出图片缺失，请重新导出。")
                    archive.write(image, relative)
            return bundle

    def _close_browser(self):
        if self.browser is not None:
            try:
                self.browser.close()
            finally:
                self.browser = None
        if self.owned is not None:
            try:
                self.owned.close()
            finally:
                self.owned = None

    def close(self):
        with self.lock:
            try:
                self._close_browser()
                if self.recorder is not None:
                    self.recorder.close()
            finally:
                self._release_desktop()

    def _release_desktop(self):
        if self.desktop_lease is not None:
            self.desktop_lease.close()
            self.desktop_lease = None


def collection_router(controller):
    router = APIRouter(prefix="/collect")

    def failure(exc):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/status")
    def status():
        return controller.state()

    @router.get("/frame")
    def frame(session_id: str):
        try:
            return controller.frame({"session_id": session_id})
        except Exception as exc:
            return failure(exc)

    @router.get("/export")
    def export(session_id: str, format: str = "zip"):
        try:
            path = controller.export({"session_id": session_id}, format)
            return FileResponse(path, filename=path.name, media_type="application/zip" if format == "zip" else "application/x-ndjson")
        except Exception as exc:
            return failure(exc)

    @router.get("/asset")
    def asset(session_id: str, name: str):
        try:
            with controller.lock:
                controller._check_session({"session_id": session_id})
                if Path(name).name != name or Path(name).suffix not in {".png", ".jpg"}:
                    raise CollectionError("图片名称无效。")
                root = (controller.directory / "assets").resolve()
                path = (root / name).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise CollectionError("采集图片不存在。")
                return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg")
        except Exception as exc:
            return failure(exc)

    def endpoint(method):
        def handle(request: Request, payload: dict):
            origin = request.headers.get("origin")
            if (origin and urlsplit(origin).netloc != request.headers.get("host")) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"ok": False, "error": "请从采集页面提交操作。"}, status_code=403)
            try:
                return method(payload)
            except Exception as exc:
                return failure(exc)
        return handle

    for name in ("start", "action", "pause", "resume", "stop", "review"):
        router.add_api_route("/" + name, endpoint(getattr(controller, name)), methods=["POST"], name="collect_" + name)
    return router
