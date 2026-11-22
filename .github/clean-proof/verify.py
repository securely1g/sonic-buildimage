#!/usr/bin/env python3
"""Record bounded hash evidence for independently produced SONiC VS installers."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path.cwd()
DEVICE = Path('target/debs/trixie/sonic-device-data_1.0-1_all.deb')
PLATFORM = Path('target/debs/trixie/sonic-platform-vs_1.0_amd64.deb')
DEVICE_TAR = Path('target/files/trixie/sonic-device-data-vs.tar')
BASE = 'host-base-cache.json'
CONFIGURED = 'configured-host-cache.json'

def check(ok,message):
    if not ok: raise ValueError(message)

def read(p):return json.loads(Path(p).read_bytes())

def save(p,obj):
    p.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n')

def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(2**20),b''):h.update(chunk)
    return h.hexdigest()

def listing(values):return hashlib.sha256(json.dumps(sorted(values),separators=(',',':')).encode()).hexdigest()

def cmd(*args):return subprocess.check_output(args, text=True).strip()

def installed(expected,commit):
    sys.path.insert(0,str(ROOT/'scripts'));import validate_sonic_version as validate
    import yaml
    img=Path('target/sonic-vs.bin')
    with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as d, img.open('rb') as binary:
        data=binary.read(65536);marker=b'\nexit_marker\n';offset=data.index(marker)+len(marker);binary.seek(offset)
        with tarfile.open(fileobj=binary,mode='r:') as wrapper,wrapper.extractfile('installer/fs.zip') as f,zipfile.ZipFile(f) as payload:
            squash=Path(d)/'fs.squashfs'
            with payload.open('fs.squashfs') as source,squash.open('wb') as dest:shutil.copyfileobj(source,dest,2**20)
            validate.validate_boot_bundle(wrapper,payload,squash)
            with payload.open('platform.tar.gz') as source,tarfile.open(fileobj=source,mode='r|gz') as platform:
                platform_digest=None
                for item in platform:
                    if item.name.removeprefix('./')=='common/'+PLATFORM.name and item.isfile():
                        platform_digest=hashlib.sha256(platform.extractfile(item).read()).hexdigest()
            check(platform_digest==sha(PLATFORM),'installer did not contain the rebuilt VS platform package')
        def member(name):return subprocess.check_output(['unsquashfs','-cat',squash,name])
        version=yaml.safe_load(member('etc/sonic/sonic_version.yml'));validate.validate_version(version,'no_sign')
        check(expected==version['build_version'] and commit.startswith(version['commit_id']),'installer release/commit mismatch')
        images=json.loads(member('usr/share/sonic/docker-image-inventory.json'))
        latest={name.rsplit(':',1)[0] for name in images if name.endswith(':latest')}
        release={name.rsplit(':',1)[0] for name in images if name.endswith(':'+expected)}
        check(latest==release and len(images)==2*len(latest) and len(latest)>=26,'wrong installed runtime tags')
        inventory=[]
        for entry in member('var/lib/dpkg/status').decode().split('\n\n'):
            fields=dict(line.split(': ',1) for line in entry.splitlines() if line[:1].isalpha() and ': ' in line)
            if fields.get('Status')=='install ok installed':inventory.append((fields['Package'],fields['Architecture'],fields['Version']))
        check(any(item[0]=='sonic-device-data' for item in inventory),'device TAR package missing from dpkg inventory')
    return {'version':expected,'commit':commit,'bytes':img.stat().st_size,'packages':len(inventory),
            'package_inventory_sha256':listing(inventory),'selected_images':sorted(latest),'tags':len(images)}

def packages():
    declared=Path('artifacts/vs/clean-declared.txt').read_text().split()
    check(str(PLATFORM) in declared and str(DEVICE_TAR) in declared and str(DEVICE) not in declared,
          'expected the platform DEB and the device TAR in the installer selection')
    items={Path(n) for n in declared if n.startswith('target/') and n.endswith(('.deb','.whl'))}
    items.update(Path('target/debs/trixie').glob('*.deb'))
    for base in Path('target').glob('*wheel*'):
        items.update(p for p in base.rglob('*.whl') if p.is_file())
    items.update((DEVICE_TAR,PLATFORM,DEVICE))
    return {str(p):sha(p) for p in sorted(items)}

def built(deb):
    check(deb.is_file(),f'missing independently built {deb.name}')
    # Native producer logs hold dpkg-deb output. Publish only a boolean and the
    # final archive hash; full package or build logs stay with this runner.
    log=Path(str(deb)+'.log').read_text(errors='replace')
    package=cmd('dpkg-deb','-f',deb,'Package')
    check('dpkg-deb: building package' in log and package in log and '[ CACHE::HIT ]' not in log,
          f'native producer evidence missing for {deb.name}')
    return {'package':package,'version':cmd('dpkg-deb','-f',deb,'Version'),'sha256':sha(deb),'native_dpkg_deb':True}

def compare(a,b):
    check(a['commit']==b['commit'] and a['runner']==b['runner'] and a['version']!=b['version'],'not an A/B release pair')
    check(a['checkout']!=b['checkout'] and a['local_bazel']!=b['local_bazel'] and a['host_cache']!=b['host_cache'],'checkout or local cache reused')
    check(a['builders'] and b['builders'] and set(a['builders']).isdisjoint(b['builders']),'build container reused')
    for kind in ('base','configured'):
        s,r=a[kind],b[kind]
        check(s['cache_hit'] is False and r['cache_hit'] is True, f'{kind} did not hit across builds')
        check(s['identity_sha256']==r['identity_sha256'] and s['outputs']==r['outputs'], f'{kind} identity/output changed')
    check(a['configured']['input_sha256']==b['configured']['input_sha256'] and b['configured']['native_build_seconds']==0,'configured native action ran in B')
    check(a['tested_producers']==b['tested_producers'], 'corrected package bytes differ across clean builds')
    check(a['package_candidates'].get(str(DEVICE_TAR))==b['package_candidates'].get(str(DEVICE_TAR)), 'native device TAR differs')
    for field in ('packages','package_inventory_sha256','selected_images','tags'):
        check(a['installed'][field]==b['installed'][field],f'installer {field} differs')

def main(role):
    check(role in ('a','b'),'invalid arm')
    evidence=Path(os.environ['PAIR_ROOT'])/'evidence'; evidence.mkdir(mode=0o700,parents=True,exist_ok=True)
    arm=evidence/role;arm.mkdir(mode=0o700,exist_ok=True)
    cfg=read('artifacts/vs/'+CONFIGURED);base=read('artifacts/vs/'+BASE)
    commit=cmd('git','rev-parse','HEAD');version=os.environ['SONIC_IMAGE_VERSION']
    data={'commit':commit,'version':version,'runner':os.environ['RUNNER_NAME'],'checkout':str(ROOT.relative_to(evidence.parent)),
          'local_bazel':str(Path(os.environ['SONIC_BAZEL_CACHE_SOURCE']).relative_to(evidence.parent)),'host_cache':str(Path(os.environ['SONIC_HOSTBASE_CACHE_SOURCE']).relative_to(evidence.parent)),
          'builders':cmd('docker','ps','--all','--quiet','--no-trunc','--filter','label='+os.environ['SONIC_CI_CONTAINER_LABEL']).split(),
          'base':base,'configured':cfg,'installed':installed(version,commit),'tested_producers':[built(DEVICE),built(PLATFORM)],
          'package_candidates':packages()}
    check(data['builders'], 'missing retained installer container')
    # Save the content before enforcing a hit so a legitimate miss always has
    # attributable section/package hashes in the diagnostic artifact.
    save(arm/'result.json',data)
    print(f"{role}: {data['installed']['packages']} packages, {data['installed']['tags']} tags, configured host {'hit' if cfg['cache_hit'] else 'miss'} {cfg['identity_sha256']}",flush=True)
    if role=='a':check(base['cache_hit'] is False and cfg['cache_hit'] is False,'seed was not cold')
    else:
        a=read(evidence/'a/result.json')
        ac,bc=a['package_candidates'],data['package_candidates'];sections=cfg['input_sha256'];sc=a['configured']['input_sha256']
        save(evidence/'comparison.json',{'version_a':a['version'],'version_b':version,'base_key_a':a['base']['identity_sha256'],'base_key_b':base['identity_sha256'],
            'configured_key_a':a['configured']['identity_sha256'],'configured_key_b':cfg['identity_sha256'],'configured_hit_b':cfg['cache_hit'],
            'different_sections':sorted(k for k in sc.keys()|sections.keys() if sc.get(k)!=sections.get(k)),
            'different_candidate_files':sorted(k for k in ac.keys()|bc.keys() if ac.get(k)!=bc.get(k)),
            'candidate_files_a':len(ac),'candidate_files_b':len(bc)})
        compare(a,data)

if __name__=='__main__':main(sys.argv[1])
