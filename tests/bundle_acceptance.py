"""Disposable single-container acceptance; no external network or real devices."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('context', type=Path)
    parser.add_argument('prefix')
    args = parser.parse_args()
    if not re.fullmatch(r'pb-acceptance-[a-z0-9-]+', args.prefix):
        parser.error('Use a new pb-acceptance- prefix')
    name = args.prefix + '-bundle'
    image = name
    volume = name + '-data'
    restored = volume + '-restored'
    created = []
    def dk(*cmd, input=None):
        return subprocess.check_output(['docker', *cmd], input=input, stderr=subprocess.STDOUT).decode()
    def execute(code):
        return dk('exec', '-i', name, 'python', '-', input=code.encode())
    def check(label, value=True):
        if not value: raise RuntimeError('Failed: ' + label)
        print('PASS ' + label, flush=True)
    def start(mount):
        dk('run', '-d', '--name', name, '--network', 'none', '--init', '--read-only',
           '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
           '--tmpfs', '/tmp:size=16m,mode=1777', '--restart', 'no',
           '-e', 'BRIDGE_HOST=192.0.2.10', '-e', 'BRIDGE_LISTEN_HOST=127.0.0.1', '-v', mount + ':/data', image)
        created.append(name)
    def healthy():
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            state = json.loads(dk('inspect', name))[0]['State']
            if not state['Running']: raise RuntimeError('Bundle exited')
            if state.get('Health', {}).get('Status') == 'healthy': return
            time.sleep(1)
        raise RuntimeError('Bundle health deadline')
    def fingerprint():
        return execute("from pathlib import Path\nimport hashlib,json\nprint(json.dumps({n:hashlib.sha256((Path('/data')/n).read_bytes()).hexdigest() for n in ['playback/identity.json','handoff/ready.json','exports/playback/credentials.json','setup/setup.json']}))")
    check('resource names unused', not dk('ps','-aq','--filter','name='+name).strip()
          and not dk('volume','ls','-q','--filter','name='+volume).strip()
          and not dk('images','-q',image).strip())
    try:
        subprocess.run(['docker','build','--network=none','--target','bundle','-t',image,str(args.context)], check=True)
        start(volume)
        healthy()
        check('fresh setup and both distinct waiters healthy')
        execute('''
from pathlib import Path
from http.client import HTTPConnection
from urllib.parse import urlencode
import json,secrets,os
assert os.getuid()==10001
assert Path('/tmp/bridge-waiting-playback.json').exists()
assert Path('/tmp/bridge-waiting-topology.json').exists()
cookie=''
def request(path,fields=None,origin='http://127.0.0.1:8795'):
 global cookie
 c=HTTPConnection('127.0.0.1',8795,timeout=10)
 c.request('POST' if fields is not None else 'GET',path,urlencode(fields) if fields is not None else None,
           {'Cookie':cookie,'Origin':origin,'Content-Type':'application/x-www-form-urlencoded'})
 r=c.getresponse()
 if r.getheader('Set-Cookie'): cookie=r.getheader('Set-Cookie').split(';')[0]
 status,body=r.status,r.read().decode();c.close()
 return request('/') if status==303 else (status,body)
code=json.loads(Path('/data/setup/setup-code.json').read_text())['code']
assert request('/claim',{'code':code},'http://example.invalid')[0]==403
assert request('/claim',{'code':code})[0]==200
assert request('/administrator',{'username':'acceptance','password':secrets.token_urlsafe(32)})[0]==200
assert request('/players',{'advertised_origin':'http://192.0.2.10:8794','plex_server_origin':'http://192.0.2.20:32400','rooms':'Synthetic room, 192.0.2.30'})[0]==200
assert 'Local setup is complete' in request('/finish',{})[1]
''')
        execute('''
from pathlib import Path
from urllib.request import urlopen
import time
for port in (8793,8794,8795):
 deadline=time.monotonic()+30
 while True:
  try:
   assert urlopen('http://127.0.0.1:'+str(port)+'/health',timeout=3).status==200
   break
  except OSError:
   if time.monotonic()>deadline: raise
   time.sleep(.25)
assert not Path('/tmp/bridge-waiting-playback.json').exists()
assert not Path('/tmp/bridge-waiting-topology.json').exists()
''')
        check('HTTP claim, setup handoff and all three listeners ready')
        before = fingerprint()
        execute('''
from pathlib import Path
from http.client import HTTPConnection
from plex_bridge.playback import App
import json,re,html
credentials=json.loads(Path('/data/exports/playback/credentials.json').read_text())
cookie='wiim_focus_session='+App(None,'unused','unused',credentials['WEB_SESSION_SECRET']).session_token()
def get(path,auth=False):
 c=HTTPConnection('127.0.0.1',8794,timeout=10);c.request('GET',path,headers={'Cookie':cookie} if auth else {})
 r=c.getresponse();result=r.status,r.read();c.close();return result
assert get('/diagnostics')[0]==303
assert get('/symfonium/library/sections')[0]==401
assert get('/symfonium/:/timeline?X-Plex-Token=synthetic-private')[0]==200
status,page=get('/diagnostics',True);assert status==200
incident=re.search(rb'/diagnostics/download/([0-9a-f]{32})',page).group(1).decode()
status,download=get('/diagnostics/download/'+incident,True);assert status==200
assert html.unescape(re.search(r'<pre[^>]*>(.*?)</pre>',page.decode(),re.S).group(1)).encode()==download
for value in (*credentials.values(),'synthetic-private','Synthetic room','192.0.2.'):
 assert str(value).encode() not in download
assert b'proxy_report_suppressed' in download
''')
        logs = dk('logs', name)
        check('combined logs are structured and private fields excluded',
              all('event' in json.loads(line) for line in logs.splitlines() if line.strip())
              and not any(v in logs for v in ['Synthetic room','synthetic-private','192.0.2.','Traceback']))
        dk('restart', name); healthy()
        check('restart retains all state identities', before == fingerprint())
        # Kill a real child, not the supervisor: all services must stop together.
        execute("from pathlib import Path\nimport os,signal\nfor p in Path('/proc').glob('[0-9]*/cmdline'):\n try:\n  if b'plex_bridge.topology' in p.read_bytes().split(b'\\0'): os.kill(int(p.parent.name),signal.SIGKILL)\n except (ProcessLookupError,FileNotFoundError): pass")
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=json.loads(dk('inspect',name))[0]['State']
            if not state['Running']: break
            time.sleep(.2)
        check('child failure exits entire bundle for Docker restart', not state['Running'] and state['ExitCode']!=0)
        dk('start',name); healthy()
        check('recover after child failure without new identities', before==fingerprint())
        dk('stop','-t','15',name)
        check('normal shutdown exits cleanly', json.loads(dk('inspect',name))[0]['State']['ExitCode']==0)
        dk('volume','create',restored)
        dk('run','--rm','--network','none','--user','0','--entrypoint','sh',
           '-v',volume+':/from:ro','-v',restored+':/to',image,'-c','cd /from && tar cf - . | tar xpf - -C /to')
        dk('rm',name);created.clear();start(restored);healthy()
        check('single-volume stopped restore preserves identities',before==fingerprint())
        dk('stop','-t','15',name);dk('rm',name);created.clear()
        # Test the documented pre-owned appdata-style bind mount on this Linux host.
        bind=(args.context/'artifacts'/'bundle-bind-data').resolve();bind.mkdir(parents=True,exist_ok=False)
        dk('run','--rm','--network','none','--user','0','--entrypoint','sh','-v',str(bind)+':/to',
           '-v',restored+':/from:ro',image,'-c','cd /from && tar cf - . | tar xpf - -C /to')
        start(str(bind));healthy()
        check('Linux appdata-style bind restore preserves identities',before==fingerprint())
        print('Image ID: '+json.loads(dk('image','inspect',image))[0]['Id'],flush=True)
    finally:
        for container in created:
            subprocess.run(['docker','stop','-t','15',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__': main()
