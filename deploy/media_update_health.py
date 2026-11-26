#!/usr/bin/env python3
"""Read-only patch/updater visibility; leaves updates to existing Watchtower."""
import datetime
import json
from pathlib import Path
import re
import subprocess

BASE = Path('/opt/media-server/security')
SERVICES = ['transmission','plex','sonarr','radarr','prowlarr']

def run(cmd):
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
    if r.returncode:raise RuntimeError(cmd[0]+' failed')
    return r.stdout


def main():
    result={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'containers':{},'warnings':[]}
    for name in SERVICES:
        try:
            info=json.loads(run(['docker','inspect',name]))[0]
            image=json.loads(run(['docker','image','inspect',info['Config']['Image']]))[0]
            result['containers'][name]={'running':info['State']['Running'],'current_image':info['Image'],
                                        'fetched_image':image['Id'],'restart_for_update':info['Image']!=image['Id']}
        except Exception:
            result['warnings'].append(name+': version/health check failed')
    try:
        r=subprocess.run(['docker','logs','--since','24h','watchtower'],capture_output=True,text=True,timeout=30)
        if r.returncode:raise RuntimeError()
        logs=r.stdout+r.stderr
        failures=set(re.findall(r'(?:Could not pull image|Update failed).*?container=([^\s]+)',logs))
        result['watchtower_pull_failures']=sorted(failures)
        result['watchtower_recent_session']='Update session completed' in logs
        if failures:result['warnings'].append('Watchtower could not pull: '+', '.join(sorted(failures)))
        if not result['watchtower_recent_session']:result['warnings'].append('No completed Watchtower update session in 24 hours')
    except Exception:result['warnings'].append('Watchtower log check failed')
    for package in ['docker-ce','ffmpeg','bubblewrap','clamav']:
        policy=run(['apt-cache','policy',package])
        installed=re.search(r'Installed: (.+)',policy)
        candidate=re.search(r'Candidate: (.+)',policy)
        result[package]={'installed':installed.group(1) if installed else None,'candidate':candidate.group(1) if candidate else None}
    result['scanner_runtime']='ClamAV extracted Ubuntu packages; update with deploy/install_media_guard.py prepare after apt download'
    p=BASE/'state/update-health.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(result,indent=2));tmp.replace(p)
    print(json.dumps({'warnings':result['warnings'],'pending_container_restarts':[k for k,v in result['containers'].items() if v['restart_for_update']]}))

if __name__=='__main__':main()
