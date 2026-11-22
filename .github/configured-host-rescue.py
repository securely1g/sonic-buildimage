"""Publish bounded native failure evidence and resume this live benchmark at a pinned diagnostic revision."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path.cwd()
SHARED = ROOT / ".configured-host-bench"
STATE = SHARED / "rescue"
EVIDENCE = ROOT / "artifacts/vs/configured-host-benchmark"
PUBLIC = ROOT / "artifacts/vs/configured-host-errors"
PRIVATE = ROOT / "artifacts/vs/.configured-host-benchmark-private"
BASE_DRIVER = Path(__file__).resolve().parent
REMOTE = "https://api.github.com/repos/securely1g/sonic-buildimage"
BRANCH = "bench/vs-configured-host-ab"
FILES = ("configured-host-benchmark.py", "configured-host-inside.py")
ARMS = ("bootstrap-b", "seed-a", "restored-b", "paired-control-b", "changed-deb-b")
HEX = re.compile(r"[a-f0-9]{40}\Z")
def regular(path):
    return path.is_file() and not path.is_symlink()

def safe_data(path):
    if not regular(path) or path.stat().st_size>1048576: return {}
    try:
        return json.loads(path.read_bytes())
    except (OSError,ValueError):
        return {}

def atomic(path, data):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp=Path(stream.name)
        try:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True); raise
    os.replace(temp,path)

def put(path,data):
    atomic(path,(json.dumps(data,sort_keys=True,indent=2)+'\n').encode())

def commit(ref):
    return subprocess.check_output(['git','rev-parse',ref],text=True).strip()

def script_revision():
    return safe_data(STATE/'harness.json').get('sha') or os.environ['GITHUB_SHA']

def output_path(arm,slot):
    return PUBLIC/f"{arm}-{slot}.json"

def report(arm, slot, start, code):
    detail=EVIDENCE/arm/'failure.json'
    # This scanner is pinned to the dispatched diagnostic workflow. It emits only
    # approved fields (failure categories, targets, statuses and SHA fingerprints).
    import configured_host_errors as errors
    start_ns=int(start*1_000_000_000)
    info=errors.collect(ROOT,arm,RuntimeError("arm exited with a nonzero status"),
                        EVIDENCE/arm,PRIVATE/(arm+'.log'),started_ns=start_ns,stage=arm)
    if errors.current(detail,start_ns):
        saved=errors.safe_json(detail,ROOT)
        if isinstance(saved,dict):
            # Keep the driver's exception category only if recognized by the
            # same fixed vocabulary used by the scanner.
            category=saved.get('reason',{}).get('error') if isinstance(saved.get('reason'),dict) else None
            if category in {description for _,description in errors.CLASSES}:
                info['reason']={'error':category}
    info.update(arm=arm,slot=slot,source_commit=commit('HEAD'),diagnostic_commit=script_revision(),
                driver_exit=code,duration_seconds=round(max(0,time.time()-start),2))
    put(output_path(arm,slot),info)
    print(f"{arm} driver exited {code}. Safe evidence is ready at {output_path(arm,slot).name}.",flush=True)

def run(arm,slot):
    env=dict(os.environ)
    for key in ('GH_TOKEN','GITHUB_TOKEN','SONIC_RESCUE_API_TOKEN'): env.pop(key,None)
    rev=script_revision()
    driver=BASE_DRIVER/'configured-host-benchmark.py' if rev==env['GITHUB_SHA'] else STATE/rev/'configured-host-benchmark.py'
    if slot: env.update(SONIC_HOSTBENCH_RESUME='1',SONIC_HOSTBENCH_RESUME_SLOT=str(slot))
    start=time.time()
    base=STATE/f'{arm}-start.json'
    if slot==0:
        put(base,{'source_commit':commit('HEAD')})
    elif commit('HEAD')!=safe_data(base).get('source_commit'):
        raise ValueError('source checkout changed before rescue')
    code=subprocess.run([sys.executable,'-B',str(driver),arm],env=env).returncode
    if code:
        if not regular(STATE/f'{arm}-deadline.json'): put(STATE/f'{arm}-deadline.json',{'deadline':time.time()+90*60})
        try:
            report(arm,slot,start,code)
        except (OSError,KeyError,TypeError,ValueError,ImportError) as error:
            # A diagnostic scanner failure must leave a recoverable arm.
            put(output_path(arm,slot),{'arm':arm,'slot':slot,'driver_exit':code,'diagnostic_error':type(error).__name__})
    return code

def api(endpoint):
    request=urllib.request.Request(REMOTE+'/'+endpoint,headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+os.environ['SONIC_RESCUE_API_TOKEN'],'User-Agent':'native-vs-host-benchmark'})
    with urllib.request.urlopen(request,timeout=12) as response:
        body=response.read(2*1024*1024+1)
    if len(body)>2*1024*1024: raise ValueError('oversize GitHub metadata')
    return json.loads(body)

def ref(name):
    item=api('git/ref/heads/'+urllib.parse.quote(name,safe='/'))
    if item.get('ref')!='refs/heads/'+name or item.get('object',{}).get('type')!='commit': raise ValueError('invalid diagnostic ref')
    sha=item['object']['sha']
    if not HEX.fullmatch(sha): raise ValueError('invalid diagnostic commit')
    return sha

def checked_diff(base,head,restricted):
    comparison=api(f'compare/{base}...{head}')
    if comparison.get('status') not in ('ahead','identical') or comparison.get('merge_base_commit',{}).get('sha')!=base:
        raise ValueError('diagnostic commit ancestry did not verify')
    if restricted:
        paths={'.github/'+f for f in FILES}
        for item in comparison.get('files',[]):
            if item.get('filename') not in paths or item.get('status')!='modified':
                raise ValueError('diagnostic repair changes a file outside the two executable drivers')

def checked_files(candidate):
    for name in FILES:
        item=api('contents/.github/'+name+'?ref='+candidate)
        if (item.get('name')!=name or item.get('path')!='.github/'+name or item.get('type')!='file'
                or 'target' in item or item.get('encoding')!='base64' or not 0<item.get('size',0)<200000):
            raise ValueError('unexpected diagnostic file')
        data=base64.b64decode(''.join(item['content'].split()),validate=True)
        git_id=hashlib.sha1(f'blob {len(data)}\0'.encode()+data,usedforsecurity=False).hexdigest()
        if item['size']!=len(data) or item['sha']!=git_id: raise ValueError('diagnostic blob verification failed')
        compile(data,name,'exec')
        yield name,data

def wait_for_fix(arm,slot):
    env=os.environ
    if env.get('GITHUB_REPOSITORY')!='securely1g/sonic-buildimage' or env.get('GITHUB_REF')!='refs/heads/'+BRANCH or not HEX.fullmatch(env['GITHUB_SHA']):
        raise ValueError('expected the trusted diagnostic workflow')
    if not all(re.fullmatch('[1-9][0-9]*',env[k]) for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT')): raise ValueError('invalid Actions run')
    src=safe_data(STATE/f'{arm}-start.json').get('source_commit')
    if src!=commit('HEAD'): raise ValueError('production checkout changed')
    base=script_revision(); deadline=safe_data(STATE/f'{arm}-deadline.json').get('deadline',0)
    selector=f"bench/vs-host-rescue-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}-{arm}-{slot}"
    print(f"Awaiting refs/heads/{selector} until {time.strftime('%H:%M:%SZ',time.gmtime(deadline))}.",flush=True)
    last=''
    while time.time()<deadline:
        try:
            candidate=ref(selector)
            # Allow re-selecting the same SHA (retry unchanged code); require
            # every other SHA to be an advance reachable on the owner's branch.
            checked_diff(base,candidate,True)
            if base!=env['GITHUB_SHA']: checked_diff(env['GITHUB_SHA'],candidate,True)
            checked_diff(candidate,ref(BRANCH),False)
            downloads=dict(checked_files(candidate))
            for name,data in downloads.items(): atomic(STATE/candidate/name,data)
            atomic(SHARED/'inside.py',downloads['configured-host-inside.py'])
            put(STATE/'harness.json',{'sha':candidate})
            put(EVIDENCE/arm/f'rescue-{slot}.json',{'diagnostic_commit':candidate,'arm':arm,'source':src})
            print(f"Selected immutable diagnostic revision {candidate}.",flush=True)
            return 0
        except urllib.error.HTTPError as error: outcome=f"GitHub HTTP {error.code}"
        except (OSError,ValueError,KeyError,TypeError,SyntaxError) as error: outcome=type(error).__name__
        if outcome!=last and outcome!='GitHub HTTP 404': print(f"Selection temporarily unavailable ({outcome}).",flush=True)
        last=outcome
        time.sleep(max(0,min(35,deadline-time.time())))
    print(f"No qualifying pinned fix selected for {arm} within its 90-minute recovery window.",flush=True)
    return 1

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('op',choices=('run','wait'))
    parser.add_argument('arm',choices=ARMS)
    parser.add_argument('slot',type=int,choices=(0,1,2))
    args=parser.parse_args()
    if args.op=='wait' and not args.slot: parser.error('a retry slot is required')
    sys.exit(run(args.arm,args.slot) if args.op=='run' else wait_for_fix(args.arm,args.slot))
