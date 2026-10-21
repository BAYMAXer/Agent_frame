"""Device B: authenticated browser observation and execution, with no agent/model.

Only BrowserSession owns browser actions. Every request is serialized, and action
receipts are retained for the session so a lost HTTP response cannot replay input.
"""
from __future__ import annotations

import argparse
import base64
import copy
from dataclasses import dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

from .remote_browser_protocol import (PROTOCOL_VERSION, MAX_REQUEST_BYTES, MAX_ARTIFACT_CHUNK,
    MAX_SCREENSHOT_BYTES, RemoteBrowserError, checked_action, fingerprint, http_url, positive_number)
from .run_service import RunService


@dataclass
class ExecutorConfig:
    device_id: str
    workspace_root: str
    token_env: str = 'YAERO_BROWSER_TOKEN'
    cdp_url: str | None = None
    profile_dir: str | None = None
    headless: bool = False
    session_timeout_s: float = 3600
    idle_timeout_s: float = 60
    download_max_bytes: int = 50 * 1024 * 1024
    desktop_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.device_id, str) or not self.device_id.strip() or len(self.device_id) > 128:
            raise ValueError('A nonempty device_id is required.')
        if not self.workspace_root or not Path(self.workspace_root).expanduser().is_absolute():
            raise ValueError('workspace_root must be an absolute directory on B.')
        self.workspace_root = str(Path(self.workspace_root).expanduser().resolve())
        positive_number(self.session_timeout_s, 'session timeout', 86400)
        positive_number(self.idle_timeout_s, 'idle timeout', 3600)
        if self.cdp_url:
            http_url(self.cdp_url)
            if urlsplit(self.cdp_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
                raise ValueError('B may attach only to its own loopback CDP browser.')
        if type(self.download_max_bytes) is not int or self.download_max_bytes <= 0:
            raise ValueError('download_max_bytes must be positive.')


class ManagedChromium:
    """A Playwright-owned browser on one dedicated thread, exposed only by CDP."""
    def __init__(self, config):
        from .persistent_chrome import prepare_persistent_profile
        profile = config.profile_dir or str(Path(config.workspace_root) / '.browser-profile')
        profile = prepare_persistent_profile(profile)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.endpoint = f'http://127.0.0.1:{port}'
        self._ready, self._stop = threading.Event(), threading.Event()
        self._error = None

        def own_browser():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as runtime:
                    context = runtime.chromium.launch_persistent_context(profile,
                        headless=config.headless, accept_downloads=True,
                        viewport={'width': 1280, 'height': 800},
                        args=[f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
                              '--disable-extensions', '--no-first-run'])
                    try:
                        self._ready.set()
                        self._stop.wait()
                    finally:
                        context.close()
            except BaseException as exc:
                self._error = exc
                self._ready.set()

        self._thread = threading.Thread(target=own_browser, name='executor-chromium', daemon=True)
        self._thread.start()
        if not self._ready.wait(30):
            self.close()
            raise RuntimeError('Dedicated Chromium startup timed out.')
        if self._error:
            self.close()
            raise RuntimeError('Dedicated Chromium could not start: ' + str(self._error)) from self._error
        try:
            import httpx
            deadline = time.monotonic() + 10
            with httpx.Client(trust_env=False, timeout=.5) as client:
                while time.monotonic() < deadline:
                    try:
                        if client.get(self.endpoint + '/json/version').status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
            raise RuntimeError('Dedicated Chromium CDP endpoint did not become ready.')
        except BaseException:
            self.close()
            raise

    def close(self):
        self._stop.set()
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise RuntimeError('Dedicated browser did not finish closing; executor must remain unavailable.')


class BrowserExecutor:
    def __init__(self, config, *, session_factory=None, lease_factory=None, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.lock = threading.RLock()
        self.session_factory = session_factory
        self.service = RunService(Path(config.workspace_root) / '.executor-tasks', desktop_id=config.desktop_id)
        self.lease_factory = lease_factory or self.service.acquire_external
        self.session = self.browser = self.lease = None
        self._cleanup_error = None
        self.session_id = None
        self.observation = None
        self.observation_id = None
        self.routes = {}
        self.receipts = {}
        self.artifacts = {}
        self.deadline = self.idle_deadline = 0
        self.idle_timeout_s = config.idle_timeout_s
        self._closed = threading.Event()
        self._janitor = threading.Thread(target=self._expire_loop, name='browser-lease-expiry', daemon=True)
        self._janitor.start()

    def _expire_loop(self):
        while not self._closed.wait(.5):
            with self.lock:
                try:
                    self._expire()
                except Exception as exc:
                    self._cleanup_error = str(exc)

    def _expire(self):
        if self.session is not None and self.clock() >= min(self.deadline, self.idle_deadline):
            self._close_session()

    def _close_session(self):
        session, browser, lease = self.session, self.browser, self.lease
        self.session = self.browser = self.lease = None
        self.session_id = None
        self.observation = self.observation_id = None
        self.routes.clear()
        self.artifacts.clear()
        errors = []
        try:
            if session is not None:
                session.close()
        except Exception as exc:
            errors.append(exc)
        try:
            if browser is not None:
                browser.close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            # Keep the desktop exclusion held if teardown cannot be confirmed.
            self.session, self.browser, self.lease = session, browser, lease
            self._cleanup_error = str(errors[0])
            raise RemoteBrowserError('Browser cleanup did not complete; executor remains reserved.', 'cleanup_failed')
        if lease is not None:
            lease.close()
        self._cleanup_error = None

    def close(self):
        self._closed.set()
        with self.lock:
            self._close_session()
        self.service.close()
        if threading.current_thread() is not self._janitor:
            self._janitor.join(timeout=2)

    def _require_session(self, value):
        self._expire()
        if self._cleanup_error or self.session is None or value != self.session_id:
            raise RemoteBrowserError('Browser session is closed, expired, or belongs to another request.', 'session_expired')
        self.idle_deadline = min(self.deadline, self.clock() + self.idle_timeout_s)

    def rpc(self, body):
        with self.lock:
            if not isinstance(body, dict) or body.get('device_id') != self.config.device_id:
                raise RemoteBrowserError('Configured device_id does not match this executor.', 'device_mismatch')
            if set(body) - {'device_id', 'session_id', 'request_id', 'method', 'params'}:
                raise RemoteBrowserError('Unknown request fields.', 'invalid_request')
            method, params = body.get('method'), body.get('params', {})
            request_id = body.get('request_id')
            if not isinstance(params, dict) or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise RemoteBrowserError('Each RPC requires object params and a unique request_id.', 'invalid_request')
            digest = fingerprint(body)
            # Never replay an old mutation, even after its session has expired.
            if request_id in self.receipts:
                old_digest, response = self.receipts[request_id]
                if old_digest != digest:
                    raise RemoteBrowserError('request_id was used with different parameters.', 'request_conflict')
                return copy.deepcopy(response)
            self._expire()
            if method == 'capabilities':
                return {'protocol': PROTOCOL_VERSION, 'device_id': self.config.device_id,
                        'scope': 'browser_only', 'busy': self.session is not None,
                        'session_timeout_s': self.config.session_timeout_s, 'idle_timeout_s': self.config.idle_timeout_s}
            mutating = method in {'open_session', 'execute', 'cancel_download', 'close_session'}
            if mutating and len(self.receipts) >= 10000:
                raise RemoteBrowserError('Receipt capacity reached; restart the executor after closing its session.', 'receipt_capacity')
            if method != 'open_session':
                self._require_session(body.get('session_id'))
            try:
                response = self._dispatch(method, params)
            except RemoteBrowserError as exc:
                if mutating:
                    response = {'error': {'code': exc.code, 'message': str(exc), 'dispatched': exc.dispatched}}
                    self.receipts[request_id] = (digest, response)
                raise
            except Exception as exc:
                # Input may already have reached Chromium. A retry gets this same
                # uncertain receipt and must observe, never repeat the action.
                response = {'error': {'code': 'executor_failure', 'message': str(exc)[:500],
                                      'dispatched': method == 'execute'}}
            if mutating:
                self.receipts[request_id] = (digest, copy.deepcopy(response))
            return response

    def _dispatch(self, method, params):
        if method == 'open_session':
            return self._open(params)
        if method == 'heartbeat':
            return {'session_id': self.session_id, 'remaining_s': max(0, self.deadline - self.clock())}
        if method == 'observe':
            return self._observe()
        if method == 'events':
            return self._public_info(self.session.event_metadata())
        if method == 'download_policy':
            timeout = positive_number(params.get('timeout_s'), 'download timeout', self.config.session_timeout_s)
            maximum = params.get('max_bytes')
            if type(maximum) is not int or maximum < 0 or maximum > self.config.download_max_bytes:
                raise RemoteBrowserError('Download bytes exceed the configured B resource limit.', 'invalid_argument')
            self.session.options.download_timeout_s = min(timeout, max(.1, self.deadline - self.clock()))
            self.session.options.download_max_bytes = maximum or self.config.download_max_bytes
            return {'timeout_s': self.session.options.download_timeout_s,
                    'max_bytes': self.session.options.download_max_bytes}
        if method == 'route':
            return self._route(params)
        if method == 'execute':
            return {'result': self._execute(params)}
        if method == 'get_page_source':
            maximum = params.get('max_chars', 20000)
            if type(maximum) is not int or not 1 <= maximum <= 20000:
                raise RemoteBrowserError('max_chars must be 1–20000.', 'invalid_argument')
            return {'text': self.session.get_page_source(target_ref=params.get('target_ref'), max_chars=maximum)}
        if method == 'cancel_download':
            return self._public_info(self.session.cancel_download(str(params.get('download_id', ''))))
        if method == 'close_session':
            closed_id = self.session_id
            self._close_session()
            return {'session_id': closed_id, 'closed': True}
        raise RemoteBrowserError('Unsupported RPC method.', 'unsupported_method')

    def _open(self, params):
        if set(params) - {'task_id', 'viewport', 'start_url', 'session_timeout_s', 'idle_timeout_s'}:
            raise RemoteBrowserError('Browser endpoint, profile and workspace are configured only on B.', 'invalid_argument')
        if self.session is not None or self.lease is not None:
            raise RemoteBrowserError('B already has an active browser session.', 'desktop_busy')
        duration = positive_number(params.get('session_timeout_s', self.config.session_timeout_s),
                                   'session_timeout_s', self.config.session_timeout_s)
        idle = positive_number(params.get('idle_timeout_s', self.config.idle_timeout_s),
                               'idle_timeout_s', self.config.idle_timeout_s)
        viewport = params.get('viewport', {'width': 1280, 'height': 800})
        if (not isinstance(viewport, dict) or set(viewport) != {'width', 'height'} or
                any(type(v) is not int or not 200 <= v <= 4096 for v in viewport.values())):
            raise RemoteBrowserError('Invalid viewport.', 'invalid_argument')
        start_url = http_url(params['start_url']) if params.get('start_url') else None
        session_id = uuid.uuid4().hex
        self.lease = self.lease_factory('browser-executor-' + session_id)
        try:
            root = Path(self.config.workspace_root) / 'sessions' / session_id
            root.mkdir(parents=True, exist_ok=False)
            self.download_root = root.resolve()
            if self.session_factory:
                self.session = self.session_factory(self.config, root)
            else:
                from .browser_session import BrowserSession, BrowserOptions
                endpoint = self.config.cdp_url
                if not endpoint:
                    self.browser = ManagedChromium(self.config)
                    endpoint = self.browser.endpoint
                self.session = BrowserSession(BrowserOptions(endpoint=endpoint, require_focus=False,
                    session_mode='persistent', download_max_bytes=self.config.download_max_bytes), workspace_dir=str(root))
            self.session_id = session_id
            self.initial_state = self.session.begin_episode(task_id=params.get('task_id') or session_id,
                isolated=False, viewport=viewport, start_url=start_url)
            self.initial_state['execution_capabilities'] = {'browser_only': True, 'dom': True,
                'css_viewport': True, 'desktop': False, 'cli': False, 'arbitrary_javascript': False}
            self.deadline = self.clock() + duration
            self.idle_timeout_s = idle
            self.idle_deadline = self.clock() + idle
            return {'protocol': PROTOCOL_VERSION, 'device_id': self.config.device_id,
                    'session_id': session_id, 'idle_timeout_s': idle, 'session_timeout_s': duration,
                    'initial_state': self._public_info(self.initial_state)}
        except BaseException:
            self._close_session()
            raise

    def _public_info(self, value):
        def convert(item):
            if isinstance(item, list):
                return [convert(child) for child in item]
            if not isinstance(item, dict):
                return item
            item = copy.deepcopy(item)
            artifact_id, path = item.get('artifact_id'), item.get('path')
            if isinstance(artifact_id, str) and isinstance(path, str) and item.get('completed') is True:
                candidate = Path(path)
                try:
                    resolved = candidate.resolve(strict=True)
                    if (not resolved.is_relative_to(self.download_root) or candidate.is_symlink()
                            or not resolved.is_file()):
                        raise ValueError('Artifact outside session downloads')
                    self.artifacts[artifact_id] = {'path': resolved, 'metadata': copy.deepcopy(item)}
                except (OSError, ValueError):
                    item.update(completed=False, status='unavailable')
                item.update(device_id=self.config.device_id, session_id=self.session_id)
            for key in ('path', 'from_path', 'to_path', 'workspace_dir', 'workspace_root', 'downloads_dir', 'file_identity'):
                if key in item:
                    item['remote_' + key] = item.pop(key)
            if 'workspace_location' in item:
                item['workspace_location'] = 'remote_browser'
            return {key: convert(child) for key, child in item.items()}
        result = convert(value)
        if isinstance(result, dict):
            result['remote_browser'] = {'protocol': PROTOCOL_VERSION, 'device_id': self.config.device_id,
                                        'session_id': self.session_id}
        return result

    def _observe(self):
        from .env import Observation
        from .scene import browser_scene
        shot, snapshot, info = self.session.collect_observation()
        if shot is None or (info or {}).get('screenshot_space') != 'css_viewport':
            self.observation = self.observation_id = None
            self.routes.clear()
            raise RemoteBrowserError('B could not obtain a fresh browser viewport observation.', 'observation_unavailable')
        buffer = io.BytesIO()
        if isinstance(shot, bytes):
            from PIL import Image
            shot = Image.open(io.BytesIO(shot))
        shot.save(buffer, format='PNG')
        image = buffer.getvalue()
        if len(image) > MAX_SCREENSHOT_BYTES:
            raise RemoteBrowserError('Viewport screenshot exceeded the transfer limit.', 'observation_too_large')
        info = dict(info or {})
        info['execution_capabilities'] = dict(self.initial_state['execution_capabilities'])
        self.observation_id = uuid.uuid4().hex
        info['observation_id'] = self.observation_id
        info['scene'] = browser_scene(snapshot, shot, page_id=info.get('page_id', ''),
                                     snapshot_id=info.get('snapshot_id', '')).to_dict()
        self.observation = Observation(screenshot=shot, info=info, context=[snapshot] if snapshot else [])
        self.routes.clear()
        return {'screenshot_png': base64.b64encode(image).decode('ascii'), 'text': '',
                'context': self.observation.context, 'info': self._public_info(info)}

    def _check_observation(self, value):
        if self.observation is None or value != self.observation_id:
            raise RemoteBrowserError('Action observation has been superseded; observe again.', 'stale_observation')

    def _route(self, params):
        self._check_observation(params.get('observation_id'))
        action = checked_action(params.get('action'))
        # A may narrow exposed refs to the policy prompt, never add unknown refs.
        exposed = params.get('exposed_refs')
        observed = copy.deepcopy(self.observation)
        if exposed is not None:
            if not isinstance(exposed, list) or any(not isinstance(v, str) for v in exposed):
                raise RemoteBrowserError('Invalid exposed_refs.', 'invalid_argument')
            observed.info['exposed_refs'] = [ref for ref in exposed if ref in getattr(self.session, 'nodes', {})]
        route = self.session.route(action, observed)
        if route is None and action.action in {'goto', 'scroll', 'press', 'hotkey'}:
            route = {'channel': 'browser_dom', 'coordinate_space': 'css_viewport'}
        if route is None:
            return {'route': None}
        if route.get('channel') == 'reobserve':
            return {'route': route}
        token = uuid.uuid4().hex
        self.routes[token] = (fingerprint(params['action']), copy.deepcopy(route), self.observation_id)
        return {'route': {**route, 'remote_route_token': token}}

    def _execute(self, params):
        self._check_observation(params.get('observation_id'))
        action = checked_action(params.get('action'))
        name, args = action.action, action.args
        if args.get('grounding_failed'):
            return {'status': 'rejected', 'dispatched': False, 'error_code': 'grounding_failed'}
        route = None
        if params.get('route_token'):
            stored = self.routes.get(params['route_token'])
            if not stored or stored[0] != fingerprint(params['action']) or stored[2] != self.observation_id:
                raise RemoteBrowserError('Route is stale or does not match the action.', 'stale_route')
            route = stored[1]
        target_ref = (route or {}).get('target_ref')
        if target_ref:
            node = getattr(self.session, 'nodes', {}).get(target_ref, {})
            href = node.get('url') or node.get('href')
            if href and urlsplit(str(href)).scheme.lower() in {'javascript', 'file', 'chrome', 'chrome-extension', 'devtools'}:
                raise RemoteBrowserError('This link leaves the permitted browser web surface.', 'unsupported_url')
        try:
            if name == 'goto':
                result = self.session.goto(args['url'])
            elif target_ref:
                result = self.session.execute(action, route)
            elif name in {'click', 'double_click', 'right_click', 'type', 'drag_and_drop', 'scroll', 'press', 'hotkey'}:
                if name not in {'scroll', 'press', 'hotkey'} and args.get('coordinate_space') != 'css_viewport':
                    raise RemoteBrowserError('A coordinate action requires CSS viewport grounding.', 'invalid_coordinates')
                result = self.session.execute_viewport_action(action, metadata=self.observation.info)
            elif name in {'handle_dialog', 'list_tabs', 'switch_tab', 'close_tab'}:
                fields = {'handle_dialog': {'accept', 'prompt_text', 'dialog_id'}, 'list_tabs': set(),
                          'switch_tab': {'tab_id'}, 'close_tab': {'tab_id'}}[name]
                result = getattr(self.session, name)(**{key: value for key, value in args.items() if key in fields})
            elif name == 'wait':
                time.sleep(args.get('seconds', args.get('duration', 1)))
                result = {'status': 'executed', 'dispatched': False, 'action': name}
            elif name == 'observe_browser':
                result = {'status': 'executed', 'dispatched': False, 'action': name}
            elif name == 'get_page_source':
                result = {'status': 'executed', 'dispatched': False, 'text': self.session.get_page_source(max_chars=20000)}
            else:
                raise RemoteBrowserError('Action requires a current DOM route.', 'unroutable_action')
            return self._public_info(result if isinstance(result, dict) else {'value': result, 'status': 'executed'})
        finally:
            # Even uncertain dispatch may have changed the document. No second
            # action may use this observation until A explicitly observes again.
            self.observation = self.observation_id = None
            self.routes.clear()

    def read_artifact(self, body):
        with self.lock:
            if not isinstance(body, dict) or body.get('device_id') != self.config.device_id:
                raise RemoteBrowserError('Wrong device.', 'device_mismatch')
            if set(body) - {'device_id', 'session_id', 'artifact_id', 'offset', 'max_bytes', 'snapshot_token'}:
                raise RemoteBrowserError('Artifact reads accept registered IDs only.', 'invalid_request')
            self._require_session(body.get('session_id'))
            artifact_id = body.get('artifact_id')
            if not isinstance(artifact_id, str) or artifact_id not in self.artifacts:
                raise RemoteBrowserError('Unknown registered artifact_id.', 'artifact_not_found')
            offset, maximum = body.get('offset', 0), body.get('max_bytes', MAX_ARTIFACT_CHUNK)
            if type(offset) is not int or offset < 0 or type(maximum) is not int or not 1 <= maximum <= MAX_ARTIFACT_CHUNK:
                raise RemoteBrowserError('Invalid artifact byte range.', 'invalid_argument')
            record = self.artifacts[artifact_id]
            path, metadata = record['path'], record['metadata']
            try:
                if path.is_symlink() or not path.resolve(strict=True).is_relative_to(self.download_root):
                    raise OSError('Artifact path changed.')
                before = path.stat()
                if metadata.get('file_identity') != [before.st_dev, before.st_ino]:
                    raise RemoteBrowserError('Downloaded artifact file identity changed after registration.', 'artifact_changed')
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(MAX_ARTIFACT_CHUNK), b''):
                        digest.update(chunk)
                    sha = digest.hexdigest()
                    if sha != metadata.get('sha256') or before.st_size != metadata.get('bytes'):
                        raise RemoteBrowserError('Downloaded artifact changed after registration.', 'artifact_changed')
                    token = fingerprint([artifact_id, before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size, sha])
                    if body.get('snapshot_token') not in (None, token):
                        raise RemoteBrowserError('Artifact snapshot changed during transfer.', 'artifact_changed')
                    stream.seek(offset)
                    data = stream.read(maximum)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise RemoteBrowserError('Artifact changed during read.', 'artifact_changed')
            except OSError as exc:
                raise RemoteBrowserError('Registered artifact is no longer readable.', 'artifact_unavailable') from exc
            return {'artifact_id': artifact_id, 'device_id': self.config.device_id, 'session_id': self.session_id,
                    'data_base64': base64.b64encode(data).decode('ascii'), 'offset': offset,
                    'size': before.st_size, 'sha256': sha, 'snapshot_token': token,
                    'eof': offset + len(data) >= before.st_size,
                    'metadata': self._public_info(metadata), 'artifact': self._public_info(metadata)}


def make_http_server(executor, host='127.0.0.1', port=8766):
    if host not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Bind the browser executor to loopback; use an authenticated SSH tunnel for A.')
    token = os.environ.get(executor.config.token_env, '')
    if len(token) < 16:
        raise ValueError(f'{executor.config.token_env} must contain a token of at least 16 characters.')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log authorization, page content, task text, or URLs.

        def do_POST(self):
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                return self._send(401, {'ok': False, 'error': {'code': 'unauthorized', 'message': 'Bearer authentication required.'}})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise RemoteBrowserError('Request body size is invalid.', 'request_too_large')
                body = json.loads(self.rfile.read(length))
                if self.path == '/v1/rpc':
                    result = executor.rpc(body)
                elif self.path == '/v1/artifacts/read':
                    result = executor.read_artifact(body)
                else:
                    return self._send(404, {'ok': False, 'error': {'code': 'not_found'}})
                if isinstance(result, dict) and 'error' in result:
                    return self._send(409, {'ok': False, **result})
                self._send(200, {'ok': True, 'result': result})
            except RemoteBrowserError as exc:
                self._send(409, {'ok': False, 'error': {'code': exc.code, 'message': str(exc), 'dispatched': exc.dispatched}})
            except (ValueError, TypeError) as exc:
                self._send(400, {'ok': False, 'error': {'code': 'invalid_request', 'message': str(exc)[:300]}})
            except Exception:
                self._send(500, {'ok': False, 'error': {'code': 'executor_failure', 'message': 'Executor request failed.'}})

        def _send(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ':' in host else socket.AF_INET
    server = Server((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='Device B browser-only executor; run all agent/model logic on A.')
    parser.add_argument('--device-id', required=True)
    parser.add_argument('--workspace-root', required=True)
    parser.add_argument('--token-env', default='YAERO_BROWSER_TOKEN')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--cdp-url')
    parser.add_argument('--profile-dir')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--session-timeout', type=float, default=3600)
    parser.add_argument('--idle-timeout', type=float, default=60)
    args = parser.parse_args(argv)
    config = ExecutorConfig(device_id=args.device_id, workspace_root=args.workspace_root,
        token_env=args.token_env, cdp_url=args.cdp_url, profile_dir=args.profile_dir, headless=args.headless,
        session_timeout_s=args.session_timeout, idle_timeout_s=args.idle_timeout)
    executor = BrowserExecutor(config)
    server = None
    try:
        server = make_http_server(executor, args.host, args.port)
        print(f'Browser executor {args.device_id} listening on {args.host}:{server.server_port}', flush=True)
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        executor.close()


if __name__ == '__main__':
    main()
