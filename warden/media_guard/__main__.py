"""Local owner CLI; no network listener and no LLM calls."""
import argparse
import fcntl
import json
from pathlib import Path
import sys
import time
from .core import Guard, Hold, sandbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/opt/media-server/security/config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['run', 'once', 'list', 'health', 'freshen']:
        sub.add_parser(name)
    for name in ['show', 'retry', 'release']:
        p = sub.add_parser(name)
        p.add_argument('hash')
        if name == 'release':
            p.add_argument('--reason', required=True)
            p.add_argument('--accept-risk', action='store_true', required=True)
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    if args.command == 'freshen':
        # Serialize definition verification with payload scans to avoid competing
        # ClamAV engines and partial definition updates on this small server.
        update_lock = open(Path(cfg['state_dir']) / 'worker.lock', 'a')
        try:
            fcntl.flock(update_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Signature update deferred: inspection is busy', file=sys.stderr)
            return 3
        if not Guard(cfg).scan_window():
            print('Signature update deferred: resource/playback window closed', file=sys.stderr)
            return 3
        import os
        import subprocess
        # Only the signature updater has network access. It never opens downloads.
        r = subprocess.run([str(Path(cfg['runtime']) / 'usr/bin/freshclam'),
            '--config-file=' + str(Path(cfg['state_dir']) / 'freshclam.conf')],
            env={'PATH': '/usr/bin:/bin', 'LD_LIBRARY_PATH': str(Path(cfg['runtime']) / 'usr/lib/x86_64-linux-gnu'), 'CVD_CERTS_DIR': str(Path(cfg['runtime']) / 'etc/clamav/certs')},
            timeout=900, capture_output=True, text=True)
        if r.returncode:
            print('Signature update failed; existing freshness timestamp unchanged', file=sys.stderr)
            return 1
        version = sandbox(cfg, [(cfg['signatures'], '/signatures')],
                          ['/runtime/usr/bin/clamscan', '--database=/signatures', '--version'])
        if version.returncode:
            return 1
        # Check the database's embedded date, not merely whether freshclam ran.
        import datetime
        date = version.stdout.strip().split('/', 2)[-1]
        stamp = datetime.datetime.strptime(date, '%a %b %d %H:%M:%S %Y').replace(tzinfo=datetime.timezone.utc).timestamp()
        if not 0 <= time.time() - stamp <= 48 * 3600:
            print('Signature database date is stale', file=sys.stderr)
            return 1
        p = Path(cfg['signatures']) / 'fresh.json'
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps({'checked_at': time.time(), 'database_at': stamp, 'engine': version.stdout.strip()}))
        os.replace(tmp, p)
        print('Antivirus signatures current:', version.stdout.strip())
        return 0
    if args.command in ('run', 'once'):
        from .broker import start
        start(cfg)
        from .confinement import confine
        confine(cfg)
    guard = Guard(cfg)
    if args.command in ('list', 'show', 'health'):
        if args.command == 'health':
            health = json.loads((Path(cfg['state_dir']) / 'health.json').read_text())
            health['worker_stale'] = time.time() - health['checked_at'] > 120
            health['signatures'] = json.loads((Path(cfg['signatures']) / 'fresh.json').read_text())
            updates = Path(cfg['state_dir']) / 'update-health.json'
            health['updates'] = json.loads(updates.read_text()) if updates.exists() else 'not checked'
            print(json.dumps(health, indent=2))
        else:
            rows = guard.store.db.execute('SELECT * FROM jobs' + (' WHERE hash=?' if args.command == 'show' else ''),
                                          (args.hash.lower(),) if args.command == 'show' else ()).fetchall()
            print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    # Commands share one lock with the worker; operator changes cannot race scans.
    with open(Path(cfg['state_dir']) / 'worker.lock', 'a') as lock:
        if args.command == 'run':
            while True:
                with_lock_tick(guard, lock)
                time.sleep(30)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == 'once':
                guard.tick()
            else:
                row = guard.store.get(args.hash.lower())
                if not row or row['state'] != 'held':
                    raise SystemExit('Only a held job can be retried or explicitly released')
                source, staged, target = guard.paths(row)
                if args.command == 'retry':
                    # Published-but-not-resumed jobs must finish their original transaction.
                    state = 'publishing' if row['manifest'] else ('inspecting' if staged.exists() else 'incoming')
                    guard.store.state(row['hash'], state, 'Owner requested retry')
                else:
                    # No override for path traversal or incomplete/unisolated payloads.
                    from .core import manifest, metadata
                    ts = guard.rpc('torrent-get', {'ids': [row['hash']], 'fields': ['name','files','percentDone']})['torrents']
                    if len(ts) != 1 or ts[0]['percentDone'] != 1 or not staged.exists():
                        raise SystemExit('Release requires a complete, privately staged payload')
                    metadata(ts[0])
                    files = manifest(staged, ts[0])
                    reason = 'Owner override: ' + args.reason
                    guard.store.state(row['hash'], 'publishing', reason, {'files': files, 'verdict': reason})
                    guard.publish(guard.store.get(row['hash']))
    return 0


def with_lock_tick(guard, lock):
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        guard.tick()
    except Exception as exc:
        print('Guard cycle failed:', type(exc).__name__, flush=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == '__main__':
    sys.exit(main())
