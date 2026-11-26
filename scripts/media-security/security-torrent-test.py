import sys,json,base64,hashlib,subprocess,time,sqlite3,os
from pathlib import Path
sys.path.insert(0,'/opt/warden')
from warden.media_guard.core import RPC
cfg=json.loads(Path('/opt/media-server/security/config.json').read_text());rpc=RPC(cfg)
NAME='Media.Guard.Test.2026.1080p.WEB-DL'
if len(sys.argv)>1 and sys.argv[1]=='resume':
 h=Path('/tmp/media-guard-test-hash').read_text()
 root=Path(cfg['incoming'])/'complete/movies'/NAME
 subprocess.run(['/usr/bin/python3','-m','warden.media_guard','retry',h],cwd='/opt/warden',check=True)
else:
 root=Path(cfg['incoming'])/'complete/movies'/NAME
 root.mkdir(exist_ok=True)
 v=root/(NAME+'.mp4')
 subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=160x90:d=1','-c:v','libx264','-y',str(v)],check=True)
 (root/(NAME+'.srt')).write_text('1\n00:00:00,000 --> 00:00:00,900\nGuard integration test\n')
 def enc(x):
  if isinstance(x,int):return b'i'+str(x).encode()+b'e'
  if isinstance(x,str):return enc(x.encode())
  if isinstance(x,bytes):return str(len(x)).encode()+b':'+x
  if isinstance(x,list):return b'l'+b''.join(map(enc,x))+b'e'
  return b'd'+b''.join(enc(k)+enc(x[k]) for k in sorted(x))+b'e'
 files=sorted(root.iterdir());data=b''.join(p.read_bytes() for p in files);piece=16384
 info={'name':NAME,'piece length':piece,'pieces':b''.join(hashlib.sha1(data[i:i+piece]).digest() for i in range(0,len(data),piece)),
       'files':[{'length':p.stat().st_size,'path':[p.name]} for p in files],'private':1}
 h=hashlib.sha1(enc(info)).hexdigest()
 Path('/tmp/media-guard-test-hash').write_text(h)
 rpc('torrent-add',{'metainfo':base64.b64encode(enc({'info':info})).decode(),'paused':True,'download-dir':'/quarantine/complete/movies'})
 rpc('torrent-verify',{'ids':[h]})
print('TEST HASH',h,flush=True)
for _ in range(90):
 with sqlite3.connect(Path(cfg['state_dir'])/'guard.sqlite3') as db:
  row=db.execute('SELECT state,reason FROM jobs WHERE hash=?',(h,)).fetchone()
 if row and row[0] in ['released','held']:
  print('GUARD',row,flush=True);break
 time.sleep(2)
else:raise RuntimeError('Test guard timeout')
if row[0]!='released':raise RuntimeError('Test held')
t=rpc('torrent-get',{'ids':[h],'fields':['downloadDir','percentDone','error','errorString','status']})['torrents'][0]
print('TRANSMISSION',t)
assert t['downloadDir']=='/data/downloads/complete/movies' and not t['error']
approved=Path(cfg['approved'])/'movies'/NAME
assert approved.exists() and not root.exists()
for container,cmd in [('transmission',['test','!','-e','/data/Film']),('sonarr',['test','!','-e','/quarantine']),('radarr',['test','!','-e','/quarantine'])]:
 assert subprocess.run(['docker','exec',container]+cmd).returncode==0
r=subprocess.run(['docker','exec','transmission','touch','/data/downloads/complete/.guard-write-test'],capture_output=True,text=True)
assert r.returncode!=0 and 'Read-only file system' in r.stderr
print('PASS library isolation, unapproved invisibility, approved read-only')
rpc('torrent-verify',{'ids':[h]})
for _ in range(30):
 time.sleep(1)
 t=rpc('torrent-get',{'ids':[h],'fields':['percentDone','status','error']})['torrents'][0]
 if t['status'] not in [1,2]:break
assert t['percentDone']==1 and not t['error']
print('PASS Transmission re-verifies published files for seeding')
