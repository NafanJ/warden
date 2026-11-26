"""Private pre-confinement broker: Landlock forbids the mount syscall itself.

A socketpair exposes only read-only, networkless inspection sandboxes. No shell,
write mounts, credentials or arbitrary executables can be requested. The broker
exits on controller disconnect and is killed with its systemd control group.
"""
import json
import os
from pathlib import Path
import socket
import subprocess


class Broker:
    def __init__(self, sock):
        self.io = sock.makefile('rwb', buffering=0)

    def run(self, mounts, command, timeout):
        self.io.write((json.dumps({'mounts': [(str(a), b) for a,b in mounts], 'command': command, 'timeout': timeout}) + '\n').encode())
        line = self.io.readline(2_100_000)
        if not line or not line.endswith(b'\n'):
            raise RuntimeError('Inspection broker disconnected or exceeded limit')
        result = json.loads(line)
        if 'error' in result:
            raise RuntimeError('Inspection sandbox failed: ' + result['error'])
        return subprocess.CompletedProcess(command, result['code'], result['stdout'], result['stderr'])


def start(cfg):
    from . import core
    parent, child = socket.socketpair()
    pid = os.fork()
    if pid:
        child.close()
        core._BROKER = Broker(parent)
        return
    parent.close()
    io = child.makefile('rwb', buffering=0)
    try:
        while True:
            line = io.readline(65536)
            if not line:
                break
            try:
                if not line.endswith(b'\n'):
                    raise ValueError('request too long')
                r = json.loads(line)
                command = r['command']
                if command[0] not in ['/usr/bin/ffprobe', '/usr/bin/file', '/runtime/usr/bin/clamscan']:
                    raise ValueError('executable denied')
                mounts = r['mounts']
                if len(mounts) > 3:
                    raise ValueError('too many mounts')
                for source, target in mounts:
                    p = Path(source).resolve(strict=True)
                    if target in ['/payload', '/input']:
                        if not p.is_relative_to(Path(cfg['stage']).resolve()):
                            raise ValueError('payload outside private stage')
                    elif target == '/signatures':
                        if p != Path(cfg['signatures']).resolve():
                            raise ValueError('unexpected signatures')
                    elif target == '/scan-files':
                        if p != Path(cfg['state_dir']).resolve() / 'scan-files.txt':
                            raise ValueError('unexpected file list')
                    else:
                        raise ValueError('unexpected mount target')
                result = core.sandbox(cfg, mounts, command, min(int(r['timeout']), 1800))
                reply = {'code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
            except Exception as exc:
                reply = {'error': type(exc).__name__}
            io.write((json.dumps(reply) + '\n').encode())
    finally:
        os._exit(0)
