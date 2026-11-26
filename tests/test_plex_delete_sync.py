import json
import os
import time
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from warden.plex_delete_sync.core import Sync, completed_delete, empty_media_tree, identity, mapped_path


def line(key, code=200, suffix='', stamp=None):
    from datetime import datetime
    date = datetime.fromtimestamp(stamp or time.time()).strftime('%b %d, %Y %H:%M:%S.%f')[:-3]
    return f'{date} [123] INFO - Completed: [127.0.0.1:123] {code} DELETE /library/metadata/{key}{suffix} (1 live) #ab12 4ms 166 bytes\n'


class FakeAPI:
    def __init__(self, base):
        self.record = {'id': 7, 'tmdbId': 123, 'tvdbId': 456, 'path': '/data/Film/Test', 'added': '2026-01-01', 'title': 'Test'}
        self.present = True
        self.deleted = []
        self.kind = 'movie'
        self.base = base

    def check_server(self):
        pass

    def records(self, app):
        return [self.record] if self.record and app == ('radarr' if self.kind == 'movie' else 'sonarr') else []

    def request(self, app, path):
        if '/sections/' in path:
            if not self.present:
                return ET.Element('MediaContainer')
            tag = 'Video' if self.kind == 'movie' else 'Directory'
            return ET.fromstring(f'<MediaContainer><{tag} ratingKey="11" type="{self.kind}" guid="plex://test"><Guid id="tmdb://123"/><Guid id="tvdb://456"/><Media><Part file="/movies/Test/movie.mkv"/></Media></{tag}></MediaContainer>')
        if path == '/library/metadata/11' and self.present:
            return ET.fromstring('<MediaContainer><Directory><Location path="/movies/Test"/></Directory></MediaContainer>')
        return None

    def get_record(self, app, item_id):
        return self.record

    def remove_record(self, app, item_id):
        self.deleted.append((app, item_id))
        self.record = None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    base = tmp_path / 'media'; base.mkdir()
    root = base / 'Test'; root.mkdir()
    (root / 'movie.mkv').write_bytes(b'video')
    logs = tmp_path / 'logs'; logs.mkdir()
    log = logs / 'Plex Media Server.log'; log.write_text('')
    cfg = {'state_dir': str(tmp_path / 'state'), 'mount': str(base), 'log_dir': str(logs),
           'roots': [{'host': str(base), 'arr': '/data/Film', 'plex': '/movies'}],
           'sections': [{'id': 1, 'app': 'radarr'}]}
    api = FakeAPI(base)
    sync = Sync(cfg, api)
    monkeypatch.setattr(sync, 'mounted', lambda: True)
    return sync, api, log, root


def delete(rig, key=11, code=200, suffix=''):
    sync, api, log, root = rig
    api.present = False
    (root / 'movie.mkv').unlink(missing_ok=True)
    log.write_text(log.read_text() + line(key, code, suffix, time.time() + .01))


def test_successful_movie_and_restart_idempotency(rig):
    sync, api, log, root = rig
    sync.tick(); delete(rig); sync.tick(); sync.tick()
    assert api.deleted == [('radarr', 7)]
    assert sync.db.execute("select state from events").fetchone()[0] == 'done'
    again = Sync(sync.cfg, api); again.mounted = lambda: True; again.tick()
    assert len(api.deleted) == 1


def test_whole_series_deletion(rig):
    sync, api, *_ = rig
    api.kind = 'show'; sync.cfg['sections'][0]['app'] = 'sonarr'
    sync.tick(); delete(rig); sync.tick()
    assert api.deleted == [('sonarr', 7)]


@pytest.mark.parametrize('key,suffix,code', [(22, '', 200), (33, '', 200), (11, '/media/45', 200), (11, '', 403), (11, '', 500)])
def test_episode_season_version_failed_deletes_do_not_remove_show(rig, key, suffix, code):
    sync, api, *_ = rig
    api.kind = 'show'; sync.cfg['sections'][0]['app'] = 'sonarr'
    sync.tick(); delete(rig, key, code, suffix); sync.tick()
    assert api.deleted == []


def test_absence_without_delete_and_old_logs_are_ignored(rig):
    sync, api, log, root = rig
    log.write_text(line(11, stamp=time.time() - 60))
    sync.tick()
    api.present = False; (root / 'movie.mkv').unlink(); sync.tick()
    assert api.deleted == []


def test_remaining_media_prevents_deletion(rig):
    sync, api, log, root = rig
    sync.tick(); delete(rig)
    (root / 'other.mp4').write_bytes(b'survivor'); sync.tick()
    assert api.deleted == []


def test_unmounted_drive_blocks_action_until_recovered(rig):
    sync, api, *_ = rig
    sync.tick(); delete(rig)
    sync.mounted = lambda: False
    with pytest.raises(RuntimeError): sync.tick()
    assert api.deleted == []
    sync.mounted = lambda: True; sync.tick()
    assert api.deleted == [('radarr', 7)]


def test_readded_or_changed_arr_record_not_removed(rig):
    sync, api, *_ = rig
    sync.tick(); delete(rig); api.record['added'] = '2026-12-01'; sync.tick()
    assert api.deleted == []


def test_plex_still_present_blocks_action(rig):
    sync, api, *_ = rig
    sync.tick(); delete(rig); api.present = True; sync.tick()
    assert api.deleted == []


def test_rotation_and_partial_lines(rig):
    sync, api, log, root = rig
    sync.tick(); delete(rig)
    text = log.read_text(); log.write_text(text[:50]); sync.tick()
    assert api.deleted == []
    log.write_text(text); log.rename(log.with_name('Plex Media Server.1.log'))
    log.write_text('new log\n'); sync.tick()
    assert api.deleted == [('radarr', 7)]


def test_outage_retries_without_losing_event(rig, monkeypatch):
    sync, api, *_ = rig
    sync.tick(); delete(rig)
    original = api.get_record
    def fail(*args): raise ConnectionError('Unavailable')
    monkeypatch.setattr(api, 'get_record', fail)
    with pytest.raises(ConnectionError): sync.tick()
    assert api.deleted == []
    monkeypatch.setattr(api, 'get_record', original); sync.tick()
    assert api.deleted == [('radarr', 7)]


def test_exact_path_and_guid_mapping_required(rig):
    sync, api, *_ = rig
    api.record['path'] = '/data/Film/Different'; sync.tick(); delete(rig); sync.tick()
    assert api.deleted == []


def test_no_path_traversal_or_symlinks(tmp_path):
    roots = [{'host': str(tmp_path), 'arr': '/data/Film'}]
    assert mapped_path('/data/Film/../TV/title', roots, 'arr') is None
    assert mapped_path('/data/Film', roots, 'arr') is None
    outside = tmp_path / 'outside'; outside.mkdir()
    link = tmp_path / 'link'; link.symlink_to(outside, target_is_directory=True)
    assert not empty_media_tree(link / 'missing')


def test_exact_log_shape():
    assert completed_delete(line(11))[0] == '11'
    assert completed_delete(line(11).replace('DELETE', 'GET')) is None
    assert completed_delete(line(11, suffix='/children')) is None
