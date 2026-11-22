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
ARMS = ('seed-a', 'version-b-control', 'restored-b', 'paired-control-b', 'changed-deb-b')


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


def run(arm):
    require(CACHE and CACHE.is_dir() and not CACHE.is_symlink(), 'job-private native host cache missing')
    require(os.environ['SONIC_BAZEL_REMOTE_UPLOAD'] == 'n' and os.environ['SONIC_DPKG_CACHE_METHOD'] == 'rcache', 'shared caches must be read-only')
    commit = shell('git', 'rev-parse', 'HEAD', capture_output=True, text=True).stdout.strip()
    version = 'hostbench-' + ('a' if arm == 'seed-a' else 'b') + '-' + commit[:9]
    work = EVIDENCE / arm
    work.mkdir(mode=0o700, parents=True)
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    transfer = private_transfer() if arm == 'restored-b' else {}
    changed = replace_selected_package(commit) if arm == 'changed-deb-b' else None
    if changed:
        write(work/'package-perturbation.json', changed)
    if transfer:
        write(work/'transfer.json', transfer)
    enabled = arm not in ('version-b-control', 'paired-control-b')
    case = dict(os.environ, SONIC_IMAGE_VERSION=version, SONIC_VS_CONFIGURED_HOST_CACHE='y' if enabled else 'n')
    if changed:
        # The deliberately substituted DEB is an input for this arm. Preserve
        # its bytes if Make would otherwise regenerate it in a nested build.
        case['MAKEFLAGS'] = (case.get('MAKEFLAGS', '') + ' --old-file=' + changed['path']).strip()
    # A retained target must not satisfy the published installer or configured-host request.
    for file in (OUTPUT, CONFIGURED, CACHE_RECEIPT):
        file.unlink(missing_ok=True)
    operations = ROOT / 'artifacts/vs/image-timings/installer-steps'
    if operations.is_dir():
        for previous in operations.glob('*.json'):
            previous.unlink()
    log = PRIVATE / (arm + '.log')
    started = time.monotonic()
    with log.open('wb') as stdout:
        process = subprocess.run(['bash', 'tools/bazel/ci/build_vs.sh'], env=case, stdout=stdout, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - started
    if process.returncode:
        raise RuntimeError(f'native Make failed for {arm}: exit {process.returncode}')
    require(OUTPUT.is_file(), 'Make produced no installer')
    phase = phase_receipt(log)
    write(work/'phases.json', phase)
    if enabled:
        require(CACHE_RECEIPT.is_file(), 'configured-host cache unsupported or unexpectedly fell back')
        receipt = read(CACHE_RECEIPT)
        write(work/'cache-receipt.json', receipt)
        require(receipt['cache_hit'] == (arm == 'restored-b'), 'wrong configured-host cache outcome')
        if arm != 'seed-a':
            version_only = read(EVIDENCE/'seed-a/cache-receipt.json')['identity_sha256'] == receipt['identity_sha256']
            require(version_only == (not changed), 'wrong configured-host input identity')
    else:
        require(not CACHE_RECEIPT.exists(), 'disabled control unexpectedly used cache')
    if operations.is_dir():
        shutil.copytree(operations, work/'operations')
    with tempfile.TemporaryDirectory(prefix='hostbench-inspect-', dir=PRIVATE) as inspection:
        package = inspect_installer(arm, version, commit, Path(inspection), changed)
    if arm in ('restored-b', 'paired-control-b'):
        control = read(EVIDENCE/'version-b-control/metrics.json')['installed']
        for name in ('installed_packages_sha256', 'image_inventory_sha256'):
            require(package[name] == control[name], f'cached installer differs from native control: {name}')
    if changed:
        require(digest(ROOT / changed['path']) == changed['modified_sha256'], 'another build replaced the diagnostic package')
    measurement = dict(arm=arm, runner=os.environ['RUNNER_NAME'], commit=commit, installed=package,
        make_seconds=elapsed, assembly_seconds=phase['duration_seconds'],
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


def failure_details(arm, error):
    # Keep private reports and rootfs local; publish only errors, never a complete
    # recipe's xtrace, environment, repository credentials, or mirror configuration.
    diagnostics = {'error': type(error).__name__, 'sources': {}}
    checks = re.compile(r'^(?:ERROR:|E:|W: Failed|VS configured host|Unknown host extension phase|'
                        r'ValueError:|RuntimeError:|FileNotFoundError:|PermissionError:|'
                        r'pip: error:|unsquashfs:|mksquashfs:|dpkg: error|awk:|sed:|chmod:|mount:|'
                        r'cannot (?:open|copy|access|stat)|cp:|mv:|install:|tar:|fatal:|'
                        r'(?:ERROR|Error): (?:Could not|Missing|No matching|Unknown))', re.I)
    def sanitized(raw):
        if re.search(r'passw(?:or)?d|authoriz|credential|bearer|private.key|secret|token[=:]|BEGIN [A-Z ]*KEY', raw, re.I):
            return None
        return re.sub(r'(?:https?|grpc|file)://[^\s\'"<>]+', '[location]', raw)[:500]
    diagnostics['reason'] = sanitized(str(error))
    paths = [ROOT/'target/vs-configured-host-native.log', ROOT/'target/sonic-vs.bin.log', PRIVATE/(arm+'.log')]
    if CACHE_RECEIPT.is_file():
        obj = read(CACHE_RECEIPT)
        diagnostics['cache'] = {k: obj.get(k) for k in ('cache_hit','identity_sha256','input_sha256','elapsed_seconds','native_build_seconds')}
    for path in paths:
        if not path.is_file():
            continue
        messages = []
        with path.open(errors='replace') as lines:
            for line in lines:
                clean = line.strip()
                if checks.search(clean):
                    safe = sanitized(clean)
                    if safe and (not messages or messages[-1] != safe):
                        messages.append(safe)
        diagnostics['sources'][path.name] = messages[-32:]
    write(EVIDENCE/arm/'failure.json', diagnostics)
    print(json.dumps(diagnostics, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('arm', choices=ARMS + ('generate-probe', 'verify-probe'))
    arm = parser.parse_args().arm
    try:
        if arm in ARMS:
            run(arm)
        else:
            equal_size_transfer(arm)
    except Exception as exc:
        failure_details(arm, exc)
        raise SystemExit(1)
