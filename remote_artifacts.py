"""Explicit B download receipts mirrored on A for existing file verifiers.

Remote paths are display labels only. Bytes are fetched by registered artifact
ID, never by path. Live verifier reads refresh B before trusting the A mirror;
a lost connection or changed/deleted receipt invalidates that artifact for the
remainder of the task, including any already-observed completion baseline.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading
import uuid
import weakref


_REGISTRY = {}
_REGISTRY_LOCK = threading.RLock()
_CHUNK = 1024 * 1024


class RemoteArtifactError(ValueError):
    pass


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _remote_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RemoteArtifactError('Remote artifact path is missing or invalid.')
    path = PureWindowsPath(value) if '\\' in value or re.match(r'^[A-Za-z]:', value) else PurePosixPath(value)
    if not path.is_absolute():
        raise RemoteArtifactError('Remote artifact path must be an absolute delivery label.')
    return path


def ensure_remote_current(path):
    """Recheck registered mirrors; ordinary local files need no remote check."""
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(_key(path))
    if entry is None:
        return True
    reference, ident = entry
    mirror = reference()
    return mirror is not None and mirror.ensure_current(ident, expected_path=path)


def remote_provenance(path):
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(_key(path))
    mirror = entry[0]() if entry is not None else None
    return mirror.provenance(entry[1]) if mirror is not None else {}


class RemoteArtifactMirror:
    def __init__(self, result_dir, device_id, session_id, read_artifact, *, max_bytes=256 * 1024 * 1024):
        self.root = (Path(result_dir).resolve() / 'remote_artifacts').resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.device_id, self.session_id = str(device_id), str(session_id)
        self.read_artifact = read_artifact
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ValueError('Remote mirror max_bytes must be positive.')
        self._records = {}
        self._remote_workspace = None
        self._lock = threading.RLock()
        self._closed = False
        self._invalidated_reason = None

    def _local_path(self, remote):
        remote = _remote_path(remote)
        name = remote.name
        if (not name or name in {'.', '..'} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or name.endswith((' ', '.')) or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name)):
            raise RemoteArtifactError('Remote filename cannot be safely represented in the mirror.')
        parent = str(remote.parent)
        folder = ('workspace' if self._remote_workspace is not None
                  and _remote_path(self._remote_workspace) == remote.parent else
                  'other_' + hashlib.sha256(parent.encode()).hexdigest()[:16])
        path = self.root / folder / name
        path.resolve().relative_to(self.root)
        if path.is_symlink() or path.parent.is_symlink():
            raise RemoteArtifactError('Mirror symlinks are not allowed.')
        return path

    def _register_path(self, path, ident):
        with _REGISTRY_LOCK:
            _REGISTRY[_key(path)] = (weakref.ref(self), ident)

    def _validate_event(self, event):
        if not isinstance(event, dict):
            raise RemoteArtifactError('Remote artifact metadata must be an object.')
        ident = event.get('artifact_id') or event.get('download_id')
        if not isinstance(ident, str) or not ident or len(ident) > 256:
            raise RemoteArtifactError('Remote artifact requires a registered ID.')
        if event.get('device_id') != self.device_id or event.get('session_id') != self.session_id:
            raise RemoteArtifactError('Remote artifact belongs to a different device or session.')
        if (event.get('completed') is not True or event.get('transport') != 'browser'
                or event.get('status') not in {'completed', 'executed', 'already_downloaded'}):
            raise RemoteArtifactError('Remote artifact has no completed browser receipt.')
        digest = event.get('sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RemoteArtifactError('Remote artifact requires a SHA-256 baseline.')
        if type(event.get('bytes')) is not int or not 0 < event['bytes'] <= self.max_bytes:
            raise RemoteArtifactError('Remote artifact exceeds the mirror byte limit or is empty.')
        if event.get('initial_sha256', digest) != digest:
            raise RemoteArtifactError('Remote artifact no longer matches its initial digest.')
        identity = event.get('remote_file_identity')
        if (not isinstance(identity, (list, tuple)) or len(identity) != 2
                or any(type(value) is not int for value in identity)):
            raise RemoteArtifactError('Remote artifact is missing its browser file identity.')
        version, history = event.get('version', 1), event.get('rename_history', [])
        if type(version) is not int or not 1 <= version <= 1000 or not isinstance(history, list) or len(history) != version - 1:
            raise RemoteArtifactError('Remote artifact rename history is invalid.')
        current = _remote_path(event.get('remote_path'))
        previous = None
        for number, change in enumerate(history, 1):
            if not isinstance(change, dict):
                raise RemoteArtifactError('Remote rename record is invalid.')
            before = _remote_path(change.get('remote_from_path', change.get('from_path')))
            after = _remote_path(change.get('remote_to_path', change.get('to_path')))
            if (change.get('from_version') != number or change.get('to_version') != number + 1
                    or change.get('sha256') != digest or before.parent != after.parent
                    or (previous is not None and before != previous)):
                raise RemoteArtifactError('Remote rename chain is inconsistent.')
            previous = after
        if previous is not None and previous != current:
            raise RemoteArtifactError('Remote rename history does not reach the current path.')
        return ident

    def _invalidate(self, ident, reason):
        record = self._records.get(ident)
        if record is None:
            return
        record['invalid_reason'] = str(reason)
        path = record.get('local_path')
        if path:
            target = Path(path)
            target.resolve().relative_to(self.root)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass  # Live verifier hooks still fail closed on the invalid flag.

    def invalidate_all(self, reason='Remote executor became unavailable.'):
        # A heartbeat may hold the HTTP-client lock while a verifier holds this
        # mirror lock and waits for HTTP. Set the revocation flag immediately;
        # never wait for the mirror lock in that inverse acquisition order.
        self._invalidated_reason = str(reason)
        if not self._lock.acquire(blocking=False):
            return
        try:
            for ident in list(self._records):
                self._invalidate(ident, reason)
        finally:
            self._lock.release()

    def _fetch(self, event):
        """Read one immutable snapshot, verifying offsets, limits and its hash."""
        ident = event['artifact_id']
        temporary = self.root / ('.transfer_' + uuid.uuid4().hex)
        token, offset, digest = None, 0, hashlib.sha256()
        latest = event
        try:
            with temporary.open('xb') as handle:
                while True:
                    reply = self.read_artifact(artifact_id=ident, offset=offset, max_bytes=_CHUNK,
                                               snapshot_token=token)
                    if not isinstance(reply, dict) or reply.get('ok') is False:
                        raise RemoteArtifactError('Remote artifact read failed.')
                    current = reply.get('artifact', reply.get('metadata'))
                    if isinstance(current, dict):
                        if self._validate_event(current) != ident:
                            raise RemoteArtifactError('Remote artifact ID changed during transfer.')
                        latest = current
                    snapshot = reply.get('snapshot_token')
                    if not isinstance(snapshot, str) or not snapshot or (token is not None and token != snapshot):
                        raise RemoteArtifactError('Remote artifact snapshot changed during transfer.')
                    token = snapshot
                    if (reply.get('offset') != offset or type(reply.get('size')) is not int
                            or reply['size'] != event['bytes'] or reply.get('sha256') != event['sha256']
                            or latest.get('sha256') != event['sha256']
                            or latest.get('remote_file_identity') != event.get('remote_file_identity')):
                        raise RemoteArtifactError('Remote artifact changed from its browser receipt.')
                    data = base64.b64decode(reply.get('data_base64', ''), validate=True)
                    if len(data) > _CHUNK or offset + len(data) > event['bytes']:
                        raise RemoteArtifactError('Remote artifact chunk exceeded its declared bounds.')
                    handle.write(data)
                    digest.update(data)
                    offset += len(data)
                    if reply.get('eof') is True:
                        if offset != event['bytes']:
                            raise RemoteArtifactError('Remote artifact was truncated during transfer.')
                        break
                    if not data or offset >= event['bytes']:
                        raise RemoteArtifactError('Remote artifact transfer made no valid progress.')
            if digest.hexdigest() != event['sha256']:
                raise RemoteArtifactError('Remote artifact bytes do not match the receipt hash.')
            return temporary, latest
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _refresh(self, ident):
        record = self._records[ident]
        if self._invalidated_reason:
            self._invalidate(ident, self._invalidated_reason)
        if record.get('invalid_reason') or self._closed:
            return False
        temporary = None
        try:
            event = record['event']
            temporary, current = self._fetch(event)
            if self._invalidated_reason:
                raise RemoteArtifactError(self._invalidated_reason)
            old_version, version = event.get('version', 1), current.get('version', 1)
            if (version < old_version or current.get('bytes') != event['bytes']
                    or current.get('rename_history', [])[:old_version - 1] != event.get('rename_history', [])):
                raise RemoteArtifactError('Remote artifact version moved backwards or replaced its history.')
            if version > old_version:
                migration = current['rename_history'][old_version - 1]
                start = migration.get('remote_from_path', migration.get('from_path'))
                if _remote_path(start) != _remote_path(event['remote_path']):
                    raise RemoteArtifactError('Remote rename chain does not continue the observed artifact.')
            target = self._local_path(current['remote_path'])
            old_path = Path(record['local_path']) if record.get('local_path') else None
            if old_path is not None:
                if not old_path.is_file() or old_path.is_symlink():
                    raise RemoteArtifactError('A mirror file was removed or replaced.')
                old_stat = old_path.stat()
                local_hash = hashlib.sha256()
                with old_path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(_CHUNK), b''):
                        local_hash.update(chunk)
                local_digest = local_hash.hexdigest()
                if (local_digest != event['sha256'] or [old_stat.st_dev, old_stat.st_ino] != record['local_identity']):
                    raise RemoteArtifactError('A mirror file changed since the verified transfer.')
                if target != old_path:
                    if version <= old_version or target.exists():
                        raise RemoteArtifactError('Remote rename would overwrite a mirror baseline.')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    old_path.rename(target)  # Retain inode for the verifier rename chain.
            else:
                if target.exists():
                    raise RemoteArtifactError('Remote artifact collides with an existing mirror file.')
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary.rename(target)
                temporary = None
            local_stat = target.stat()
            record.update(event=copy.deepcopy(current), local_path=str(target),
                          local_identity=[local_stat.st_dev, local_stat.st_ino])
            self._register_path(target, ident)
            return True
        except Exception as exc:
            self._invalidate(ident, type(exc).__name__ + ': ' + str(exc))
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def ensure_current(self, ident, expected_path=None):
        with self._lock:
            record = self._records.get(ident)
            if record is None or not self._refresh(ident):
                return False
            return expected_path is None or _key(record['local_path']) == _key(expected_path)

    def provenance(self, ident):
        record = self._records.get(ident)
        if not record or not record.get('local_path'):
            return {}
        event = record['event']
        return {'device_id': self.device_id, 'session_id': self.session_id,
                'remote_path': event['remote_path'], 'local_mirror_path': record['local_path'],
                'path_scope': 'controller_evidence_mirror', 'delivery_location': 'remote_browser'}

    def _view(self, ident):
        record = self._records[ident]
        event = copy.deepcopy(record['event'])
        event.pop('path', None)
        if record.get('invalid_reason'):
            event.update(status='unavailable', completed=False, mirror_error=record['invalid_reason'])
            return event
        event.update(self.provenance(ident))
        event.update(path=record['local_path'], file_identity=record['local_identity'])
        for change in event.get('rename_history', []):
            before = change.get('remote_from_path', change.get('from_path'))
            after = change.get('remote_to_path', change.get('to_path'))
            change.update(remote_from_path=before, remote_to_path=after,
                          from_path=str(self._local_path(before)), to_path=str(self._local_path(after)))
        return event

    def refresh_info(self, info):
        """Return adapter info with live, local verifier receipts and provenance."""
        if not isinstance(info, dict):
            raise RemoteArtifactError('Remote observation info must be an object.')
        with self._lock:
            result = copy.deepcopy(info)
            initial = result.get('initial_state')
            if initial is None and 'remote_workspace_dir' in result:
                initial = result  # prepare_workspace/begin_episode response.
            if isinstance(initial, dict):
                remote = initial.get('remote_workspace_dir')
                if isinstance(remote, str):
                    if self._remote_workspace is not None and _remote_path(remote) != _remote_path(self._remote_workspace):
                        self.invalidate_all('Remote workspace unexpectedly changed.')
                    self._remote_workspace = remote
                    (self.root / 'workspace').mkdir(exist_ok=True)
                    initial.update(workspace_dir=str(self.root / 'workspace'),
                                   workspace_location='controller_evidence_mirror', delivery_location='remote_browser')
            events = result.get('artifacts', [])
            if not isinstance(events, list) or len(events) > 1000:
                self.invalidate_all('Remote artifact list is invalid or exceeds the task limit.')
                raise RemoteArtifactError('Remote artifact list is invalid or too large.')
            for event in events:
                try:
                    ident = self._validate_event(event)
                    event = {**event, 'artifact_id': ident}
                    record = self._records.get(ident)
                    if record is None:
                        if len(self._records) >= 1000:
                            raise RemoteArtifactError('Remote task artifact limit exceeded.')
                        self._records[ident] = {'event': copy.deepcopy(event)}
                    elif not record.get('invalid_reason'):
                        old = record['event']
                        if event.get('version', 1) >= old.get('version', 1):
                            if event['sha256'] != old['sha256'] or event.get('remote_file_identity') != old.get('remote_file_identity'):
                                self._invalidate(ident, 'Remote browser receipt baseline changed.')
                            # _refresh obtains current metadata; do not overwrite
                            # the old rename baseline before checking its chain.
                except (ValueError, TypeError):
                    ident = event.get('artifact_id') if isinstance(event, dict) else None
                    if ident in self._records:
                        self._invalidate(ident, 'Remote artifact receipt became invalid.')
            for ident in self._records:
                self._refresh(ident)
            result['artifacts'] = [self._view(ident) for ident in self._records]
            # Legacy immediate-download responses must use the same mirror.
            ident = result.get('artifact_id') or result.get('download_id')
            if ident in self._records:
                view = self._view(ident)
                result.update({key: view[key] for key in ('path', 'remote_path', 'local_mirror_path',
                              'path_scope', 'delivery_location', 'file_identity') if key in view})
                if view.get('completed') is not True:
                    result.update(status='unavailable', completed=False, mirror_error=view.get('mirror_error'))
            return result

    def close(self):
        """Keep evidence copies for inspection, detach live validation hooks."""
        with self._lock:
            self._closed = True
            with _REGISTRY_LOCK:
                for key, (reference, _) in list(_REGISTRY.items()):
                    if reference() is self:
                        _REGISTRY.pop(key, None)
