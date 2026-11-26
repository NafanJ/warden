"""Read-only cleanup gate. Failure means no destructive cleanup this cycle."""
import sqlite3
import json
import time
from pathlib import Path

DATABASE = Path('/opt/media-server/security/state/guard.sqlite3')
ENABLED = Path('/opt/media-server/security/enabled')


def protected_hashes():
    if not ENABLED.exists():
        return set()
    try:
        health = json.loads((DATABASE.parent / 'health.json').read_text())
        if not 0 <= time.time() - health['checked_at'] <= 120:
            return None
        with sqlite3.connect(f'file:{DATABASE}?mode=ro', uri=True, timeout=5) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                return None
            return {r[0].upper() for r in db.execute("SELECT hash FROM jobs WHERE state != 'released'")}
    except Exception:
        return None


def cleanup_allowed():
    """Legacy orphan sweeper skips during any unfinished publication/held job."""
    hashes = protected_hashes()
    return hashes is not None and not hashes


def guarded_cleanup(function):
    """Serialize the entire destructive operation with adoption/publication."""
    from functools import wraps
    import fcntl
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not ENABLED.exists():
            return function(*args, **kwargs)
        try:
            lock = open(DATABASE.parent / 'worker.lock', 'a')
        except OSError:
            return []
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return []
            if not cleanup_allowed():
                return []
            return function(*args, **kwargs)
    return wrapped
