import fcntl
import json
import time
from pathlib import Path
from warden.media_guard import protection
from warden.media_guard.core import Store


def rig(tmp_path, monkeypatch):
    enabled=tmp_path/'enabled';enabled.touch()
    db=tmp_path/'guard.sqlite3';Store(db)
    (tmp_path/'health.json').write_text(json.dumps({'checked_at':time.time()}))
    monkeypatch.setattr(protection,'ENABLED',enabled)
    monkeypatch.setattr(protection,'DATABASE',db)
    return db


def test_database_failure_skips_cleanup(tmp_path,monkeypatch):
    db=rig(tmp_path,monkeypatch);db.unlink()
    called=[]
    protection.guarded_cleanup(lambda:called.append(True))()
    assert not called


def test_stale_worker_skips_cleanup(tmp_path,monkeypatch):
    rig(tmp_path,monkeypatch)
    (tmp_path/'health.json').write_text(json.dumps({'checked_at':time.time()-180}))
    assert not protection.cleanup_allowed()


def test_scan_lock_excludes_cleanup(tmp_path,monkeypatch):
    rig(tmp_path,monkeypatch);called=[]
    with open(tmp_path/'worker.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        protection.guarded_cleanup(lambda:called.append(True))()
    assert not called
    protection.guarded_cleanup(lambda:called.append(True))()
    assert called==[True]


def test_pending_job_protected(tmp_path,monkeypatch):
    db=rig(tmp_path,monkeypatch);store=Store(db)
    store.add('a'*40,'Film','movies')
    assert not protection.cleanup_allowed()
