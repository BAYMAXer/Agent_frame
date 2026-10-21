"""Device A environment: browser RPC only, never local desktop/CLI fallback."""
from __future__ import annotations

import base64
import copy
import io
import json
import os
from pathlib import Path
import threading
import uuid

from ..env import Environment, Observation, StepResult
from ..actions import Action
from ..remote_browser_protocol import RemoteBrowserError, checked_action, http_url, MAX_SCREENSHOT_BYTES


class RemoteBrowserEnvironment(Environment):
    evaluation_available = False
    browser_only = True
    cli_target = 'none'
    os_name = 'linux'  # Prompts describe web content; no OS input capability exists.

    def __init__(self, endpoint, device_id, token_env='YAERO_BROWSER_TOKEN', *, timeout_s=30,
                 session_timeout_s=1800, idle_timeout_s=60, width=1280, height=800,
                 artifact_mirror=None, http_client=None):
        self.endpoint = http_url(endpoint).rstrip('/')
        if not isinstance(device_id, str) or not device_id:
            raise ValueError('Remote browser requires an explicit device_id.')
        self.device_id, self.token_env = device_id, token_env
        self.token = os.environ.get(token_env, '')
        if len(self.token) < 16:
            raise ValueError(f'{token_env} must contain the browser executor bearer token (16+ characters).')
        self.session_timeout_s, self.idle_timeout_s = session_timeout_s, idle_timeout_s
        self.width, self.height = width, height
        self.browser_start_url = None
        self.session_id = None
        self.initial_state = {}
        self._last_observation = None
        self._last_observation_info = {}
        self._stop = threading.Event()
        self._heartbeat = None
        self._heartbeat_error = None
        self._mirror_spec = artifact_mirror
        self._mirror = None
        self._download_policy = None
        self._own_client = http_client is None
        if http_client is None:
            import httpx
            http_client = httpx.Client(timeout=timeout_s, trust_env=False)
        self.client = http_client

    def _invalidate(self, reason):
        if self._mirror is not None:
            self._mirror.invalidate_all(str(reason))

    def _post(self, path, body):
        import httpx
        for attempt in range(2):
            try:
                response = self.client.post(self.endpoint + path, json=body,
                    headers={'Authorization': 'Bearer ' + self.token})
                data = response.json()
                if response.status_code != 200 or not isinstance(data, dict) or data.get('ok') is not True:
                    error = data.get('error', {}) if isinstance(data, dict) else {}
                    raise RemoteBrowserError(error.get('message', 'Remote browser RPC failed.'),
                        error.get('code', 'remote_error'), dispatched=bool(error.get('dispatched')))
                return data['result']
            except (httpx.TransportError, TimeoutError, ConnectionError) as exc:
                self._invalidate(exc)
                if attempt == 0:
                    continue  # Same body/request_id: B returns its original action receipt.
                raise RemoteBrowserError('Connection to browser executor failed; no local fallback.',
                                         'connection_failed', dispatched=body.get('method') == 'execute') from exc
            except Exception as exc:
                self._invalidate(exc)
                raise

    def _rpc(self, method, params=None, *, request_id=None):
        return self._post('/v1/rpc', {'device_id': self.device_id, 'session_id': self.session_id,
            'request_id': request_id or uuid.uuid4().hex, 'method': method, 'params': params or {}})

    def _ensure_session(self, task_id=None):
        if self._heartbeat_error is not None:
            raise RemoteBrowserError('Browser session heartbeat failed; task must stop.', 'session_unavailable')
        if self.session_id is not None:
            return
        response = self._rpc('open_session', {'task_id': task_id,
            'viewport': {'width': self.width, 'height': self.height}, 'start_url': self.browser_start_url,
            'session_timeout_s': self.session_timeout_s, 'idle_timeout_s': self.idle_timeout_s})
        if response.get('device_id') != self.device_id or not response.get('session_id'):
            raise RemoteBrowserError('Executor returned the wrong device/session.', 'device_mismatch')
        self.session_id = response['session_id']
        self.idle_timeout_s = response['idle_timeout_s']
        if self._mirror_spec is not None:
            if isinstance(self._mirror_spec, (str, Path)):
                from ..remote_artifacts import RemoteArtifactMirror
                self._mirror = RemoteArtifactMirror(self._mirror_spec, self.device_id, self.session_id,
                                                     self.read_artifact)
            else:
                self._mirror = self._mirror_spec
        self.initial_state = self._refresh_info({'initial_state': response.get('initial_state', {})})['initial_state']
        if self._download_policy:
            self._rpc('download_policy', self._download_policy)
        self._stop.clear()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name='remote-browser-heartbeat', daemon=True)
        self._heartbeat.start()

    def _heartbeat_loop(self):
        while not self._stop.wait(min(15, max(.2, self.idle_timeout_s / 3))):
            try:
                self._rpc('heartbeat')
            except Exception as exc:
                self._heartbeat_error = exc
                self._invalidate(exc)
                return

    def _refresh_info(self, info):
        copied = copy.deepcopy(info)
        return self._mirror.refresh_info(copied) if self._mirror is not None else copied

    def set_download_policy(self, timeout_s, max_bytes):
        self._download_policy = {'timeout_s': timeout_s, 'max_bytes': max_bytes}
        if self.session_id is not None:
            self._rpc('download_policy', self._download_policy)

    def prepare_workspace(self, task_id=None):
        self._ensure_session(task_id)
        return copy.deepcopy(self.initial_state)

    def begin_episode(self, task_id=None, isolated=True):
        self._ensure_session(task_id)
        return copy.deepcopy(self.initial_state)

    def reset(self, task):
        self._ensure_session()
        return self.observe()

    def observe(self):
        self._ensure_session()
        try:
            value = self._rpc('observe')
            raw = base64.b64decode(value['screenshot_png'], validate=True)
            if not raw or len(raw) > MAX_SCREENSHOT_BYTES:
                raise ValueError('Remote screenshot size is invalid.')
            from PIL import Image
            shot = Image.open(io.BytesIO(raw))
            if shot.format != 'PNG' or shot.width * shot.height > 4096 * 4096:
                raise ValueError('Remote screenshot must be a bounded PNG viewport.')
            shot.load()
            info = self._refresh_info(value['info'])
            remote = info.get('remote_browser') or {}
            if remote.get('device_id') != self.device_id or remote.get('session_id') != self.session_id:
                raise RemoteBrowserError('Observation has wrong remote provenance.', 'device_mismatch')
            observation = Observation(screenshot=shot, text=value.get('text', ''),
                                      context=value.get('context', []), info=info)
            self._last_observation = observation
            self._last_observation_info = copy.deepcopy(info)
            return observation
        except Exception as exc:
            self._last_observation = None
            self._last_observation_info = {}
            self._invalidate(exc)
            raise

    @staticmethod
    def _action_payload(action):
        # Browser bindings are maintained by B, never accepted from A metadata.
        args = {key: value for key, value in action.args.items() if key != 'observation_metadata'}
        value = {'action': action.action, 'args': args}
        checked_action(value)
        return value

    def route_action(self, action, obs):
        self._ensure_session()
        if obs is None:
            return {'channel': 'reobserve', 'reason': 'A fresh browser observation is required.'}
        value = self._rpc('route', {'action': self._action_payload(action),
            'observation_id': obs.info.get('observation_id'), 'exposed_refs': obs.info.get('exposed_refs')})
        return value.get('route')

    def execute_routed(self, action, route):
        return self._execute(action, route)

    def step(self, action):
        return self._execute(action)

    def _execute(self, action, route=None):
        self._ensure_session()
        if self._last_observation is None:
            self.observe()
        value = self._rpc('execute', {'action': self._action_payload(action),
            'observation_id': self._last_observation_info.get('observation_id'),
            'route_token': (route or {}).get('remote_route_token')})
        observation = self.observe()
        info = self._refresh_info(value.get('result', {}))
        info['artifacts'] = observation.info.get('artifacts', [])
        return StepResult(observation, info=info)

    def browser_event_metadata(self):
        self._ensure_session()
        return self._refresh_info(self._rpc('events'))

    def cancel_download(self, download_id):
        self._ensure_session()
        return self._refresh_info(self._rpc('cancel_download', {'download_id': download_id}))

    def read_artifact(self, artifact_id, offset=0, max_bytes=1024 * 1024, snapshot_token=None):
        if self.session_id is None:
            raise RemoteBrowserError('No active browser session.', 'session_expired')
        return self._post('/v1/artifacts/read', {'device_id': self.device_id, 'session_id': self.session_id,
            'artifact_id': artifact_id, 'offset': offset, 'max_bytes': max_bytes, 'snapshot_token': snapshot_token})

    def get_page_source(self, target_ref=None, max_chars=20000):
        self._ensure_session()
        return self._rpc('get_page_source', {'target_ref': target_ref, 'max_chars': max_chars})['text']

    def browser_action(self, action_name, args):
        if action_name == 'get_page_source':
            return self.get_page_source(args.get('target_ref'), args.get('max_chars', 20000))
        raise RemoteBrowserError('Arbitrary browser JavaScript and non-browser actions are disabled.', 'unsupported_action')

    def run_command(self, command):
        raise RemoteBrowserError('Remote browser mode has no terminal or local fallback.', 'unsupported_action')

    def run_python(self, code):
        raise RemoteBrowserError('Remote browser mode has no Python execution or local fallback.', 'unsupported_action')

    def evaluate(self):
        return 0.0

    def close(self):
        self._stop.set()
        try:
            if self.session_id is not None:
                self._rpc('close_session')
        finally:
            self.session_id = None
            if self._heartbeat is not None and threading.current_thread() is not self._heartbeat:
                self._heartbeat.join(timeout=2)
            if self._mirror is not None:
                self._mirror.close()
            if self._own_client:
                self.client.close()
