"""Target-side CLI worker. No GUI input, host fallback, or success-by-stdout.

The worker uses stdlib only so the identical implementation can execute through
an OSWorld controller inside its guest. Shell commands are NOT a security sandbox.

Integration contract:
- A local adapter opts in with cli_target='local' and a prepared workspace.
- A guest adapter supplies cli_target='guest', guest_workspace_dir, and an
  explicit controller.run_python_script port. Transport failure never runs on
  the controller host; unknown dispatch is reported without automatic retry.
- Pipeline records the action, result, target, cwd, timing and review evidence;
  the sampled CLI action uses the same SFT/RL transition schema as DOM/GUI.
- File predicates are independently read on the target. Exit code zero is not
  a completion contract. Jev reviews remaining semantics using these facts.
- Windows shells use cmd.exe; Linux/macOS use bash. Relative paths use the run
  workspace unless cwd is explicit. Shell commands run with the runner's OS
  permissions. The workspace is a default location, not an access sandbox.
- stdout/stderr each expose at most 16 KB; directory listings expose 300 entries.
  Text reads are bounded, full-file equality uses streamed SHA-256 comparison.
- The native CLI-only runner supports Linux without a desktop/display server;
  Windows/macOS adapters also expose CLI alongside their existing UI modes.
"""
import base64
import inspect
import json

OPERATIONS = {'list', 'stat', 'read', 'write', 'mkdir', 'copy', 'move', 'rename', 'open', 'shell', 'compare'}


def validate_cli(args):
    if not isinstance(args, dict) or args.get('operation') not in OPERATIONS:
        return 'cli.operation 必须为 list/stat/read/write/mkdir/copy/move/rename/open/shell/compare'
    allowed = {'operation', 'path', 'destination', 'content', 'command', 'cwd', 'timeout_s', 'overwrite'}
    if set(args) - allowed:
        return '未知 CLI 参数：' + ', '.join(sorted(set(args) - allowed))
    required = {'shell': ['command'], 'write': ['path', 'content'],
                'compare': ['path', 'destination'], 'copy': ['path', 'destination'], 'move': ['path', 'destination'],
                'rename': ['path', 'destination']}.get(args['operation'], ['path'])
    for key in required:
        if not isinstance(args.get(key), str) or (key != 'content' and not args[key].strip()):
            return 'cli.' + key + ' 必须为字符串'
    for key in ('path', 'destination', 'cwd', 'command'):
        if key in args and (not isinstance(args[key], str) or '\x00' in args[key] or len(args[key]) > 16000):
            return '无效 CLI ' + key
    if len(args.get('content', '')) > 200000:
        return '单次写入不能超过 200000 字符'
    if 'overwrite' in args and type(args['overwrite']) is not bool:
        return 'overwrite 必须为布尔值'
    timeout = args.get('timeout_s', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        return 'timeout_s 必须在 (0,120] 秒内'
    return None


def target_worker(args, workspace):
    # Self-contained: serialized by execute_remote, must have no module globals.
    import hashlib
    import os
    import shutil
    import signal
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    started = time.monotonic()
    result = {'channel': 'cli', 'operation': args['operation'], 'status': 'failed',
              'dispatched': False, 'stdout': '', 'stderr': '', 'exit_code': None}
    try:
        root = Path(workspace).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        cwd = Path(args.get('cwd') or root).expanduser()
        cwd = (root / cwd).resolve(strict=True) if not cwd.is_absolute() else cwd.resolve(strict=True)
        if not cwd.is_dir():
            raise NotADirectoryError(str(cwd))
        result['cwd'] = str(cwd)
        def path(value):
            p = Path(value).expanduser()
            return (cwd / p).absolute() if not p.is_absolute() else p.absolute()
        def stat(p):
            exists = p.exists()
            item = {'path': str(p), 'exists': exists, 'is_file': p.is_file(), 'is_dir': p.is_dir()}
            if exists:
                s = p.stat()
                item.update(bytes=s.st_size, mtime_ns=s.st_mtime_ns)
            return item
        op = args['operation']
        result['dispatched'] = True
        if op == 'shell':
            command = args['command']
            shell = ['cmd.exe', '/d', '/s', '/c', command] if os.name == 'nt' else ['/bin/bash', '-c', command]
            result['command'] = command
            # Spool output, never buffer unbounded stdout in RAM or wait on
            # inherited pipes from a child launched by the command.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                proc = subprocess.Popen(shell, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, start_new_session=os.name != 'nt',
                                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
                try:
                    proc.wait(timeout=args.get('timeout_s', 30))
                except subprocess.TimeoutExpired:
                    result['error_code'] = 'cli_timeout'
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    else:
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                except BaseException:
                    # User stop / runner termination must not leave a command
                    # behind just because it owns a separate process group.
                    if proc.poll() is None:
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                    raise
                result['exit_code'] = proc.returncode
                for key, stream in [('stdout', out), ('stderr', err)]:
                    size = stream.tell()
                    stream.seek(0)
                    result[key] = stream.read(16000).decode('utf-8', errors='replace')
                    result[key + '_truncated'] = size > 16000
            result['status'] = 'executed' if result['exit_code'] == 0 and not result.get('error_code') else 'failed'
        else:
            p = path(args['path'])
            result['path'] = str(p)
            if op == 'stat':
                result['file'] = stat(p)
            elif op == 'list':
                entries = []
                with os.scandir(p) as scan:
                    for entry in scan:
                        if len(entries) >= 300:
                            result['truncated'] = True
                            break
                        entries.append(stat(Path(entry.path)))
                result['entries'] = sorted(entries, key=lambda e: e['path'])
            elif op == 'read':
                with p.open('rb') as f:
                    data = f.read(16001)
                result.update(stdout=data[:16000].decode('utf-8', errors='replace'), stdout_truncated=len(data)>16000,
                              file=stat(p))
            elif op == 'write':
                data = args['content'].encode('utf-8')
                # Exclusive create by default; replacement must be explicit.
                with p.open('wb' if args.get('overwrite') else 'xb') as f:
                    f.write(data)
                result['file'] = stat(p)
                readback = p.read_bytes()
                result['effect_verified'] = readback == data
                result['sha256'] = hashlib.sha256(data).hexdigest()
                result['content_preview'] = readback[:16000].decode('utf-8', errors='replace')
                result['content_truncated'] = len(readback) > 16000
            elif op == 'mkdir':
                p.mkdir(parents=True, exist_ok=True)
                result['file'] = stat(p)
                result['effect_verified'] = p.is_dir()
            elif op in {'copy', 'move', 'rename', 'compare'}:
                dest = path(args['destination'])
                result['destination'] = str(dest)
                if op != 'compare' and p.resolve() == dest.resolve():
                    raise ValueError('source and destination are the same')
                if not p.is_file():
                    raise ValueError('copy/move/rename requires a regular file')
                def digest(file):
                    h = hashlib.sha256()
                    with file.open('rb') as f:
                        for block in iter(lambda: f.read(1048576), b''):
                            h.update(block)
                    return h.hexdigest()
                original_hash = digest(p)
                if op == 'compare':
                    other_hash = digest(dest)
                    result.update(equal=original_hash == other_hash, file=stat(p), other_file=stat(dest), sha256=original_hash, other_sha256=other_hash)
                elif op in {'move', 'rename'}:
                    if args.get('overwrite'):
                        os.replace(p, dest)
                    else:
                        # Atomic exclusive destination; keeps inode/file ID so
                        # browser download receipts survive the rename.
                        try:
                            os.link(p, dest)
                        except OSError as exc:
                            import errno
                            if exc.errno not in {errno.EXDEV, errno.ENOTSUP}:
                                raise
                            with p.open('rb') as source, dest.open('xb') as target:
                                shutil.copyfileobj(source, target)
                            if digest(dest) != original_hash:
                                raise OSError('destination content mismatch; source retained')
                        p.unlink()
                else:
                    with p.open('rb') as source, dest.open('wb' if args.get('overwrite') else 'xb') as target:
                        shutil.copyfileobj(source, target)
                if op != 'compare':
                    if digest(dest) != original_hash:
                        raise OSError('destination content mismatch')
                    result.update(file=stat(dest), source=stat(p), sha256=original_hash, effect_verified=True)
            elif op == 'open':
                if not p.exists():
                    raise FileNotFoundError(str(p))
                if os.name == 'nt':
                    os.startfile(str(p))
                else:
                    import sys
                    subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(p)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                result['effect_verified'] = False  # Dispatch is not app readiness.
            result.update(status='executed', exit_code=0)
    except Exception as exc:
        result.update(status='failed', error_code=type(exc).__name__, stderr=str(exc)[:2000])
    result['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
    result['reason'] = ('后台操作已返回；子任务完成由独立核查决定' if result['status'] == 'executed'
                        else result.get('error_code') or '命令退出码非零')
    return result


def execute_cli(env, args, remaining_s=120):
    error = validate_cli(args)
    capability = cli_capability(env)
    if error or not capability['available'] or remaining_s <= 0:
        return {'channel': 'cli', 'status': 'rejected', 'dispatched': False,
                'reason': error or capability.get('reason') or '任务时限已耗尽'}
    args = dict(args, timeout_s=min(float(args.get('timeout_s', 30)), max(.001, remaining_s)))
    if capability['target'] == 'local':
        return target_worker(args, capability['workspace'])
    # Explicit guest Python port. NEVER call run_python/run_command wrappers:
    # their legacy host fallback can operate on the wrong computer.
    payload = base64.b64encode(json.dumps([args, capability['workspace']], ensure_ascii=False).encode()).decode()
    source = inspect.getsource(target_worker)
    code = source + '\nimport json,base64\nprint("AGENT_CLI_RESULT="+json.dumps(target_worker(*json.loads(base64.b64decode(' + repr(payload) + '))),ensure_ascii=False))'
    try:
        response = env.controller.run_python_script(code)
        output = response if isinstance(response, str) else response.get('output', '')
        line = next(line for line in reversed(output.splitlines()) if line.startswith('AGENT_CLI_RESULT='))
        result = json.loads(line.partition('=')[2])
        if result.get('channel') != 'cli':
            raise ValueError('invalid guest result')
        return result
    except Exception as exc:
        return {'channel': 'cli', 'status': 'uncertain', 'dispatched': True,
                'reason': '目标机 CLI 结果不可确认：' + type(exc).__name__}


def cli_capability(env):
    initial = getattr(env, 'initial_state', None) or {}
    workspace = initial.get('workspace_dir') or initial.get('downloads_dir') or getattr(env, 'workspace_dir', None)
    local = getattr(env, 'cli_target', None) == 'local'
    guest = getattr(env, 'cli_target', None) == 'guest'
    if guest:
        workspace = getattr(env, 'guest_workspace_dir', None) or initial.get('target_workspace_dir')
        # Never interpret a controller-side download path as a guest path.
    available = bool(workspace and (local or guest and callable(getattr(getattr(env, 'controller', None), 'run_python_script', None))))
    return {'available': available, 'target': 'local' if local else 'guest', 'workspace': str(workspace or ''),
            'os': getattr(env, 'os_name', ''), 'cli_only': bool(getattr(env, 'cli_only', False)), 'reason': '' if available else '目标环境没有可用 CLI 执行器或默认工作目录'}


def file_check_samples(env, predicates, remaining):
    samples = []
    for p in predicates:
        if p.get('kind') not in {'file_exists', 'file_absent', 'file_content', 'files_equal'}:
            continue
        args = {'operation': 'read' if p['kind'] == 'file_content' else 'stat', 'path': p['path']}
        if p['kind'] == 'files_equal':
            args.update(operation='compare', destination=p['other_path'])
        samples.append({'predicate': dict(p), 'result': execute_cli(env, args, remaining_s=remaining())})
    return samples
