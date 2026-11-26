import json
from pathlib import Path
import subprocess
import time
import pytest
from warden.media_guard import core


@pytest.fixture
def setup(tmp_path, monkeypatch):
    p = tmp_path / 'Film'; p.mkdir()
    video = p / 'film.mp4'; video.write_bytes(b'test')
    cfg = {'state_dir': str(tmp_path), 'signatures': str(tmp_path)}
    (tmp_path / 'fresh.json').write_text(json.dumps({'checked_at': time.time(), 'database_at': time.time(), 'engine': 'test'}))
    t = {'name': 'Film', 'files': [{'name': 'Film/film.mp4', 'length': 4}]}
    calls = []
    def run(cfg, mounts, command, timeout=120):
        calls.append(command)
        output = json.dumps({'format': {'format_name':'mov,mp4,m4a,3gp,3g2,mj2', 'duration':'10'},
                             'streams':[{'codec_type':'video','codec_name':'h264'}]}) if 'ffprobe' in command[0] else 'Scanned files: 1\n'
        return subprocess.CompletedProcess(command, 0, output, '')
    monkeypatch.setattr(core, 'sandbox', run)
    return cfg, p, t, calls


def test_complete_scan(setup):
    cfg,p,t,calls=setup
    r=core.inspect(cfg,p,t)
    assert r['verdict']=='media validated; antivirus scan passed'
    assert any('clamscan' in c[0] for c in calls)


def test_database_freshness_not_updater_time(setup):
    cfg,p,t,calls=setup
    (Path(cfg['signatures'])/'fresh.json').write_text(json.dumps({'checked_at':time.time(),'database_at':time.time()-49*3600,'engine':'test'}))
    with pytest.raises(core.Hold,match='stale'):core.inspect(cfg,p,t)


@pytest.mark.parametrize('returncode,stdout,stderr',[(1,'FOUND',''),(2,'','error'),(0,'Scanned files: 0',''),(0,'Scanned files: 1','WARNING')])
def test_scan_errors_hold(setup,monkeypatch,returncode,stdout,stderr):
    cfg,p,t,calls=setup
    orig=core.sandbox
    def run(cfg,mounts,cmd,timeout=120):
        if 'clamscan' in cmd[0]:return subprocess.CompletedProcess(cmd,returncode,stdout,stderr)
        return orig(cfg,mounts,cmd,timeout)
    monkeypatch.setattr(core,'sandbox',run)
    with pytest.raises(core.Hold,match='antivirus'):core.inspect(cfg,p,t)


def test_large_video_explicitly_limited(setup):
    cfg,p,t,calls=setup
    with (p/'film.mp4').open('r+b') as f:f.truncate(core.LIMIT+1)
    t['files'][0]['length']=core.LIMIT+1
    r=core.inspect(cfg,p,t)
    assert r['verdict']=='media validated; full antivirus scan unavailable'
    assert r['limited_files']==['Film/film.mp4']
    assert not any('clamscan' in c[0] for c in calls)


def test_parser_failure_holds(setup,monkeypatch):
    cfg,p,t,calls=setup
    monkeypatch.setattr(core,'sandbox',lambda *a:subprocess.CompletedProcess([],1,'{}','invalid media'))
    with pytest.raises(core.Hold,match='video'):core.inspect(cfg,p,t)


def test_oversized_sidecar_holds(setup):
    cfg,p,t,calls=setup
    t['files'].append({'name':'Film/info.txt','length':core.LIMIT+1})
    with pytest.raises(core.Hold,match='oversized'):core.metadata(t)
