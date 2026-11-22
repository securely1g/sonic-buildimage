#!/usr/bin/env python3
"""Run one measured arm in an isolated, same-runner native installer comparison."""
from __future__ import annotations

import argparse
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
import time
import zipfile

ROOT = Path.cwd()
EVIDENCE = ROOT / 'artifacts/vs/configured-host-benchmark'
PRIVATE = ROOT / 'artifacts/vs/.configured-host-benchmark-private'
CACHE_RECEIPT = ROOT / 'artifacts/vs/configured-host-cache.json'
OUTPUT = ROOT / 'target/sonic-vs.bin'
CONFIGURED = ROOT / 'target/sonic-vs.configured-host.squashfs'
CACHE = Path(os.environ['SONIC_HOSTBASE_CACHE_SOURCE']) if os.environ.get('SONIC_HOSTBASE_CACHE_SOURCE') else None
MARKER = 'usr/share/sonic/configured-host-benchmark-package'
ARMS = ('bootstrap-b', 'seed-a', 'restored-b', 'paired-control-b', 'changed-deb-b')
ARM_STARTED_NS = time.time_ns()


def write(path, value):
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(file):
    result = hashlib.sha256()
    for part in iter(lambda: file.read(1024 * 1024), b''):
        result.update(part)
    return result.hexdigest()


def digest(path):
    with path.open('rb') as f:
        return sha(f)


def hash_listing(items):
    return hashlib.sha256(json.dumps(sorted(items), separators=(',', ':')).encode()).hexdigest()


def shell(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def replace_selected_package(seed):
    """Replace only the checkout's directory entry, including when it points at a shared cache."""
    target = ROOT / 'target/debs/trixie/sonic-utilities-data_1.0-1_all.deb'
    require(target.is_file(), f'missing selected host DEB {target}')
    pkg = shell('dpkg-deb', '--field', target, 'Package', capture_output=True, text=True).stdout.strip()
    require(pkg == 'sonic-utilities-data', 'unexpected selected Debian package')
    original = digest(target)
    marker_bytes = f'package-content-test {seed}\n'.encode()
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.host-package-bench-') as temporary:
        temp = Path(temporary)
        unpack = temp / 'pkg'
        shell('dpkg-deb', '--raw-extract', target, unpack)
        sentinel = unpack / MARKER
        require(not sentinel.exists(), 'benchmark package marker already present')
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_bytes(marker_bytes)
        with (unpack / 'DEBIAN/md5sums').open('a') as checksums:
            checksums.write(f'{hashlib.md5(marker_bytes, usedforsecurity=False).hexdigest()}  {MARKER}\n')
        changed = temp / 'output.deb'
        shell('dpkg-deb', '--root-owner-group', '--build', unpack, changed)
        os.replace(changed, target)
    modified = digest(target)
    require(original != modified, 'repacked DEB did not change')
    return dict(package=pkg, version=shell('dpkg-deb', '--field', target, 'Version', capture_output=True, text=True).stdout.strip(),
                path=str(target.relative_to(ROOT)), original_sha256=original, modified_sha256=modified,
                installed_marker_sha256=hashlib.sha256(marker_bytes).hexdigest())


def private_transfer():
    """Exercise the same pack/restore protocol locally; the cache stays private to this job."""
    receipt = read(EVIDENCE / 'seed-a' / 'cache-receipt.json')
    key = receipt['identity_sha256']
    require(re.fullmatch('[a-f0-9]{64}', key), 'unexpected configured-host key')
    tool = ROOT / 'tools/bazel/ci/vs_host_base_transfer.py'
    pack, download = PRIVATE / 'packed', PRIVATE / 'download'
    started = time.monotonic()
    shell(sys.executable, tool, 'pack', CACHE, key, pack)
    packed = time.monotonic()
    require(not (CACHE / key).exists(), 'seed still present after packing')
    size = sum(p.stat().st_size for p in pack.rglob('*') if p.is_file())
    # Avoid reflinks or hardlinks. The runner must physically transfer and then restore the snapshot.
    shell('cp', '--reflink=never', '-R', '--', pack, download)
    copied = time.monotonic()
    shell(sys.executable, tool, 'restore', CACHE, download)
    restored = time.monotonic()
    require((CACHE / key).is_dir(), 'selected configured-host cache was not restored')
    return dict(bytes=size, local_pack_seconds=packed-started, local_copy_seconds=copied-packed,
                restore_seconds=restored-copied, consumer_transfer_seconds=restored-packed,
                total_private_transfer_seconds=restored-started, remote_transfer_measured=False)


def inspect_installer(arm, version, commit, work, changed):
    """Use the production verifier and inspect the same pristine image, without executing the installer."""
    sys.path.insert(0, str(ROOT/'scripts'))
    import validate_sonic_version as validator
    import yaml
    with OUTPUT.open('rb') as stream:
        header = b'\nexit_marker\n'
        position = stream.read(65536).find(header)
        require(position >= 0, 'installer marker missing')
        stream.seek(position + len(header))
        with tarfile.open(fileobj=stream, mode='r:') as installs:
            with installs.extractfile('installer/fs.zip') as file, zipfile.ZipFile(file) as payload:
                squashfs = work / 'fs.squashfs'
                with payload.open('fs.squashfs') as src, squashfs.open('wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                validator.validate_boot_bundle(installs, payload, squashfs)

    def cat(member):
        return shell('unsquashfs', '-cat', squashfs, member, capture_output=True).stdout

    metadata = yaml.safe_load(cat('etc/sonic/sonic_version.yml'))
    validator.validate_version(metadata, 'no_sign')
    require(version == metadata['build_version'], f'incorrect released version: {metadata["build_version"]}')
    require(commit.startswith(metadata['commit_id']), 'installed commit does not match source')
    images = json.loads(cat('usr/share/sonic/docker-image-inventory.json'))
    require(images and all(t.rsplit(':', 1)[-1] in (version, 'latest') for t in images), 'stale image tag')
    require({t.rsplit(':', 1)[0] for t in images if t.endswith(':latest')} ==
            {t.rsplit(':', 1)[0] for t in images if t.endswith(':' + version)}, 'release tag missing for an image')
    packages = []
    for entry in cat('var/lib/dpkg/status').decode().split('\n\n'):
        fields = dict(x.split(': ', 1) for x in entry.splitlines() if x and x[0].isalpha() and ': ' in x)
        if fields.get('Status') == 'install ok installed':
            packages.append((fields['Package'], fields['Architecture'], fields['Version']))
    require(any(p[0] == 'sonic-utilities-data' for p in packages), 'selected host package not installed')
    files = cat('var/lib/dpkg/info/sonic-utilities-data.list').decode().splitlines()
    if changed:
        require('/'+MARKER in files and hashlib.sha256(cat(MARKER)).hexdigest() == changed['installed_marker_sha256'],
                'changed Debian package sentinel missing from installed filesystem')
    else:
        require('/'+MARKER not in files, 'unexpected package marker in control/seed build')
    result = dict(version=version, commit=metadata['commit_id'], installed_package_count=len(packages),
                  installed_packages_sha256=hash_listing(packages), image_tags=len(images),
                  image_inventory_sha256=hash_listing(images.items()), installer_bytes=OUTPUT.stat().st_size,
                  configured_package_marker=bool(changed))
    write(work / 'packages.json', packages)
    return result


def phase_receipt(log):
    key = '[ image phases ] [ target/sonic-vs.bin ] '
    with log.open(errors='replace') as lines:
        detected = [json.loads(l.split(key)[-1].strip()) for l in lines if key in l]
    require(len(detected) == 1, f'expected one measured installer assembly, got {len(detected)}')
    phases = detected[0]
    segments = phases['segments']
    require([(s['phase'], s['context']) for s in segments[1:4]] == [('assembly', 'filesystem'), ('assembly', 'docker-load'), ('assembly', 'filesystem')], 'unexpected installer phase order')
    return phases


def start_live():
    shared = ROOT/'.configured-host-bench'
    shared.mkdir(mode=0o700)
    PRIVATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    base=read(Path(__file__).with_name('configured-host-base-receipt.json'))
    require(base['identity_sha256']=='572fcd4fbbf5d4c13dfb2954b8e30a550d181975c4bdec34b362fc8180bce45a','unexpected pinned stage-one key')
    for kind, name in [('image','target/sonic-vs.bin__vs__rfs.squashfs'),('inventory','target/versions/host-base-image/versions-deb-trixie-amd64')]:
        record=base['outputs'][kind]; sha=record['sha256']
        source=CACHE/base['identity_sha256']/'cas'/sha[:2]/sha
        dst=ROOT/name; dst.parent.mkdir(exist_ok=True,parents=True)
        # An independent file prevents any native producer from writing to the original artifact.
        shell('cp','--reflink=auto','--',source,dst)
        require(dst.stat().st_size==record['bytes'] and digest(dst)==sha,'pinned stage-one artifact digest mismatch')
    write(ROOT/'artifacts/vs/host-base-cache.json',base)
    for source, dest in [('configured-host-inside.py','inside.py'),('configured-host-capture.mk','capture.mk')]:
        shutil.copyfile(Path(__file__).with_name(source),shared/dest)
    case=dict(os.environ, SONIC_VS_CONFIGURED_HOST_CACHE='y', SONIC_IMAGE_VERSION='hostbench-bootstrap',
              SONIC_BUILD_VARS=os.environ.get('SONIC_BUILD_VARS','')+' -f /sonic/.configured-host-bench/capture.mk')
    with (PRIVATE/'launch.log').open('wb') as log:
        task=subprocess.Popen(['bash','tools/bazel/ci/build_vs.sh'],env=case,stdin=subprocess.DEVNULL,stdout=log,
                              stderr=subprocess.STDOUT,start_new_session=True)
    for _ in range(240):
        if (shared/'ready.json').is_file(): break
        require(task.poll() is None, 'production VS launcher ended before reaching the native Make invocation')
        time.sleep(5)
    require((shared/'ready.json').is_file(), 'timed out waiting for the private native Make invocation')
    ids=shell('docker','ps','--quiet','--no-trunc','--filter','label='+case['SONIC_CI_CONTAINER_LABEL'],
              capture_output=True,text=True).stdout.split()
    require(len(ids)==1 and re.fullmatch('[0-9a-f]{64}',ids[0]), 'expected exactly one live native build container')
    uid=read(shared/'ready.json')['uid']
    require(uid==os.getuid() and uid>0,'expected native build user to own the workspace')
    write(shared/'connection.json',{'id':ids[0],'uid':uid})
    print('Native installer builder ready',flush=True)


def stop_live():
    ids=shell('docker','ps','--quiet','--no-trunc','--filter','label='+os.environ['SONIC_CI_CONTAINER_LABEL'],
              capture_output=True,text=True).stdout.split()
    if ids:
        shell('docker','stop','--time','5',*ids,stdout=subprocess.DEVNULL)
    # build_vs.sh releases its private rootfs handoff after the stopped native
    # launcher returns. Only the labeled job container is stopped above.


def current_native(arm, version):
    session=read(ROOT/'.configured-host-bench/connection.json')
    allowed=shell('docker','ps','--quiet','--no-trunc','--filter','label='+os.environ['SONIC_CI_CONTAINER_LABEL'],capture_output=True,text=True).stdout.split()
    require(allowed==[session['id']],"expected exactly one retained builder for this run")
    root=ROOT/'fsroot-vs'; require(root.resolve()==root,'unexpected installer root on the runner')
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount=Path(line.split(' - ',1)[0].split()[4]);require(mount!=root and root not in mount.parents,'runner host has a mount under the installer scratch tree')
    slot=os.environ.get('SONIC_HOSTBENCH_RESUME_SLOT','0');require(slot in ('0','1','2'),'unexpected recovery slot')
    return ['docker','exec','--env','SONIC_HOSTBENCH_CALLER_MNTNS='+str(os.stat('/proc/self/ns/mnt').st_ino),'--env','SONIC_HOSTBENCH_RECOVERY_SLOT='+slot,'--env','SONIC_HOSTBENCH_INNER_RESUME='+os.environ.get('SONIC_HOSTBENCH_RESUME','0'),'--user',str(session['uid']),'--workdir','/sonic',session['id'],
            'python3','-B','/sonic/.configured-host-bench/inside.py','run',arm,version]


def transient_go_fetch(log):
    # This narrowly identifies a failed public Go dependency fetch before the
    # configured-host action/installer. Do not retry any semantic check.
    fetch = analysis = assembled = False
    with log.open(errors='replace') as stream:
        for line in stream:
            if ("ERROR: no such package '@@otelcol-contrib++go_deps+" in line and
                    'fetch_repo:' in line and 'net/http: TLS handshake timeout' in line):
                fetch = True
            if 'ERROR: Analysis of target' in line and 'failed; build aborted' in line:
                analysis = True
            if '[ image phases ] [ target/sonic-vs.bin ] ' in line:
                assembled = True
    return fetch and analysis and not assembled and not CACHE_RECEIPT.exists() and not OUTPUT.exists()


def build_with_retry(arm, case, log, work):
    attempts = []
    for number in (1, 2, 3):
        started = time.monotonic()
        with log.open('wb') as stdout:
            process = subprocess.run(current_native(arm, case['SONIC_IMAGE_VERSION']), env=case, stdout=stdout,
                                     stderr=subprocess.STDOUT)
        elapsed = time.monotonic() - started
        entry = dict(number=number, seconds=elapsed, exit=process.returncode)
        attempts.append(entry)
        if process.returncode == 0:
            write(work/'make-attempts.json', attempts)
            native=read(work/'inner-make-seconds.json')
            require(native['exit']==0,'expected successful inner Make')
            return native['seconds'], attempts
        retry = number < 3 and transient_go_fetch(log)
        entry['retry_eligible'] = retry
        write(work/'make-attempts.json', attempts)
        if not retry:
            raise RuntimeError(f'native Make failed for {arm}: exit {process.returncode}')
        log.rename(PRIVATE/(arm + '-go-fetch-attempt-' + str(number) + '.log'))
        cleanup_started = time.monotonic()
        stopped = shell('docker', 'ps', '--all', '--quiet', '--filter', 'status=exited', '--filter',
                        'label=' + case['SONIC_CI_CONTAINER_LABEL'], capture_output=True, text=True).stdout.split()
        if stopped:
            shell('docker', 'rm', *stopped, stdout=subprocess.DEVNULL)
        entry['cleanup_seconds'] = time.monotonic() - cleanup_started
        write(work/'make-attempts.json', attempts)
        print(f'{arm}: retry Go dependency TLS handshake timeout with prepared sources and outputs', flush=True)


def run(arm):
    require(CACHE and CACHE.is_dir() and not CACHE.is_symlink(), 'job-private native host cache missing')
    require(os.environ['SONIC_BAZEL_REMOTE_UPLOAD'] == 'n' and os.environ['SONIC_DPKG_CACHE_METHOD'] == 'rcache', 'shared caches must be read-only')
    commit = shell('git', 'rev-parse', 'HEAD', capture_output=True, text=True).stdout.strip()
    version = 'hostbench-' + ('a' if arm == 'seed-a' else 'b') + '-' + commit[:9]
    work = EVIDENCE / arm
    resume=os.environ.get('SONIC_HOSTBENCH_RESUME')=='1'
    slot=os.environ.get('SONIC_HOSTBENCH_RESUME_SLOT','0')
    work.mkdir(mode=0o700, parents=True,exist_ok=resume)
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if resume and (work/'metrics.json').exists():
        print('Validated measurement retained for '+arm,flush=True);return
    if resume and (work/'failure.json').exists(): shutil.copy(work/'failure.json',work/('before-rescue-'+slot+'.json'))
    if resume and (work/'make-attempts.json').exists(): shutil.copy(work/'make-attempts.json',work/('before-rescue-attempts-'+slot+'.json'))
    transfer={}
    if arm=='restored-b':
        if resume and (work/'transfer.json').exists():transfer=read(work/'transfer.json')
        else:
            transfer=private_transfer();write(work/'transfer.json',transfer)
    changed=None
    if arm=='changed-deb-b':
        if resume and (work/'package-perturbation.json').exists():
            changed=read(work/'package-perturbation.json')
            require(digest(ROOT/changed['path'])==changed['modified_sha256'],'modified benchmark input was replaced')
        else:
            changed=replace_selected_package(commit);write(work/'package-perturbation.json',changed)
    enabled=arm not in ('bootstrap-b','paired-control-b')
    case=dict(os.environ, SONIC_IMAGE_VERSION=version, SONIC_VS_CONFIGURED_HOST_CACHE='y' if enabled else 'n')
    old=work/'inner-make-seconds.json'
    retained=resume and old.exists() and read(old).get('exit')==0 and (OUTPUT.is_file() or (arm=='seed-a' and (work/'seed-boundary.json').exists()))
    # If the changed host action succeeded but the installer failed, retain its
    # first receipt and move its job-private key aside to repeat a *cold* sample.
    if not retained:
        if resume and enabled and arm!='restored-b' and CACHE_RECEIPT.is_file():
            earlier=read(CACHE_RECEIPT); k=earlier['identity_sha256']; require(re.fullmatch('[a-f0-9]{64}',k),'invalid retained key')
            if (CACHE/k).exists() and not earlier['cache_hit']:
                os.rename(CACHE/k, PRIVATE/('prior-'+arm+'-'+slot+'-'+k));write(work/('prior-cache-'+slot+'.json'),earlier)
        for file in (OUTPUT,CONFIGURED,CACHE_RECEIPT,work/'seed-boundary.json'):
            file.unlink(missing_ok=True)
    operations=ROOT/'artifacts/vs/image-timings/installer-steps'
    if not retained and operations.is_dir():
        for previous in operations.glob('*.json'):previous.unlink()
    log=PRIVATE/(arm+'.log')
    if retained:
        elapsed=read(old)['seconds'];attempts=read(work/'make-attempts.json')
        if arm=='bootstrap-b' and not (ROOT/'.configured-host-bench/B.json').exists():
            # Initial B can be a successful true native Make whose subsequent
            # input-validation helper needs repair. Rebuild only its manifest.
            shell(*current_native(arm,version),env=case)
    else: elapsed,attempts=build_with_retry(arm,case,log,work)
    if enabled:
        require(CACHE_RECEIPT.is_file(), 'configured-host cache unsupported or unexpectedly fell back')
        receipt = read(CACHE_RECEIPT)
        write(work/'cache-receipt.json', receipt)
        pinned={'bytes':646021120,'sha256':'289b2656e87b305ff5ef075ba4b239cab9c5bf99dff078ea891d93e62645659d'}
        encoded=(json.dumps(pinned,sort_keys=True,separators=(',',':'))+'\n').encode()
        require(receipt['input_sha256']['base']==hashlib.sha256(encoded).hexdigest(),'configured action used a different first-stage image')
        require(receipt['cache_hit'] == (arm == 'restored-b'), 'wrong configured-host cache outcome')
        if arm != 'seed-a':
            version_only = read(EVIDENCE/'seed-a/cache-receipt.json')['identity_sha256'] == receipt['identity_sha256']
            require(version_only == (not changed), 'wrong configured-host input identity')
            previous=read(EVIDENCE/'seed-a/cache-receipt.json')['input_sha256']
            differences={k for k,v in receipt['input_sha256'].items() if v!=previous[k]}
            require(differences==({'packages'} if changed else set()), 'unexpected input sections: '+str(differences))
    else:
        require(not CACHE_RECEIPT.exists(), 'disabled control unexpectedly used cache')
    if arm=='seed-a':
        require((work/'seed-boundary.json').exists() and not OUTPUT.exists(),'seed must stop before producing a release-A installer')
        require(read(work/'seed-boundary.json')['release']==version,'A seed release mismatch')
        write(work/'metrics.json',dict(arm=arm,runner=os.environ['RUNNER_NAME'],commit=commit,release=version,
              timing_scope='configured host action only',make_seconds=elapsed,successful_attempt=len(attempts)))
        return
    require(OUTPUT.is_file(),'Make produced no installer')
    phase=phase_receipt(log)
    write(work/'phases.json',phase)
    if operations.is_dir():
        shutil.copytree(operations,work/'operations',dirs_exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hostbench-inspect-', dir=PRIVATE) as inspection:
        package = inspect_installer(arm, version, commit, Path(inspection), changed)
    if arm in ('restored-b', 'paired-control-b','changed-deb-b'):
        control = read(EVIDENCE/'bootstrap-b/metrics.json')['installed']
        for name in ('installed_packages_sha256', 'image_inventory_sha256'):
            require(package[name] == control[name], f'cached installer differs from native control: {name}')
    if changed:
        require(digest(ROOT / changed['path']) == changed['modified_sha256'], 'another build replaced the diagnostic package')
    measurement = dict(arm=arm, runner=os.environ['RUNNER_NAME'], commit=commit, installed=package,
        timing_scope='native slave.mk installer invocation', make_seconds=elapsed, successful_attempt=len(attempts), all_attempts_seconds=sum(a['seconds'] + a.get('cleanup_seconds', 0) for a in attempts),
        assembly_seconds=phase['duration_seconds'],
        step2_seconds=phase['segments'][1]['duration_seconds'], step4_seconds=phase['segments'][3]['duration_seconds'],
        restore_inclusive_make_seconds=elapsed+transfer.get('consumer_transfer_seconds', 0))
    write(work/'metrics.json', measurement)
    print(json.dumps(measurement, indent=2), flush=True)


def equal_size_transfer(mode):
    # Only random bytes are ever uploaded as the transfer probe. The actual
    # configured rootfs and OCI/Bazel stores stay in their private directories.
    probe = PRIVATE / 'cache-transfer-sample.bin'
    if mode == 'generate-probe':
        receipt = read(EVIDENCE/'seed-a/cache-receipt.json')
        key = receipt['identity_sha256']
        require(re.fullmatch('[a-f0-9]{64}', key), 'unexpected seed cache key')
        sources = [f for f in (CACHE/key).rglob('*') if f.is_file()]
        total = sum(f.stat().st_size for f in sources)
        require(total > 0, 'seed cache is empty')
        checksum = hashlib.sha256()
        remaining = total
        with probe.open('xb') as dest:
            while remaining:
                data = os.urandom(min(1024*1024, remaining))
                checksum.update(data)
                dest.write(data)
                remaining -= len(data)
        write(EVIDENCE/'transfer-probe.json', dict(bytes=total, files_in_real_snapshot=len(sources), sha256=checksum.hexdigest()))
    else:
        downloaded = PRIVATE / 'transfer-download' / probe.name
        metadata = read(EVIDENCE/'transfer-probe.json')
        require(downloaded.stat().st_size == metadata['bytes'] and digest(downloaded) == metadata['sha256'],
                'equal-size GitHub artifact transfer did not verify')
        metadata.update(verified=True, artifact_id=int(os.environ['PROBE_ARTIFACT_ID']))
        write(EVIDENCE/'transfer-probe.json', metadata)
        probe.unlink()
        downloaded.unlink()
    print(f'{mode}: {probe.name}', flush=True)


def failure_details(arm,error):
    driver=Path(os.environ['GITHUB_WORKSPACE'])/('configured-host-bench-'+os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT'])/'.github'
    sys.path.insert(0,str(driver))
    import configured_host_errors
    work=EVIDENCE/arm
    report=configured_host_errors.collect(ROOT,arm,error,work,PRIVATE/(arm+'.log'),started_ns=ARM_STARTED_NS)
    write(work/'failure.json',report)
    print(json.dumps(report,indent=2),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('arm', choices=ARMS + ('generate-probe', 'verify-probe','start-live','stop-live'))
    arm = parser.parse_args().arm
    try:
        if arm == 'start-live': start_live()
        elif arm == 'stop-live': stop_live()
        elif arm in ARMS:
            run(arm)
        else:
            equal_size_transfer(arm)
    except Exception as exc:
        failure_details(arm, exc)
        raise SystemExit(1)
