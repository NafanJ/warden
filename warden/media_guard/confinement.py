"""Landlock confines the controller without bind mounts that break atomic renames.

Payload parsers additionally run inside networkless bubblewrap namespaces.
Require Linux Landlock ABI >= 3; unsupported hosts fail closed.
"""
import ctypes
import os
from pathlib import Path


def confine(cfg):
    libc = ctypes.CDLL(None, use_errno=True)
    create, add, restrict = 444, 445, 446  # Linux x86_64 syscall numbers
    abi = libc.syscall(create, 0, 0, 1)
    if abi < 3:
        raise RuntimeError('Landlock ABI 3 or newer required')
    # Through TRUNCATE; intentionally exclude device IOCTL on newer kernels.
    handled = (1 << 15) - 1
    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs', ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]
    attr = Ruleset(handled)
    fd = libc.syscall(create, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'Landlock create failed')
    read = (1 << 0) | (1 << 2) | (1 << 3)
    def allow(path, access):
        p = Path(path)
        if not p.exists():
            return
        if not p.is_dir():
            access &= ~((1 << 3) | sum(1 << x for x in range(4, 14)))
        pfd = os.open(p, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule(access, pfd)
            if libc.syscall(add, fd, 1, ctypes.byref(rule), 0):
                raise OSError(ctypes.get_errno(), 'Landlock rule failed')
        finally:
            os.close(pfd)
    try:
        for p in ['/usr', '/etc/ld.so.cache', '/etc/hosts', '/etc/resolv.conf', '/etc/nsswitch.conf',
                  '/etc/localtime', '/opt/warden/warden', cfg['runtime'], cfg['signatures']]:
            allow(p, read)
        allow('/proc', read)
        for p in ['/dev/null', '/dev/urandom', '/dev/random']:
            allow(p, (1 << 1) | (1 << 2))
        for key in ['state_dir', 'incoming', 'stage', 'approved']:
            allow(cfg[key], handled)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(restrict, fd, 0):
            raise OSError(ctypes.get_errno(), 'Landlock enforcement failed')
    finally:
        os.close(fd)
