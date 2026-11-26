import json
from pathlib import Path
import pytest
from warden.media_guard.core import Guard, Hold, manifest, metadata, rename_noreplace

H = 'a' * 40


def torrent():
    return dict(hashString=H, name='Film', downloadDir='/quarantine/complete/movies', status=0,
                percentDone=1, metadataPercentComplete=1, files=[dict(name='Film/movie.mp4', length=4)])


@pytest.fixture
def rig(tmp_path):
    cfg = {'categories': ['movies', 'tv-sonarr', '']}
    for key in ['incoming', 'stage', 'approved', 'state_dir']:
        cfg[key] = str(tmp_path / key)
        Path(cfg[key]).mkdir()
    src = Path(cfg['incoming']) / 'complete/movies/Film'
    src.mkdir(parents=True)
    (src / 'movie.mp4').write_bytes(b'test')
    calls = []
    def rpc(method, args):
        calls.append((method, args))
        return {'torrents': [{'status': 0}]}
    def inspect(cfg, path, t):
        return {'verdict': 'test passed', 'files': manifest(path, t)}
    return Guard(cfg, rpc, inspect), calls


def test_clean_publish_and_idempotence(rig):
    guard, calls = rig
    guard.run_one(torrent())
    assert guard.store.get(H)['state'] == 'released'
    assert (Path(guard.cfg['approved']) / 'movies/Film/movie.mp4').read_bytes() == b'test'
    n = len(calls)
    guard.run_one(torrent())
    assert len(calls) == n


@pytest.mark.parametrize('name', ['Film/../bad.mp4', '/Film/a.mp4', 'Film/a.exe', 'Film/a.zip', 'Film/a.lnk', 'Film/a.iso', 'Film/\u202ebad.mp4'])
def test_bad_metadata_holds_before_move(rig, name):
    guard, calls = rig
    t = torrent(); t['files'][0]['name'] = name
    guard.run_one(t)
    assert guard.store.get(H)['state'] == 'held'
    assert not list(Path(guard.cfg['approved']).iterdir())
    assert not any(c[0] == 'torrent-start' for c in calls)


def test_scanner_failure_is_private_hold(rig):
    guard, calls = rig
    guard.inspector = lambda *args: (_ for _ in ()).throw(Hold('scan failure'))
    guard.run_one(torrent())
    assert guard.store.get(H)['state'] == 'held'
    assert (Path(guard.cfg['stage']) / H / 'Film/movie.mp4').exists()
    assert not list(Path(guard.cfg['approved']).iterdir())


def test_recovery_after_move_before_state_commit(rig):
    guard, calls = rig
    t = torrent()
    guard.store.add(H, t['name'], 'movies')
    source, staged, target = guard.paths(guard.store.get(H))
    staged.parent.mkdir()
    guard.store.state(H, 'moving')
    rename_noreplace(source, staged)
    t['percentDone'] = 0  # Transmission restart may notice staged files are absent
    guard.run_one(t)
    assert guard.store.get(H)['state'] == 'released'


def test_recovery_after_publish_rename(rig):
    guard, calls = rig
    t = torrent()
    guard.store.add(H, t['name'], 'movies')
    source, staged, target = guard.paths(guard.store.get(H))
    staged.parent.mkdir(); target.parent.mkdir()
    rename_noreplace(source, staged)
    result = guard.inspector(guard.cfg, staged, t)
    guard.store.state(H, 'publishing', 'passed', result)
    rename_noreplace(staged, target)
    guard.run_one(t)
    assert guard.store.get(H)['state'] == 'released'


def test_collision_never_overwrites(rig):
    guard, calls = rig
    target = Path(guard.cfg['approved']) / 'movies/Film'
    target.mkdir(parents=True)
    (target / 'movie.mp4').write_bytes(b'old!')
    guard.run_one(torrent())
    assert guard.store.get(H)['state'] == 'held'
    assert (target / 'movie.mp4').read_bytes() == b'old!'


def test_symlink_hold(rig):
    guard, calls = rig
    p = Path(guard.cfg['incoming']) / 'complete/movies/Film/movie.mp4'
    p.unlink(); p.symlink_to('/etc/passwd')
    guard.run_one(torrent())
    assert guard.store.get(H)['state'] == 'held'


def test_low_resources_defer(rig):
    guard, calls = rig
    guard.run_one(torrent(), False)
    assert guard.store.get(H)['state'] == 'incoming'
    assert not list(Path(guard.cfg['approved']).iterdir())


def test_recovered_target_must_match_original_inode(rig):
    guard, calls = rig
    t = torrent(); guard.store.add(H, t['name'], 'movies')
    source, staged, target = guard.paths(guard.store.get(H))
    original = manifest(source, t)
    guard.store.state(H, 'publishing', 'passed', {'files': original})
    target.mkdir(parents=True); (target / 'movie.mp4').write_bytes(b'fake')
    guard.run_one(t)
    assert guard.store.get(H)['state'] == 'held'
    assert not any(c[0] == 'torrent-start' for c in calls)


def test_ntfs_link_move_crash_recovery(rig):
    import os
    guard,calls=rig
    t=torrent();t['name']='single.mp4';t['files']=[{'name':'single.mp4','length':4}]
    source=Path(guard.cfg['incoming'])/'complete/movies/single.mp4'
    source.write_bytes(b'test')
    guard.store.add(H,t['name'],'movies')
    src,staged,target=guard.paths(guard.store.get(H));staged.parent.mkdir()
    guard.store.state(H,'moving')
    os.link(src,staged)  # crash between exclusive link creation and source unlink
    t['percentDone']=0
    guard.run_one(t)
    assert guard.store.get(H)['state']=='released'
    assert target.exists() and not src.exists() and not staged.exists()


def test_single_file_publish_link_crash_recovery(rig):
    import os
    guard,calls=rig
    t=torrent();t['name']='single.mp4';t['files']=[{'name':'single.mp4','length':4}]
    guard.store.add(H,t['name'],'movies')
    source,staged,target=guard.paths(guard.store.get(H));staged.parent.mkdir();target.parent.mkdir()
    staged.write_bytes(b'test')
    result=guard.inspector(guard.cfg,staged,t)
    guard.store.state(H,'publishing','passed',result)
    os.link(staged,target)
    guard.run_one(t)
    assert guard.store.get(H)['state']=='released'
    assert target.stat().st_nlink==1 and not staged.exists()
