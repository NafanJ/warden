#!/usr/bin/env python3
"""Explicit prepare/cutover/verify phases. Never dumps compose or credentials."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

SOURCE = Path('/home/nathan/Sandbox/warden')
BASE = Path('/opt/media-server/security')
COMPOSE = Path('/opt/media-server/docker-compose.yml')
DEPLOY = Path('/opt/warden')
sys.path.insert(0, str(SOURCE))
from warden.media_guard.core import RPC, Store


def run(cmd, **kwargs):
    r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if r.returncode:
        raise RuntimeError(f'{cmd[0]} failed ({r.returncode}): {r.stderr[-500:]}')
    return r.stdout


def api(app, path, body=None, method=None):
    import urllib.request
    port = {'radarr': 7878, 'sonarr': 8989, 'prowlarr': 9696}[app]
    key = ET.parse('/opt/appdata/' + app + '/config.xml').findtext('ApiKey')
    version = 'v1' if app == 'prowlarr' else 'v3'
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/{version}/{path}',
        data=json.dumps(body).encode() if body is not None else None,
        headers={'X-Api-Key': key, 'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def prepare():
    BASE.mkdir(mode=0o700, exist_ok=True)
    for name in ['state', 'signatures', 'runtime', 'backups']:
        (BASE / name).mkdir(mode=0o700, exist_ok=True)
    for package in Path('/tmp/media-guard-debs').glob('*.deb'):
        run(['dpkg-deb', '-x', str(package), str(BASE / 'runtime')])
    shutil.copytree(SOURCE / 'warden/media_guard', DEPLOY / 'warden/media_guard', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    # Use only required secrets; do not give the worker Warden's full environment.
    env = {}
    for line in Path('/opt/warden/.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1); env[k] = v.strip().strip('"').strip("'")
    pref = ET.parse('/opt/appdata/plex/Library/Application Support/Plex Media Server/Preferences.xml').getroot()
    cfg = dict(transmission_url=env['TRANSMISSION_URL'].replace('localhost', '127.0.0.1'),
        transmission_user=env['TRANSMISSION_USER'], transmission_pass=env['TRANSMISSION_PASS'],
        plex_url='http://127.0.0.1:32400', plex_token=pref.attrib['PlexOnlineToken'],
        incoming='/mnt/Modi/download-quarantine', stage='/mnt/Modi/media-security-stage',
        approved='/mnt/Modi/Kodi/downloads/complete', state_dir=str(BASE / 'state'),
        signatures=str(BASE / 'signatures'), runtime=str(BASE / 'runtime'), categories=['movies', 'tv-sonarr', ''])
    config = BASE / 'config.json'; config.write_text(json.dumps(cfg, indent=2)); config.chmod(0o600)
    for p in [Path(cfg['incoming']) / 'complete', Path(cfg['incoming']) / 'incomplete', Path(cfg['stage'])]:
        p.mkdir(parents=True, exist_ok=True)
    for cat in cfg['categories']:
        (Path(cfg['incoming']) / 'complete' / cat).mkdir(exist_ok=True)
    if len({os.stat(cfg[k]).st_dev for k in ['incoming','stage','approved']}) != 1:
        raise RuntimeError('Staging and approved paths must share a filesystem')
    Store(BASE / 'state/guard.sqlite3')
    (BASE / 'state/freshclam.conf').write_text(f'''DatabaseDirectory {BASE}/signatures
CVDCertsDirectory {BASE}/runtime/etc/clamav/certs
DatabaseMirror database.clamav.net
DatabaseOwner nathan
Checks 1
TestDatabases yes
Bytecode yes
ConnectTimeout 30
ReceiveTimeout 120
''')
    units = Path.home() / '.config/systemd/user'
    for name in ['media-guard.service', 'media-signatures.service', 'media-signatures.timer', 'media-update-health.service', 'media-update-health.timer']:
        shutil.copy2(SOURCE / 'deploy' / name, units / name)
    shutil.copy2(SOURCE / 'deploy/media_update_health.py', BASE / 'media_update_health.py')
    shutil.copy2(SOURCE / 'MEDIA_SECURITY.md', BASE / 'README.md')
    run(['systemctl', '--user', 'daemon-reload'])
    print('Prepared isolated runtime, code, directories and service units; pipeline unchanged')


def cutover():
    cfg = json.loads((BASE / 'config.json').read_text())
    rpc = RPC(cfg)
    for i in [370,373,374,376,377,380,397,403,406]:
        if not api('radarr', f'movie/{i}').get('hasFile'):
            raise RuntimeError('Waiting for all nine imports')
    if rpc('torrent-get', {'fields': ['id']})['torrents']:
        raise RuntimeError('Transmission has active entries; review before migration')
    fresh = json.loads((BASE / 'signatures/fresh.json').read_text())
    if time.time() - fresh['checked_at'] > 48*3600:
        raise RuntimeError('Signatures not current')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = BASE / 'backups' / stamp; backup.mkdir(mode=0o700)
    shutil.copy2(COMPOSE, backup / 'docker-compose.yml')
    shutil.copy2('/opt/appdata/transmission/settings.json', backup / 'transmission-settings.json')
    for app in ['sonarr', 'radarr']:
        for endpoint in ['downloadclient', 'qualityprofile', 'config/downloadclient']:
            (backup / (app + '-' + endpoint.replace('/', '-') + '.json')).write_text(json.dumps(api(app, endpoint)))
    import sqlite3
    for app in ['sonarr','radarr','prowlarr']:
        for src in Path('/opt/appdata', app).glob('*.db'):
            with sqlite3.connect(f'file:{src}?mode=ro', uri=True) as src_db:
                with sqlite3.connect(backup / (app + '-' + src.name)) as dst_db:
                    src_db.backup(dst_db)
        shutil.copy2(Path('/opt/appdata', app, 'config.xml'), backup / (app + '-config.xml'))
    # Stop Arr briefly so no new grabs or imports race the cutover.
    run(['docker', 'compose', '-f', str(COMPOSE), 'stop', 'sonarr', 'radarr'])
    if rpc('torrent-get', {'fields': ['id']})['torrents']:
        run(['docker', 'compose', '-f', str(COMPOSE), 'start', 'sonarr', 'radarr'])
        raise RuntimeError('A download arrived during preparation; cutover aborted')
    run(['docker', 'compose', '-f', str(COMPOSE), 'stop', 'transmission'])
    # Pause preserves the operator's control if any subsequent step fails.
    settings = Path('/opt/appdata/transmission/settings.json')
    data = json.loads(settings.read_text())
    data.update({'download-dir':'/quarantine/complete', 'incomplete-dir':'/quarantine/incomplete',
                 'incomplete-dir-enabled':True, 'script-torrent-done-enabled':False,
                 'script-torrent-added-enabled':False})
    settings.write_text(json.dumps(data, indent=4))
    import yaml
    doc = yaml.safe_load(COMPOSE.read_text())
    tx = doc['services']['transmission']
    tx['volumes'] = ['/opt/appdata/transmission:/config',
                     cfg['incoming'] + ':/quarantine', cfg['approved'] + ':/data/downloads/complete:ro']
    tx['security_opt'] = ['no-new-privileges:true']
    COMPOSE.write_text(yaml.safe_dump(doc, sort_keys=False))
    # Install cleanup gate BEFORE enabling the new paths.
    live_reaper = DEPLOY / 'warden/sentinel/reaper.py'
    shutil.copy2(live_reaper, backup / 'reaper.py')
    shutil.copy2(SOURCE / 'warden/sentinel/reaper.py', live_reaper)
    for name in ['stall-check.py', 'clean-orphan-downloads.py']:
        p = COMPOSE.parent / name; shutil.copy2(p, backup / name)
        s = p.read_text()
        gate = '''\n    # MEDIA_GUARD: no destructive cleanup while intake is pending or DB unreadable.
    import sys
    sys.path.insert(0, "/opt/warden")
    from warden.media_guard.protection import cleanup_allowed
    if not cleanup_allowed():
        log("Media guard protects pending/held downloads; skipping cleanup")
        return
'''
        if '# MEDIA_GUARD:' not in s:
            s = s.replace('def main():\n', 'def main():\n' + gate, 1)
        if '@guarded_cleanup' not in s:
            s = s.replace('def main():', 'import sys\nsys.path.insert(0, "/opt/warden")\nfrom warden.media_guard.protection import guarded_cleanup\n\n@guarded_cleanup\ndef main():', 1)
        p.write_text(s)
    (BASE / 'enabled').write_text(stamp)
    run(['docker', 'compose', '-f', str(COMPOSE), 'up', '-d', 'transmission'])
    run(['systemctl', '--user', 'enable', '--now', 'media-guard.service', 'media-signatures.timer'])
    # Confirm guard process is healthy before restoring applications.
    time.sleep(5)
    health = json.loads((BASE / 'state/health.json').read_text())
    if time.time() - health['checked_at'] > 60 or run(['systemctl', '--user', 'is-active', 'media-guard.service']).strip() != 'active':
        raise RuntimeError('Guard not active; Arr remains stopped')
    run(['docker', 'compose', '-f', str(COMPOSE), 'start', 'sonarr', 'radarr'])
    for _ in range(30):
        try:
            api('radarr','system/status'); api('sonarr','system/status'); break
        except Exception:
            time.sleep(2)
    else:
        raise RuntimeError('Arr startup health failed')
    banned = {'cam','telesync','telecine','workprint','dvd-r','br-disk'}
    changed = []
    for profile in api('radarr', 'qualityprofile'):
        dirty = False
        def filter_items(items):
            nonlocal dirty
            for item in items:
                if item.get('quality', {}).get('name', '').lower() in banned and item.get('allowed'):
                    item['allowed'] = False; dirty = True
                filter_items(item.get('items', []))
                if item.get('items') and not any(x.get('allowed') for x in item['items']) and item.get('allowed'):
                    item['allowed'] = False; dirty = True
        filter_items(profile['items'])
        if dirty:
            api('radarr', 'qualityprofile/' + str(profile['id']), profile, 'PUT'); changed.append(profile['name'])
    print('Cutover complete; profiles tightened:', changed, 'backup:', backup)


def verify():
    cfg = json.loads((BASE / 'config.json').read_text()); rpc = RPC(cfg)
    session = rpc('session-get')
    print('Transmission',session.get('version'), 'download-dir',session.get('download-dir'))
    print('Guard',run(['systemctl','--user','is-active','media-guard.service']).strip())
    print('Health',(BASE / 'state/health.json').read_text())
    for app in ['sonarr','radarr','prowlarr']:
        print(app,api(app,'system/status')['version'])
    print(run(['docker','inspect','transmission','--format','{{range .Mounts}}{{.Destination}} RW={{.RW}} {{end}}']).strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('phase', choices=['prepare','cutover','verify'])
    args = parser.parse_args(); globals()[args.phase]()
