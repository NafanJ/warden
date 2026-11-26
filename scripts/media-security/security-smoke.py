import sys,json,subprocess,os
from pathlib import Path
sys.path.insert(0,'/opt/warden')
from warden.media_guard.core import sandbox,inspect,Hold
cfg=json.loads(Path('/opt/media-server/security/config.json').read_text())
if len(sys.argv)>1 and sys.argv[1]=='fresh-debug':
 r=subprocess.run([cfg['runtime']+'/usr/bin/freshclam','--config-file='+cfg['state_dir']+'/freshclam.conf'],env={'PATH':'/usr/bin:/bin','LD_LIBRARY_PATH':cfg['runtime']+'/usr/lib/x86_64-linux-gnu','CVD_CERTS_DIR':cfg['runtime']+'/etc/clamav/certs'},capture_output=True,text=True,timeout=900)
 print(r.returncode,r.stdout[-3000:],r.stderr[-3000:]);sys.exit()
if len(sys.argv)>1 and sys.argv[1]=='rename':
 from warden.media_guard.core import rename_noreplace
 from warden.media_guard.confinement import confine
 a=Path(cfg['incoming'])/'rename-probe';b=Path(cfg['stage'])/'rename-probe'
 a.mkdir(exist_ok=True)
 confine(cfg)
 try:rename_noreplace(a,b);print('RENAME OK')
 except OSError as e:print('RENAME ERROR',e.errno,e.strerror)
 sys.exit()
root=Path(cfg['stage'])/'guard-fixture';root.mkdir(exist_ok=True)
payload=root/'Fixture';payload.mkdir(exist_ok=True)
v=payload/'fixture.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=160x90:d=1','-c:v','libx264','-y',str(v)],check=True)
(payload/'fixture.srt').write_text('1\n00:00:00,000 --> 00:00:00,900\nTest subtitle\n')
t={'name':'Fixture','files':[{'name':'Fixture/'+p.name,'length':p.stat().st_size} for p in payload.iterdir()]}
if len(sys.argv)>1 and sys.argv[1]=='confined':
 from warden.media_guard.broker import start
 start(cfg)
 from warden.media_guard.confinement import confine
 confine(cfg)
 for forbidden in ['/opt/warden/.env','/opt/appdata/radarr/config.xml','/mnt/Modi/Kodi/Film']:
  try:
   os.listdir(forbidden) if Path(forbidden).is_dir() else open(forbidden).read(1)
  except PermissionError:print('DENIED',forbidden)
  else:raise RuntimeError('Confinement failed')
probe=sandbox(cfg,[(v,'/input')],['/usr/bin/ffprobe','-v','error','-show_format','-show_streams','-of','json','/input']);print('PROBE',probe.returncode,probe.stderr[:1000])
print('GOOD',inspect(cfg,payload,t)['verdict'])
eicar=payload/'test.txt';eicar.write_bytes(b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*')
t['files'].append({'name':'Fixture/test.txt','length':eicar.stat().st_size})
try:inspect(cfg,payload,t)
except Hold as e:print('EICAR HELD',str(e))
else:raise RuntimeError('EICAR NOT HELD')
eicar.unlink()
