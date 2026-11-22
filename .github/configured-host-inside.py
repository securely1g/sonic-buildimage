"""Run a real Make installer target in the benchmark's single native build slave."""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import time

ROOT = Path('/sonic')
SHARED = ROOT / '.configured-host-bench'
PRIVATE = Path('/tmp/.configured-host-benchmark-args')
PUBLIC = ROOT / 'artifacts/vs/configured-host-benchmark'
OBS = SHARED / 'observe.mk'
INSTALLER = 'target/sonic-vs.bin'
BASE = 'target/sonic-vs.bin__vs__rfs.squashfs'

def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True))
def read(path): return json.loads(path.read_bytes())
def check(ok,msg):
    if not ok: raise RuntimeError(msg)
def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def stat_id(p):
    s=p.stat(); return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

def capture():
    # GNU Make calls us while reading the diagnostic-only supplemental makefile.
    # Read the real Make argv and original environment without putting either
    # in the job workspace, the shell log or an artifact.
    pid=os.getppid()
    args=[]
    for _ in range(8):
        process=Path('/proc')/str(pid)
        args=[os.fsdecode(x) for x in (process/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
        if Path(args[0]).name in ('make','gmake') and 'slave.mk' in args and INSTALLER in args: break
        parent=[l for l in (process/'status').read_text().splitlines() if l.startswith('PPid:')]
        if not parent or pid<2: raise RuntimeError('missing native slave.mk process')
        pid=int(parent[0].split()[1])
    else: raise RuntimeError('native Make parent not found')
    saved_env=dict(os.fsdecode(item).split('=',1) for item in (process/'environ').read_bytes().split(b'\0') if item)
    check(os.getcwd()==str(ROOT) and os.readlink(process/'cwd')==str(ROOT),'unexpected native Make worktree')
    PRIVATE.mkdir(mode=0o700)
    write(PRIVATE/'native.json',dict(argv=args,env=saved_env,cwd=os.getcwd(),limits=resource.getrlimit(resource.RLIMIT_NOFILE)))
    OBS.write_text('target/sonic-vs.bin: private HEADER += $(file >.configured-host-bench/selected,$^) $(file >.configured-host-bench/handoff,$(SONIC_VS_BOOT_HANDOFF))\n')
    write(SHARED/'ready.json',{'uid':os.geteuid()})
    while True: time.sleep(1)

def read_inputs():
    return (SHARED/'selected').read_text().split()

def valued(path):
    # Special -install prerequisites describe the package installed in this same slave.
    return Path(path.removesuffix('-install'))

def host_files(name):
    return name.endswith(('.deb','.whl','.squashfs','-install')) and name.startswith('target/')

def selected(name):
    return (name.startswith('target/') and name not in (
        'target/.installer-docker-load-on-boot','target/.vs-configured-host-release',INSTALLER))

def snapshot(arm, names, label):
    names=sorted(n for n in names if selected(n))
    sys.path.insert(0,str(ROOT))
    from tools.bazel.oci.boot_layout import direct_layout_for_archive
    values={}
    for name in names:
        p=valued(name)
        # Do not mark missing producer outputs as old. Every nonphysical target
        # allowed later is the installation of a real selected package above.
        check(p.is_file(), f'installer direct prerequisite missing: {name}')
        values[name]={'stat':stat_id(p),'sha256':digest(p)}
    hand=(SHARED/'handoff').read_text().strip()
    check(re.fullmatch(r'[0-9a-f-]{36}',hand), 'missing current OCI handoff')
    receipts={}
    for n in names:
        if not n.endswith('.gz') or not Path(n).name.startswith('docker-'): continue
        side=Path('target/.boot-image-layouts')/(Path(n).name+'.direct.json')
        check(side.is_file(), 'missing native OCI layout receipt for '+n)
        data=read(side)
        check(data.get('handoff')==hand and direct_layout_for_archive(Path(n),hand) is not None,'unusable native OCI handoff for '+n)
        receipts[n]=digest(side)
    check(len(receipts)==28, 'incomplete native OCI handoff set')
    result=dict(names=names,values=values,handoff=hand,layouts=receipts)
    write(SHARED/(label+'.json'),result)
    # Byte fingerprints permit investigating cross-checkout drift without
    # publishing environments, package contents or cache images.
    fingerprints={n:d['sha256'] for n,d in values.items() if n.endswith(('.deb','.whl','.squashfs'))}
    for p in Path('target/debs/trixie').glob('*.deb'):
        if str(p) not in fingerprints and p.is_file(): fingerprints[str(p)]=digest(p)
    write(PUBLIC/arm/'package-digests.json',fingerprints)
    report=Path('artifacts/vs/host-base-cache.json')
    if report.is_file():
        obj=read(report); write(PUBLIC/arm/'base-input.json',{k:obj.get(k) for k in ('identity_sha256','cache_hit','outputs')})
    write(PUBLIC/arm/'observed-oci.json',{'images':len(receipts),'receipt_sha256':hashlib.sha256(json.dumps(receipts,sort_keys=True).encode()).hexdigest()})

def frozen(args, baseline, arm, changed):
    names=[]
    for n, saved in baseline['values'].items():
        if arm=='version-b-control' and not host_files(n):continue
        p=valued(n)
        wanted=changed if changed and str(p)=='target/debs/trixie/sonic-utilities-data_1.0-1_all.deb' else saved['sha256']
        if stat_id(p)!=saved['stat'] or wanted!=saved['sha256']:
            check(digest(p)==wanted,f'frozen input changed: {n}')
        names.append(n)
    if arm!='version-b-control':
        sys.path.insert(0,str(ROOT))
        from tools.bazel.oci.boot_layout import direct_layout_for_archive
        for n, dig in baseline['layouts'].items():
            check(digest(Path('target/.boot-image-layouts')/(Path(n).name+'.direct.json'))==dig and direct_layout_for_archive(Path(n),baseline['handoff']) is not None, 'frozen OCI receipt changed '+n)
        # Reuse only this job's current, verified B producer session. The
        # native bundle still checks each archive's identity against its layout.
        args.append('vs_boot_handoff='+baseline['handoff'])
    for n in names:args.extend(('-o',n))

def run(arm,version):
    state=read(PRIVATE/'native.json')
    os.chdir(state['cwd']); os.environ.clear();os.environ.update(state['env'])
    # The exact inputs are physically retained in the native container.
    # Each arm supplies its own public tag and whether host preparation is cached.
    switch='n' if arm in ('version-b-control','paired-control-b') else 'y'
    os.environ.update(SONIC_IMAGE_VERSION=version,SONIC_VS_CONFIGURED_HOST_CACHE=switch,SONIC_DIAG_NATIVE_REPLAY='1')
    os.environ.pop('SONIC_VS_RFS_HANDOFF_SESSION',None)
    resource.setrlimit(resource.RLIMIT_NOFILE,tuple(state['limits']))
    args=[a for a in state['argv'] if not a.startswith(('SONIC_IMAGE_VERSION=','SONIC_VS_CONFIGURED_HOST_CACHE='))]
    args[0]='/usr/bin/make'
    args+=['-f',str(OBS),'SONIC_IMAGE_VERSION='+version,'SONIC_VS_CONFIGURED_HOST_CACHE='+switch]
    args.extend(('-o',BASE))
    stamp=PUBLIC/arm;stamp.mkdir(parents=True,exist_ok=True)
    if arm!='seed-a':
        baseline=read(SHARED/('A.json' if arm=='version-b-control' else 'B.json'))
        changed=read(stamp/'package-perturbation.json')['modified_sha256'] if arm=='changed-deb-b' else None
        frozen(args,baseline,arm,changed)
    start=time.monotonic();result=subprocess.run(args).returncode
    write(stamp/'inner-make-seconds.json',{'seconds':time.monotonic()-start,'exit':result})
    if result: return result
    if arm in ('seed-a','version-b-control'): snapshot(arm,read_inputs(),'A' if arm=='seed-a' else 'B')
    return 0

if __name__=='__main__':
    if sys.argv[1]=='capture':capture()
    else:sys.exit(run(*sys.argv[2:]))
