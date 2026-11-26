"""Fail-closed Plex deletion sync. Never deletes media files or infers deletion from absence."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

COMPLETED = re.compile(r'Completed: .*?\] 200 DELETE /library/metadata/(\d+)(?:\?[^ ]*)? .*?#[a-zA-Z0-9]+\b')
LOG_NAME = re.compile(r'Plex Media Server(?:\.\d+)?\.log$')
MEDIA = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.mpg', '.mpeg', '.ts', '.m2ts', '.iso', '.vob', '.strm', '.webm', '.divx'}


def completed_delete(line):
    match = COMPLETED.search(line)
    if not match:
        return None
    try:
        stamp = dt.datetime.strptime(line[:24], '%b %d, %Y %H:%M:%S.%f').timestamp()
    except ValueError:
        return None
    return match[1], stamp


class API:
    def __init__(self, cfg):
        self.cfg = cfg

    def request(self, service, path, method='GET'):
        conf = self.cfg[service]
        # Read only the credentials needed by the particular local service.
        xml = ET.parse(conf['credentials']).getroot()
        if service == 'plex':
            headers = {'X-Plex-Token': xml.attrib['PlexOnlineToken']}
        else:
            headers = {'X-Api-Key': xml.findtext('ApiKey')}
        req = urllib.request.Request(conf['url'] + path, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            # Never log requests, tokens or HTTP bodies.
            raise RuntimeError(f'{service} HTTP {exc.code}') from None
        if not data:
            return {}
        return ET.fromstring(data) if service == 'plex' else json.loads(data)

    def check_server(self):
        root = self.request('plex', '/')
        if root is None or root.get('machineIdentifier') != self.cfg['plex']['machine_identifier']:
            raise RuntimeError('Plex server identity changed')

    def records(self, app):
        value = self.request(app, '/api/v3/' + ('movie' if app == 'radarr' else 'series'))
        if not isinstance(value, list):
            raise RuntimeError('Invalid arr inventory')
        return value

    def get_record(self, app, item_id):
        return self.request(app, '/api/v3/' + ('movie/' if app == 'radarr' else 'series/') + str(item_id))

    def remove_record(self, app, item_id):
        exclusion = 'addImportExclusion' if app == 'radarr' else 'addImportListExclusion'
        path = '/api/v3/' + ('movie/' if app == 'radarr' else 'series/') + str(item_id)
        return self.request(app, path + '?deleteFiles=false&' + exclusion + '=true', 'DELETE')


def mapped_path(value, mappings, column):
    """Only absolute, canonical, strictly nested paths from configured roots."""
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts:
        return None
    for row in mappings:
        base = Path(row[column])
        if p.is_relative_to(base) and p != base:
            return str(Path(row['host']) / p.relative_to(base))
    return None


def identity(record, app):
    return {k: record.get(k) for k in ['id', 'path', 'added', 'tmdbId' if app == 'radarr' else 'tvdbId']}


def empty_media_tree(path):
    """Missing directory is OK; symlinks, permission errors and surviving videos are not."""
    root = Path(path)
    # Any symlink in the ancestry could lead outside the audited disk.
    if any(p.is_symlink() for p in [root, *root.parents]):
        return False
    try:
        root.stat()
    except FileNotFoundError:
        return True
    if not root.is_dir():
        return False
    def fail(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        for name in dirs + files:
            p = Path(folder) / name
            if p.is_symlink() or (p.is_file() and p.suffix.lower() in MEDIA):
                return False
    return True


class Sync:
    def __init__(self, cfg, api=None):
        self.cfg = cfg
        self.api = api or API(cfg)
        self.state_dir = Path(cfg['state_dir'])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state_dir / 'sync.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS cursors (inode TEXT PRIMARY KEY,offset INTEGER);
          CREATE TABLE IF NOT EXISTS items (key TEXT PRIMARY KEY,record TEXT,seen REAL);
          CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY,key TEXT,record TEXT,
            created REAL,state TEXT,reason TEXT);
        ''')

    def mounted(self):
        mount = Path(self.cfg['mount'])
        if not os.path.ismount(mount):
            return False
        device = mount.stat().st_dev
        return all(Path(r['host']).is_dir() and Path(r['host']).stat().st_dev == device
                   and Path(r['host']).resolve().is_relative_to(mount.resolve()) for r in self.cfg['roots'])

    def logs(self):
        return sorted((p for p in Path(self.cfg['log_dir']).iterdir() if LOG_NAME.fullmatch(p.name)),
                      key=lambda p: p.stat().st_mtime)

    def snapshot(self):
        """Cache identities before a delete; never guess by display title."""
        records = {a: self.api.records(a) for a in ['radarr', 'sonarr']}
        roots = self.cfg['roots']
        count = 0
        for section in self.cfg['sections']:
            app = section['app']
            field, scheme, kind = ('tmdbId', 'tmdb', 'movie') if app == 'radarr' else ('tvdbId', 'tvdb', 'show')
            inventory = self.api.request('plex', f"/library/sections/{section['id']}/all?includeGuids=1")
            if inventory is None:
                raise RuntimeError('Plex library unavailable')
            for item in inventory:
                key = item.get('ratingKey')
                if item.get('type') != kind or not key or not key.isdigit():
                    continue
                guids = {g.get('id') for g in item.findall('Guid')}
                candidates = [r for r in records[app] if f'{scheme}://{r.get(field)}' in guids]
                if len(candidates) != 1:
                    continue
                record = candidates[0]
                host = mapped_path(record['path'], roots, 'arr')
                if not host:
                    continue
                if kind == 'show':
                    details = self.api.request('plex', '/library/metadata/' + key)
                    if details is None:
                        continue
                    paths = [mapped_path(p.get('path', ''), roots, 'plex') for p in details.findall('.//Location')]
                    matches = paths and all(p == host for p in paths)
                else:
                    paths = [mapped_path(p.get('file', ''), roots, 'plex') for p in item.findall('.//Part')]
                    matches = paths and all(p and Path(p).is_relative_to(host) for p in paths)
                if not matches:
                    continue
                saved = {'app': app, 'identity': identity(record, app), 'host': host,
                         'title': record['title'], 'kind': kind, 'plex_guid': item.get('guid')}
                self.db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?)', (key, json.dumps(saved), time.time()))
                count += 1
        self.db.commit()
        return count

    def baseline(self):
        # Record offsets BEFORE snapshot so a deletion during inventory is not lost.
        offsets = [(str(p.stat().st_ino), p.stat().st_size) for p in self.logs()]
        since = time.time()
        count = self.snapshot()
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO cursors VALUES(?,?)', offsets)
            self.db.execute('INSERT INTO meta VALUES(?,?)', ('since', str(since)))
        return count

    def ingest(self):
        since = float(self.db.execute("SELECT value FROM meta WHERE key='since'").fetchone()[0])
        for path in self.logs():
            with path.open('rb') as f:
                st = os.fstat(f.fileno())
                inode = str(st.st_ino)
                row = self.db.execute('SELECT offset FROM cursors WHERE inode=?', (inode,)).fetchone()
                offset = row[0] if row and row[0] <= st.st_size else 0
                f.seek(offset)
                with self.db:
                    for raw in f:
                        if not raw.endswith(b'\n'):
                            break  # retry partial log lines next tick
                        offset += len(raw)
                        event = completed_delete(raw.decode('utf-8', 'replace'))
                        if not event or event[1] < since:
                            continue
                        key, stamp = event
                        cached = self.db.execute('SELECT record,seen FROM items WHERE key=?', (key,)).fetchone()
                        # Must have existed in our inventory before this delete.
                        valid = cached and cached['seen'] <= stamp and stamp - cached['seen'] < 86400 * 7
                        record = cached['record'] if valid else None
                        self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)',
                                        (hashlib.sha256(raw).hexdigest(), key, record, stamp,
                                         'pending' if record else 'ignored', '' if record else 'Not a cached whole movie/show'))
                    self.db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (inode, offset))

    def finish(self, event, state, reason):
        with self.db:
            self.db.execute('UPDATE events SET state=?,reason=? WHERE id=?', (state, reason, event['id']))
        print(json.dumps({'event': event['id'][:12], 'key': event['key'], 'state': state, 'reason': reason}), flush=True)

    def reconcile(self):
        for event in self.db.execute("SELECT * FROM events WHERE state='pending'").fetchall():
            item = json.loads(event['record'])
            if time.time() - event['created'] > 86400:
                self.finish(event, 'expired', 'Unresolved deletion older than 24 hours; manual review required')
                continue
            if not self.mounted():
                continue
            # A failure, outage, partial delete or same-key replacement cannot remove an arr record.
            if self.api.request('plex', '/library/metadata/' + event['key']) is not None:
                continue
            current = self.api.get_record(item['app'], item['identity']['id'])
            if current is None:
                self.finish(event, 'done', 'Entry already absent')
                continue
            if identity(current, item['app']) != item['identity']:
                self.finish(event, 'ignored', 'Arr identity changed since deletion')
                continue
            if not empty_media_tree(item['host']):
                continue
            self.api.remove_record(item['app'], current['id'])
            if self.api.get_record(item['app'], current['id']) is not None:
                raise RuntimeError('Arr removal did not persist')
            self.finish(event, 'done', f"Removed {item['app']} record: {item['title']}")

    def tick(self):
        self.api.check_server()
        if not self.mounted():
            raise RuntimeError('Media mount unavailable; no actions taken')
        if not self.db.execute("SELECT 1 FROM meta WHERE key='since'").fetchone():
            count = self.baseline()
        else:
            # Ingest using the previous inventory, before refreshing it.
            self.ingest()
            self.reconcile()
            count = self.snapshot()
        summary = {'checked_at': time.time(), 'mapped_titles': count,
                   'events': dict(self.db.execute('SELECT state,count(*) FROM events GROUP BY state').fetchall())}
        (self.state_dir / 'health.json').write_text(json.dumps(summary))
        print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/plex-delete-sync/config.json')
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    Path(cfg['state_dir']).mkdir(parents=True, exist_ok=True)
    with (Path(cfg['state_dir']) / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            Sync(cfg).tick()
        except Exception as exc:
            # Exception messages from network/XML parsers may contain secrets: log only type.
            print('Plex deletion sync deferred:', type(exc).__name__, flush=True)
            raise SystemExit(1) from None
