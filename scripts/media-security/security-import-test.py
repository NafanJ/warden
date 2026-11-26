import json,os,subprocess,time,urllib.request,urllib.error,xml.etree.ElementTree as ET,shutil
from pathlib import Path
BASE=Path('/opt/media-server/security')
config=BASE/('test-radarr-'+str(int(time.time())));config.mkdir(exist_ok=True)
root=Path('/mnt/Modi/media-security-test');root.mkdir(exist_ok=True)
(root/'movies').mkdir(exist_ok=True);(root/'downloads').mkdir(exist_ok=True)
source=Path('/mnt/Modi/Kodi/downloads/complete/movies/Media.Guard.Test.2026.1080p.WEB-DL')
folder=root/'downloads/Big.Buck.Bunny.2008.1080p.WEB-DL';folder.mkdir(exist_ok=True)
video=folder/'Big.Buck.Bunny.2008.1080p.WEB-DL.mp4';subtitle=video.with_suffix('.srt')
for ext,target in [('.mp4',video),('.srt',subtitle)]:
 if not target.exists():os.link(next(source.glob('*'+ext)),target)
# Radarr deliberately rejects one-second silent samples. Use a 10-minute,
# tiny generated clip with audio for the import check; detach our fixture link first.
video.unlink()
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=160x90:r=1:d=600',
 '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','600','-c:v','libx264','-preset','ultrafast',
 '-c:a','aac','-b:a','32k','-shortest','-y',str(video)],check=True)
cmd=['docker','run','-d','--name','media-guard-radarr-test','--label','com.centurylinklabs.watchtower.enable=false',
     '--memory','768m','--cpus','1','-e','PUID=1000','-e','PGID=1000','-e','TZ=Europe/London',
     '-p','127.0.0.1:17878:7878','-v',str(config)+':/config','-v',str(root)+':/data','lscr.io/linuxserver/radarr:latest']
r=subprocess.run(cmd,capture_output=True,text=True);assert r.returncode==0,r.stderr
try:
 for _ in range(60):
  try:
   key=ET.parse(config/'config.xml').findtext('ApiKey')
   urllib.request.urlopen('http://127.0.0.1:17878/ping',timeout=2);break
  except Exception:time.sleep(1)
 def api(path,body=None,method=None):
  req=urllib.request.Request('http://127.0.0.1:17878/api/v3/'+path,data=json.dumps(body).encode() if body is not None else None,
      headers={'X-Api-Key':key,'Content-Type':'application/json'},method=method)
  with urllib.request.urlopen(req,timeout=60) as r:return json.load(r)
 print('Isolated Radarr healthy',flush=True)
 media=api('config/mediamanagement');media['copyUsingHardlinks']=True;media['importExtraFiles']=True;media['extraFileExtensions']='srt'
 api('config/mediamanagement',media,'PUT')
 api('rootfolder',{'path':'/data/movies'},'POST')
 profiles=api('qualityprofile')
 movie=api('movie/lookup?term=tmdb%3A10378')[0]
 movie.update({'qualityProfileId':profiles[0]['id'],'rootFolderPath':'/data/movies','monitored':False,'addOptions':{'searchForMovie':False}})
 movie=api('movie',movie,'POST');print('Added isolated fixture record',movie['title'],flush=True)
 # Exercise Arr's downloaded-media import path with copying/hardlinks, never a production movie.
 command=api('command',{'name':'DownloadedMoviesScan','path':'/data/downloads/'+folder.name,'importMode':'copy'},'POST')
 for _ in range(60):
  state=api('command/'+str(command['id']))
  if state['status'] in ['completed','failed']:break
  time.sleep(1)
 m=api('movie/'+str(movie['id']))
 print('Scan status',state['status'],'hasFile',m['hasFile'],flush=True)
 if not m['hasFile']:
  rows=api('manualimport?folder=%2Fdata%2Fdownloads%2F'+folder.name+'&filterExistingFiles=false')
  print('Import diagnostics',[{'path':r.get('path'),'rejections':r.get('rejections')} for r in rows]);raise RuntimeError('No import')
 mf=api('moviefile/'+str(m['movieFile']['id']))
 imported=root/Path(mf['path']).relative_to('/data')
 assert imported.stat().st_ino==video.stat().st_ino
 assert video.exists()
 print('PASS actual Radarr import shares inode',video.stat().st_ino,'and retains download',flush=True)
 # Repeating the import must not create a second movie file.
 again=api('command',{'name':'DownloadedMoviesScan','path':'/data/downloads/'+folder.name,'importMode':'copy'},'POST')
 for _ in range(30):
  state=api('command/'+str(again['id']))
  if state['status'] in ['completed','failed']:break
  time.sleep(1)
 files=api('moviefile?movieId='+str(movie['id']))
 assert len(files)==1
 print('PASS repeated import creates no duplicate; subtitles',len(list(imported.parent.glob('*.srt'))),flush=True)
 (BASE/'state/import-test.json').write_text(json.dumps({'passed':True,'inode':video.stat().st_ino,'subtitle_imported':bool(list(imported.parent.glob('*.srt'))),'at':time.time()}))
finally:
 subprocess.run(['docker','rm','-f','media-guard-radarr-test'],capture_output=True,check=True)
