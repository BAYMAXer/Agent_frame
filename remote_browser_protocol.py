"""Wire limits and action boundaries for the browser-only executor on device B."""
from __future__ import annotations

import hashlib
import json
import math
from urllib.parse import urlsplit

PROTOCOL_VERSION = 'yaero-browser-v1'
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_CHUNK = 1024 * 1024
MAX_SCREENSHOT_BYTES = 12 * 1024 * 1024
ALLOWED_ACTIONS = frozenset({'click', 'double_click', 'right_click', 'type', 'select',
    'drag_and_drop', 'scroll', 'press', 'hotkey', 'goto', 'wait', 'observe_browser',
    'handle_dialog', 'list_tabs', 'switch_tab', 'close_tab', 'get_page_source'})
ACTION_FIELDS = frozenset({'target', 'target_ref', 'element_id', 'text', 'overwrite',
    'option', 'direction', 'amount', 'key', 'keys', 'url', 'seconds', 'duration',
    'accept', 'prompt_text', 'dialog_id', 'tab_id', 'max_chars', 'x', 'y', 'x1', 'y1',
    'x2', 'y2', 'coordinate_space', 'observation_metadata', 'grounding_failed',
    'submit_key', 'from_target', 'to_target', 'start_target', 'end_target'})


class RemoteBrowserError(RuntimeError):
    def __init__(self, message, code='remote_browser_error', *, dispatched=False):
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def http_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise RemoteBrowserError('Expected an HTTP(S) URL.', 'invalid_url')
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise RemoteBrowserError('Only HTTP(S) browser navigation is supported.', 'invalid_url')
    return value


def positive_number(value, name, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise RemoteBrowserError(f'{name} must be between 0 and {maximum}.', 'invalid_argument')
    return float(value)


def checked_action(value):
    from .actions import Action
    if not isinstance(value, dict) or set(value) != {'action', 'args'}:
        raise RemoteBrowserError('Expected action and args.', 'invalid_action')
    name, args = value['action'], value['args']
    if not isinstance(name, str) or name not in ALLOWED_ACTIONS:
        raise RemoteBrowserError('This executor supports browser actions only.', 'unsupported_action')
    if not isinstance(args, dict) or set(args) - ACTION_FIELDS:
        raise RemoteBrowserError('Unsupported browser action arguments.', 'invalid_action')
    if args.get('coordinate_space') not in (None, 'css_viewport'):
        raise RemoteBrowserError('Only CSS viewport coordinates are accepted.', 'invalid_coordinates')
    if name == 'goto':
        http_url(args.get('url'))
    if name in {'press', 'hotkey'} or args.get('submit_key'):
        keys = ([args.get('key')] if name == 'press' else args.get('keys', []))
        if args.get('submit_key'):
            keys = [args['submit_key']]
        if not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys):
            raise RemoteBrowserError('Invalid browser keys.', 'invalid_action')
        normalized = [k.lower().replace('controlormeta', 'control').replace('ctrl', 'control') for k in keys]
        plain = {'enter', 'tab', 'escape', 'esc', 'backspace', 'delete', 'arrowleft', 'arrowright',
                 'arrowup', 'arrowdown', 'left', 'right', 'up', 'down', 'home', 'end', 'pageup', 'pagedown', 'space'}
        allowed = len(normalized) == 1 and normalized[0] in plain
        allowed |= len(normalized) == 2 and normalized[0] == 'control' and normalized[1] in {'a', 'z', 'y', 'backspace', 'arrowleft', 'arrowright'}
        allowed |= len(normalized) == 2 and normalized[0] == 'shift' and normalized[1] in plain
        if not allowed:
            raise RemoteBrowserError('Browser chrome, developer tools and system shortcuts are disabled.', 'unsupported_key')
    if name == 'wait':
        duration = args.get('seconds', args.get('duration', 1))
        positive_number(duration, 'wait duration', 5)
    if name == 'scroll':
        if args.get('direction', 'down') not in {'up', 'down'}:
            raise RemoteBrowserError('Unsupported scroll direction.', 'invalid_action')
        positive_number(args.get('amount', 1), 'scroll amount', 100)
    if name == 'handle_dialog' and type(args.get('accept')) is not bool:
        raise RemoteBrowserError('accept must be boolean.', 'invalid_action')
    fingerprint(value)  # Reject non-JSON values and nonfinite coordinates.
    return Action(name, dict(args))
