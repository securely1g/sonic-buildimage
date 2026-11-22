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

def program(arm):
    # The real installer recipe rendered this extension and set the selected
    # release. Only its release-independent prefix is input to the host action.
    boundary="\n## Bind docker path\n"
    data=Path('sonic_debian_extension.sh').read_text()
    check(data.count(boundary)==1,'missing native host program boundary')
    release=os.environ['SONIC_IMAGE_VERSION']
    key=hashlib.sha256(data.split(boundary)[0].encode()).hexdigest()
    obj={'host_program':key,'release':release}
    write(PUBLIC/arm/'rendered-host-program.json',obj)
    if arm=='seed-a':
        ref=read(PUBLIC/'bootstrap-b/rendered-host-program.json')
        check(ref['host_program']==key and ref['release']!=release,'host prefix changed across A/B renderer')
    return obj


def seed_done():
    arm='seed-a'
    obj=read(PUBLIC/arm/'rendered-host-program.json')
    receipt=read(Path('artifacts/vs/configured-host-cache.json'))
    check(os.environ['SONIC_IMAGE_VERSION']==obj['release'],'configured action release mismatch')
    # This digest matches the driver's independent fingerprint of the renderer.
    recorded=hashlib.sha256((json.dumps(obj['host_program'])+'\n').encode()).hexdigest()
    check(receipt['input_sha256']['host_program_sha256']==recorded,'host program identity mismatch')
    check(receipt['cache_hit'] is False and Path('target/sonic-vs.configured-host.squashfs').is_file(),'seed must produce a cold configured host')
    write(PUBLIC/arm/'cache-receipt.json',receipt)
    write(PUBLIC/arm/'seed-boundary.json',dict(release=obj['release'],configured_host_key=receipt['identity_sha256'],expected_shell_status=85))


HOOK='''if [[ ${0:-} == */build_debian.sh && ${RFS_SPLIT_LAST_STAGE:-} == y && -n ${SONIC_BENCH_ARM:-} ]]; then
    if [[ $SONIC_BENCH_ARM == seed-a || $SONIC_BENCH_ARM == bootstrap-b ]]; then
        /usr/bin/python3 -B /sonic/.configured-host-bench/inside.py program "$SONIC_BENCH_ARM" || exit $?
    fi
    if [[ $SONIC_BENCH_ARM == seed-a ]]; then
        python3() {
            if [[ $# -eq 2 && $1 == -B && $2 == tools/bazel/ci/vs_configured_host.py ]]; then
                command python3 "$@" || exit $?
                /usr/bin/python3 -B /sonic/.configured-host-bench/inside.py seed-done || exit $?
                exit 85
            fi
            command python3 "$@"
        }
    fi
fi
'''


def mount_table():
    result=[]
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left,right=line.split(' - ',1); fields=left.split()
        result.append((Path(fields[4]),fields[2],Path(fields[3]),fields[6:],right.split()[0]))
    return result


def release_benchmark_root(arm, hostns, slot):
    # build_debian.sh self-binds fsroot. Production starts a new container per
    # invocation; our benchmark deliberately keeps the same native container.
    root=ROOT/'fsroot'
    check(ROOT==Path('/sonic') and Path.cwd()==ROOT and ROOT.resolve()==ROOT and root.resolve()==root,
          'unexpected retained installer root')
    check(SHARED.resolve()==SHARED and os.geteuid()>0 and hostns.isdecimal() and
          os.stat('/proc/self/ns/mnt').st_ino!=int(hostns),'expected a private benchmark container namespace')
    scopes=mount_table()
    workspace=[m for m in scopes if m[0]==ROOT]
    check(len(workspace)==1 and not any(v.startswith('shared:') for v in workspace[0][3]),
          'unexpected shared workspace mount')
    nested=[m for m in scopes if m[0]==root or root in m[0].parents]
    # Only the exact known Make self-bind is expected to survive a completed
    # installer. Any child or foreign mount is a reason to leave it untouched.
    check(all(m[0]==root and m[1]==workspace[0][1] and m[2]==workspace[0][2]/'fsroot' and
              m[4]==workspace[0][4] and not any(v.startswith('shared:') for v in m[3]) for m in nested),
          'unexpected mounted filesystem under retained installer root')
    start=time.monotonic()
    for unused in nested:
        subprocess.run(['sudo','-n','umount','--',str(root)],check=True)
    check(not any(m[0]==root or root in m[0].parents for m in mount_table()),'installer root still mounted')
    if root.exists():
        check(root.is_dir() and not root.is_symlink(),'invalid retained installer root')
        # Move the previous private scratch tree aside atomically. Every timed
        # recipe starts with an empty destination. The normal workflow teardown
        # frees these trees along with the rest of this one job's workspace.
        retire=SHARED/('retired-fsroot-'+arm+'-'+slot)
        check(not os.path.lexists(retire),'retained installer root destination exists')
        subprocess.run(['sudo','-n','mv','-T','--',str(root),str(retire)],check=True)
    write(PUBLIC/arm/('fixture-cleanup-'+slot+'.json'),dict(self_binds=len(nested),
          elapsed_seconds=time.monotonic()-start,separate_mount_namespace=True,runner_host_mounts=0))


def run(arm,version):
    if arm in ('restored-b','paired-control-b','changed-deb-b'):
        release_benchmark_root(arm,os.environ.get('SONIC_HOSTBENCH_CALLER_MNTNS',''),os.environ.get('SONIC_HOSTBENCH_RECOVERY_SLOT','0'))
    state=read(PRIVATE/'native.json')
    os.chdir(state['cwd']); resume=os.environ.get('SONIC_HOSTBENCH_INNER_RESUME',''); os.environ.clear();os.environ.update(state['env']); os.environ['SONIC_HOSTBENCH_INNER_RESUME']=resume
    # The exact inputs are physically retained in the native container.
    # Each arm supplies its own public tag and whether host preparation is cached.
    switch='n' if arm in ('bootstrap-b','paired-control-b') else 'y'
    os.environ.update(SONIC_IMAGE_VERSION=version,SONIC_VS_CONFIGURED_HOST_CACHE=switch,SONIC_DIAG_NATIVE_REPLAY='1')
    os.environ.pop('SONIC_VS_RFS_HANDOFF_SESSION',None)
    (SHARED/'hook.bash').write_text(HOOK)
    os.environ.update(BASH_ENV=str(SHARED/'hook.bash'),SONIC_BENCH_ARM=arm)
    resource.setrlimit(resource.RLIMIT_NOFILE,tuple(state['limits']))
    args=[a for a in state['argv'] if not a.startswith(('SONIC_IMAGE_VERSION=','SONIC_VS_CONFIGURED_HOST_CACHE='))]
    args[0]='/usr/bin/make'
    args+=['-f',str(OBS),'SONIC_IMAGE_VERSION='+version,'SONIC_VS_CONFIGURED_HOST_CACHE='+switch]
    args.extend(('-o',BASE))
    stamp=PUBLIC/arm;stamp.mkdir(parents=True,exist_ok=True)
    if arm!='bootstrap-b':
        baseline=read(SHARED/'B.json')
        changed=read(stamp/'package-perturbation.json')['modified_sha256'] if arm=='changed-deb-b' else None
        frozen(args,baseline,arm,changed)
    old=stamp/'inner-make-seconds.json'
    can_resume=os.environ.get('SONIC_HOSTBENCH_INNER_RESUME') == '1' and old.exists() and read(old).get('exit')==0 and Path(INSTALLER).is_file()
    if not can_resume:
        start=time.monotonic();result=subprocess.run(args).returncode
        seconds=time.monotonic()-start
        if arm=='seed-a':
            if result==2 and (stamp/'seed-boundary.json').is_file() and not Path(INSTALLER).exists():
                write(old,{'seconds':seconds,'exit':0,'expected_make_exit':2,'scope':'host action only'})
                return 0
        write(old,{'seconds':seconds,'exit':result,'scope':'native installer'})
        if result: return result
    if arm=='bootstrap-b': snapshot(arm,read_inputs(),'B')
    return 0

if __name__=='__main__':
    if sys.argv[1]=='capture':capture()
    elif sys.argv[1]=='program':program(sys.argv[2])
    elif sys.argv[1]=='seed-done':seed_done()
    else:sys.exit(run(*sys.argv[2:]))
