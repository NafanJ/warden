from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.request

VIDEO = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.mpg', '.mpeg', '.ts', '.m2ts', '.webm'}
TEXT = {'.srt', '.ass', '.ssa', '.vtt', '.nfo', '.txt'}
IMAGE = {'.jpg', '.jpeg', '.png'}
ALLOWED = VIDEO | TEXT | IMAGE
LIMIT = 2047 * 1024**2
_BROKER = None
HASH = re.compile(r'^[0-9a-fA-F]{40}$')


class Hold(Exception):
    pass


def safe_name(name):
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or '\\' in name or any(x in ('', '.', '..') for x in name.split('/'))
            or any(ord(c) < 32 or ord(c) == 127 or 0x202a <= ord(c) <= 0x202e or 0x2066 <= ord(c) <= 0x2069 for c in name)):
        raise Hold('unsafe path or filename')
    return p


def metadata(t):
    root = str(safe_name(t['name']))
    if '/' in root:
        raise Hold('invalid torrent root')
    files = t.get('files', [])
    if not files or len(files) > 5000:
        raise Hold('missing metadata or too many files')
    video = False
    seen = set()
    for f in files:
        p = safe_name(f['name'])
        if p.parts[0] != root or str(p).casefold() in seen:
            raise Hold('torrent root mismatch or filename collision')
        seen.add(str(p).casefold())
        ext = p.suffix.lower()
        if ext not in ALLOWED:
            raise Hold('unapproved file type: ' + ext)
        if f['length'] > LIMIT and ext not in VIDEO:
            raise Hold('oversized non-video file')
        video |= ext in VIDEO
    if not video:
        raise Hold('no video in torrent')
    return root


def same_regular_file(a, b):
    try:
        x, y = Path(a).lstat(), Path(b).lstat()
        return stat.S_ISREG(x.st_mode) and stat.S_ISREG(y.st_mode) and (x.st_dev, x.st_ino) == (y.st_dev, y.st_ino) and x.st_nlink == 2
    except FileNotFoundError:
        return False


def rename_noreplace(source, dest):
    """No payload overwrite, same-device publication under the shared worker lock.

    NTFS-3G rejects renameat2 flags. For files, link/unlink atomically claims the
    destination without overwrite; recovery recognises an interrupted link pair.
    For directories, ordinary rename cannot replace a nonempty directory or file.
    An explicit existence check holds even empty collisions; all pipeline writers
    and cleanup share worker.lock, and Transmission's approved mount is read-only.
    """
    source, dest = Path(source), Path(dest)
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(dest), 1)
    if result:
        code = ctypes.get_errno()
        if code not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code))
        if os.path.lexists(dest):
            raise FileExistsError(errno.EEXIST, 'destination exists')
        if source.is_dir() and not source.is_symlink():
            os.rename(source, dest)
        elif stat.S_ISREG(source.lstat().st_mode):
            os.link(source, dest, follow_symlinks=False)
            source.unlink()
        else:
            raise Hold('special source cannot be moved')
    for directory in {source.parent, dest.parent}:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno != errno.EINVAL or libc.syncfs(fd):
                    raise
        finally:
            os.close(fd)


class RPC:
    def __init__(self, cfg):
        self.url = cfg['transmission_url']
        auth = (cfg['transmission_user'] + ':' + cfg['transmission_pass']).encode()
        self.auth = 'Basic ' + base64.b64encode(auth).decode()
        self.sid = ''

    def __call__(self, method, arguments=None):
        # Installed Transmission 4 retains this protocol. Unknown future versions
        # fail closed rather than guessing new RPC semantics.
        for _ in range(3):
            req = urllib.request.Request(self.url, json.dumps({'method': method, 'arguments': arguments or {}}).encode(),
                {'Authorization': self.auth, 'X-Transmission-Session-Id': self.sid, 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    data = json.load(response)
                if data.get('result') != 'success':
                    raise Hold('Transmission rejected operation')
                return data['arguments']
            except urllib.error.HTTPError as exc:
                if exc.code != 409:
                    raise Hold('Transmission HTTP error') from None
                self.sid = exc.headers['X-Transmission-Session-Id']
        raise Hold('Transmission handshake failed')


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS jobs (
            hash TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', manifest TEXT,
            updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, hash TEXT, state TEXT, reason TEXT, at REAL);
        ''')

    def get(self, h):
        return self.db.execute('SELECT * FROM jobs WHERE hash=?', (h,)).fetchone()

    def add(self, h, name, category):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO jobs(hash,name,category,state,updated) VALUES(?,?,?,?,?)',
                            (h, name, category, 'incoming', time.time()))

    def state(self, h, state, reason='', manifest=None):
        with self.db:
            self.db.execute('UPDATE jobs SET state=?,reason=?,manifest=COALESCE(?,manifest),updated=? WHERE hash=?',
                            (state, reason, json.dumps(manifest) if manifest is not None else None, time.time(), h))
            self.db.execute('INSERT INTO events(hash,state,reason,at) VALUES(?,?,?,?)', (h, state, reason, time.time()))


def sandbox(cfg, mounts, command, timeout=120):
    if _BROKER is not None:
        return _BROKER.run(mounts, command, timeout)
    args = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--setenv', 'PATH', '/usr/bin',
            '--setenv', 'LANG', 'C', '--setenv', 'LD_LIBRARY_PATH', '/runtime/usr/lib/x86_64-linux-gnu',
            '--ro-bind', cfg['runtime'], '/runtime']
    for source, target in mounts:
        args += ['--ro-bind', str(source), target]
    result = subprocess.run(args + ['--'] + command, capture_output=True, text=True, timeout=timeout,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
    if len(result.stdout) + len(result.stderr) > 2_000_000:
        raise Hold('inspection output limit exceeded')
    return result


def manifest(payload, torrent):
    """Check exact metadata match, sizes and ordinary unlinked files, no traversal."""
    expected = {f['name']: f['length'] for f in torrent['files']}
    paths = [payload]
    result = []
    while paths:
        p = paths.pop()
        s = p.lstat()
        if stat.S_ISDIR(s.st_mode):
            paths.extend(p.iterdir())
            if len(paths) + len(result) > 10000:
                raise Hold('too many filesystem entries')
        elif stat.S_ISREG(s.st_mode) and s.st_nlink == 1:
            rel = str(p.relative_to(payload.parent))
            safe_name(rel)
            if rel not in expected or s.st_size != expected[rel]:
                raise Hold('payload differs from torrent manifest')
            result.append({'name': rel, 'size': s.st_size, 'inode': s.st_ino, 'mtime_ns': s.st_mtime_ns})
        else:
            raise Hold('symlink, hardlink or special file')
    if {f['name'] for f in result} != set(expected):
        raise Hold('missing files')
    return sorted(result, key=lambda x: x['name'])


def inspect(cfg, payload, torrent):
    metadata(torrent)
    before = manifest(payload, torrent)
    stamp = Path(cfg['signatures']) / 'fresh.json'
    freshness = json.loads(stamp.read_text())
    if any(not 0 <= time.time() - freshness[key] <= 48 * 3600 for key in ('checked_at', 'database_at')):
        raise Hold('antivirus signatures stale')
    eligible = []
    limited = []
    for entry in before:
        p = payload.parent / entry['name']
        ext = p.suffix.lower()
        if ext in VIDEO:
            r = sandbox(cfg, [(p, '/input')], ['/usr/bin/ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                    '-show_entries', 'format=format_name,duration:stream=codec_type,codec_name', '-of', 'json', '/input'])
            try:
                info = json.loads(r.stdout)
                formats = set(info['format']['format_name'].split(','))
                allowed_formats = {'matroska', 'webm', 'mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2', 'avi', 'mpeg', 'mpegts'}
                good = formats <= allowed_formats and float(info['format']['duration']) > 0 and any(
                    s.get('codec_type') == 'video' and s.get('codec_name') not in (None, 'unknown') for s in info['streams'])
            except (KeyError, ValueError, TypeError):
                good = False
            if r.returncode or r.stderr.strip() or not good:
                raise Hold('video format validation failed')
        else:
            # Parsing sidecars is bounded and never executes their contents.
            if entry['size'] > 32 * 1024**2:
                raise Hold('sidecar too large')
            r = sandbox(cfg, [(p, '/input')], ['/usr/bin/file', '--brief', '--mime-type', '/input'], 30)
            mime = r.stdout.strip()
            if r.returncode or (ext in IMAGE and mime not in {'image/jpeg', 'image/png'}) or (
                    ext in TEXT and mime not in {'text/plain', 'text/x-ssa', 'application/x-subrip', 'text/vtt', 'inode/x-empty'}):
                raise Hold('sidecar signature mismatch')
        if entry['size'] > LIMIT:
            limited.append(entry['name'])
        else:
            eligible.append('/payload/' + entry['name'])
    if eligible:
        listing = Path(cfg['state_dir']) / 'scan-files.txt'
        listing.write_text('\n'.join(eligible) + '\n')
        r = sandbox(cfg, [(payload.parent, '/payload'), (cfg['signatures'], '/signatures'), (listing, '/scan-files')],
            ['/runtime/usr/bin/clamscan', '--database=/signatures', '--cvdcertsdir=/runtime/etc/clamav/certs', '--file-list=/scan-files',
             '--max-filesize=2047M', '--max-scansize=2047M', '--alert-exceeds-max=yes',
             '--max-scantime=180000', '--infected'], 1800)
        match = re.search(r'^Scanned files:\s*(\d+)', r.stdout, re.M)
        if r.returncode or not match or int(match.group(1)) != len(eligible) or r.stderr.strip():
            raise Hold('antivirus detection, error or incomplete scan')
    after = manifest(payload, torrent)
    if before != after:
        raise Hold('payload changed during inspection')
    return {'files': before, 'engine': freshness['engine'], 'signatures_checked_at': freshness['checked_at'],
            'limited_files': limited, 'verdict': 'media validated; full antivirus scan unavailable' if limited else 'media validated; antivirus scan passed'}


class Guard:
    def __init__(self, cfg, rpc=None, inspector=inspect):
        self.cfg = cfg
        self.store = Store(Path(cfg['state_dir']) / 'guard.sqlite3')
        self.rpc = rpc or RPC(cfg)
        self.inspector = inspector

    def paths(self, row):
        root = str(safe_name(row['name']))
        category = row['category']
        if category not in self.cfg['categories'] or '/' in root or not HASH.fullmatch(row['hash']):
            raise Hold('invalid persisted job')
        return (Path(self.cfg['incoming']) / 'complete' / category / root,
                Path(self.cfg['stage']) / row['hash'] / root,
                Path(self.cfg['approved']) / category / root)

    def pause(self, h):
        self.rpc('torrent-stop', {'ids': [h]})
        for _ in range(20):
            ts = self.rpc('torrent-get', {'ids': [h], 'fields': ['status']})['torrents']
            if len(ts) == 1 and ts[0]['status'] == 0:
                return
            time.sleep(.25)
        raise Hold('torrent did not stop')

    def run_one(self, t, can_scan=True):
        h = t['hashString'].lower()
        if not HASH.fullmatch(h):
            raise Hold('invalid torrent hash')
        row = self.store.get(h)
        if row and row['state'] == 'released':
            return
        category = PurePosixPath(t['downloadDir']).name if t['downloadDir'] != '/quarantine/complete' else ''
        allowed_dirs = {('/quarantine/complete/' + c).rstrip('/') for c in self.cfg['categories']}
        if row is None:
            if t['downloadDir'] not in allowed_dirs:
                self.pause(h)
                return  # never adopt an unexpected path or legacy download
            self.store.add(h, t['name'], category)
            row = self.store.get(h)
        try:
            if row['state'] == 'held':
                self.pause(h)
                return
            if row['state'] in ('publishing', 'published'):
                return self.publish(row)
            # Metadata is screened early; actual files are checked after completion.
            if t.get('metadataPercentComplete', 1) < 1:
                return
            metadata(t)
            if row['state'] == 'incoming' and t.get('percentDone', 0) < 1:
                return
            if not can_scan:
                return
            self.pause(h)
            source, staged, target = self.paths(row)
            staged.parent.mkdir(exist_ok=True)
            if row['state'] in ('incoming', 'moving'):
                self.store.state(h, 'moving')
                if source.exists() and staged.exists():
                    if same_regular_file(source, staged):
                        source.unlink()
                    else:
                        raise Hold('stage collision')
                if not staged.exists():
                    rename_noreplace(source, staged)
                self.store.state(h, 'inspecting')
            result = self.inspector(self.cfg, staged, t)
            self.store.state(h, 'publishing', result['verdict'], result)
            self.publish(self.store.get(h))
        except Exception as exc:
            # Record only controlled messages, never URLs, credentials or subprocess output.
            reason = str(exc) if isinstance(exc, Hold) else type(exc).__name__ + (f' errno={exc.errno}' if isinstance(exc, OSError) else '') + ' during inspection; retry after review'
            self.store.state(h, 'held', reason)
            try:
                self.pause(h)
            except Exception:
                pass

    def publish(self, row):
        h = row['hash']
        source, staged, target = self.paths(row)
        if not row['manifest']:
            raise Hold('publication missing inspection record')
        self.pause(h)
        target.parent.mkdir(exist_ok=True)
        if row['state'] == 'publishing':
            if staged.exists():
                if same_regular_file(staged, target):
                    staged.unlink()
                else:
                    rename_noreplace(staged, target)
            elif not target.exists():
                raise Hold('publication payload missing')
            self.store.state(h, 'published', row['reason'])
        # Verify a recovered rename really belongs to this job, never adopt a collision.
        for item in json.loads(row['manifest'])['files']:
            p = target.parent / item['name']
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_ino != item['inode'] or st.st_size != item['size']:
                raise Hold('publication identity mismatch')
        self.rpc('torrent-set-location', {'ids': [h], 'location': '/data/downloads/complete/' + row['category'], 'move': False})
        # Set-location is idempotent, and existing seed limits stay unchanged.
        self.rpc('torrent-start', {'ids': [h]})
        self.store.state(h, 'released', row['reason'])

    def scan_window(self):
        values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        if int(values['MemAvailable'].split()[0]) < 5 * 1024**2:
            return False
        # Query Plex without printing its token. Unavailable Plex conservatively defers scans.
        try:
            import xml.etree.ElementTree as ET
            req = urllib.request.Request(self.cfg['plex_url'] + '/status/sessions', headers={'X-Plex-Token': self.cfg['plex_token']})
            with urllib.request.urlopen(req, timeout=10) as r:
                root = ET.fromstring(r.read())
            if root.findall('.//TranscodeSession'):
                return False
        except Exception:
            return False
        disk = os.statvfs(self.cfg['incoming'])
        return disk.f_bavail * disk.f_frsize >= 10 * 1024**3

    def tick(self):
        ts = self.rpc('torrent-get', {'fields': ['hashString', 'name', 'downloadDir', 'status', 'files',
                'metadataPercentComplete', 'percentDone']})['torrents']
        can_scan = self.scan_window()
        for t in ts:
            self.run_one(t, self.scan_window())
        health = {'checked_at': time.time(), 'scan_window_open': can_scan,
                  'states': dict(self.store.db.execute('SELECT state,count(*) FROM jobs GROUP BY state').fetchall())}
        p = Path(self.cfg['state_dir']) / 'health.json'
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(health))
        os.replace(tmp, p)
