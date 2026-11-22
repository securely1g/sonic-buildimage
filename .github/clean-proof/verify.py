#!/usr/bin/env python3
"""Collect bounded evidence for each fresh native VS installer before judging the pair."""
import hashlib
import json
import math
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
PRODUCERS = (DEVICE, PLATFORM)
RECEIPTS = {'base': 'host-base-cache.json', 'configured': 'configured-host-cache.json'}
SECTIONS = {'base', 'builder', 'files', 'format', 'host_program_sha256', 'options', 'packages'}
HEX = r'[0-9a-f]{64}'
LABEL = r'[A-Za-z0-9][A-Za-z0-9_./:+~%-]*'

class EvidenceError(Exception):
    """A diagnostic that is safe to publish."""

def check(ok, message):
    if not ok:
        raise EvidenceError(message)

def read(p):
    return json.loads(Path(p).read_bytes())

def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    tmp.replace(path)

def fingerprint(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(2 ** 20), b''):
        h.update(chunk)
    return h.hexdigest()

def sha(path):
    with path.open('rb') as stream:
        return fingerprint(stream)

def listing(values):
    return hashlib.sha256(json.dumps(sorted(values), separators=(',', ':')).encode()).hexdigest()

def cmd(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()

class Recorder:
    def __init__(self, path):
        self.path = path
        self.data = {'validation_errors': [], 'installed': {}, 'tested_producers': [],
                     'package_candidates': {}, 'archive_sha256': {}}
        self.flush()

    def flush(self):
        save(self.path, self.data)

    def record(self, stage, fn):
        try:
            return fn()
        except Exception as exc:
            reason = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
            if isinstance(exc, subprocess.CalledProcessError):
                reason += ' (exit %d)' % exc.returncode
            self.data['validation_errors'].append({'stage': stage, 'error': reason})
            print(f'{stage}: {reason}', flush=True)
        finally:
            self.flush()

def load_receipt(rec, name):
    path = Path('artifacts/vs') / RECEIPTS[name]
    raw = read(path)
    out = rec.data[name] = {}
    # Preserve every supported evidence field, never arbitrary producer text.
    for key in ('identity_sha256', 'builder', 'revision'):
        pattern = {'identity_sha256': HEX, 'revision': r'[0-9a-f]{40}',
                   'builder': r'sha256:' + HEX}[key]
        if isinstance(raw.get(key), str) and re.fullmatch(pattern, raw[key]):
            out[key] = raw[key]
    if type(raw.get('cache_hit')) is bool:
        out['cache_hit'] = raw['cache_hit']
    for key in ('elapsed_seconds', 'bazel_invocation_seconds', 'materialize_seconds', 'native_build_seconds'):
        value = raw.get(key)
        if type(value) in (float, int) and math.isfinite(value) and value >= 0:
            out[key] = value
    out['outputs'] = {}
    for key in ('image', 'inventory'):
        item = raw.get('outputs', {}).get(key, {})
        if (re.fullmatch(HEX, str(item.get('sha256'))) and type(item.get('bytes')) is int and item['bytes'] > 0):
            out['outputs'][key] = {'sha256': item['sha256'], 'bytes': item['bytes']}
    if name == 'configured':
        out['input_sha256'] = {k: v for k, v in raw.get('input_sha256', {}).items()
                               if k in SECTIONS and isinstance(v, str) and re.fullmatch(HEX, v)}
    check({'cache_hit','identity_sha256','builder','native_build_seconds','bazel_invocation_seconds'} <= out.keys()
          and 'image' in out['outputs'], 'missing valid producer receipt fields')
    if name == 'configured':
        check(set(out['input_sha256']) == SECTIONS, 'incomplete configured-host input digests')
    else:
        check('inventory' in out['outputs'], 'missing host-base inventory digest')
    marker = Path('artifacts/vs/clean-observe.mk')
    check(path.stat().st_mtime_ns >= marker.stat().st_mtime_ns, 'receipt predates fresh checkout marker')
    rec.data.setdefault('fresh_receipts', {})[name] = True

def packages(rec):
    declared = set()
    def declared_inputs():
        declared.update(Path('artifacts/vs/clean-declared.txt').read_text().split())
        selection = {name: str(path) in declared for name, path in (
            ('platform_deb', PLATFORM), ('device_tar', DEVICE_TAR), ('device_deb', DEVICE))}
        rec.data['declared_selection'] = selection
        check(selection == {'platform_deb': True, 'device_tar': True, 'device_deb': False},
              'expected platform DEB and device TAR in installer inputs')
    rec.record('make.selection', declared_inputs)
    paths = {Path(n) for n in declared if n.startswith('target/') and n.endswith(('.deb', '.whl'))}
    paths.update(Path('target/debs/trixie').glob('*.deb'))
    for parent in Path('target').glob('*wheel*'):
        paths.update(parent.rglob('*.whl'))
    key_paths = [DEVICE_TAR, *PRODUCERS]
    for path in key_paths + sorted(paths.difference(key_paths)):
        def collect(path=path):
            check(not path.is_absolute() and '..' not in path.parts and re.fullmatch(LABEL, str(path)),
                  'unexpected package path')
            digest = sha(path)
            rec.data['package_candidates'][str(path)] = digest
            if path in key_paths:
                rec.data['archive_sha256'][path.name] = digest
        stage = str(path) if re.fullmatch(LABEL, str(path)) else 'invalid path'
        rec.record('archive.' + stage, collect)

def built(rec, deb):
    producer = {'archive': deb.name, 'sha256': rec.data['archive_sha256'].get(deb.name)}
    rec.data['tested_producers'].append(producer)
    for field in ('Package', 'Version'):
        def collect(field=field):
            value = cmd('dpkg-deb', '-f', deb, field)
            check(re.fullmatch(LABEL, value), 'unexpected Debian control value')
            producer[field.lower()] = value
        rec.record(f'producer.{deb.name}.{field}', collect)
    def inspect_log():
        log = Path(str(deb) + '.log').read_text(errors='replace')
        package = producer.get('package') or deb.name.split('_')[0]
        producer['native_dpkg_deb'] = bool(re.search(
            r"dpkg-deb: building package ['‘]" + re.escape(package) + r"['’] in ", log))
        producer['loaded_from_cache'] = '[ CACHE::LOADED ]' in log
        check(producer['native_dpkg_deb'] and not producer['loaded_from_cache'],
              'missing native dpkg-deb output or archive restored from package cache')
    rec.record('producer.' + deb.name + '.log', inspect_log)

def installed(rec, expected, commit):
    sys.path.insert(0, str(ROOT / 'scripts'))
    img = Path('target/sonic-vs.bin')
    result = rec.data['installed']
    result['bytes'] = img.stat().st_size
    rec.flush()
    with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as d, img.open('rb') as binary:
        marker = b'\nexit_marker\n'
        offset = binary.read(65536).find(marker)
        check(offset >= 0, 'missing ONIE exit marker')
        binary.seek(offset + len(marker))
        squash = Path(d) / 'fs.squashfs'
        with tarfile.open(fileobj=binary, mode='r:') as wrapper, wrapper.extractfile('installer/fs.zip') as f, zipfile.ZipFile(f) as payload:
            def rootfs():
                with payload.open('fs.squashfs') as source, squash.open('wb') as dest:
                    shutil.copyfileobj(source, dest, 2 ** 20)
                result['squashfs_bytes'] = squash.stat().st_size
            rec.record('installer.fs.squashfs', rootfs)
            def member(name):
                return subprocess.check_output(['unsquashfs', '-cat', squash, name], stderr=subprocess.DEVNULL)
            def release():
                import yaml
                from validate_sonic_version import validate_version
                version = yaml.safe_load(member('etc/sonic/sonic_version.yml'))
                for src, key, pattern in [('build_version', 'version', LABEL), ('commit_id', 'commit', r'[0-9a-f]{7,40}'),
                                          ('secure_boot_image', 'secure_boot_image', r'yes|no')]:
                    value = version.get(src)
                    if isinstance(value, str) and re.fullmatch(pattern, value):
                        result[key] = value
                validate_version(version, 'no_sign')
                check(expected == result.get('version') and commit.startswith(result.get('commit') or '!'),
                      'installer release or commit mismatch')
            def image_tags():
                images = json.loads(member('usr/share/sonic/docker-image-inventory.json'))
                check(isinstance(images, dict), 'invalid image inventory')
                latest = {name[:-7] for name in images if name.endswith(':latest')}
                release = {name.rsplit(':', 1)[0] for name in images if name.endswith(':' + expected)}
                check(all(re.fullmatch(LABEL, name) for name in latest), 'invalid repository name')
                result.update(selected_images=sorted(latest), tags=len(images))
                check(len(latest) == 28 and latest == release and len(images) == 56, 'wrong installed runtime tags')
                check(all(isinstance(images[name + ':latest'], str) and re.fullmatch('sha256:' + HEX, images[name + ':latest'])
                          and images[name + ':latest'] == images[name + ':' + expected] for name in latest),
                      'latest and release tags do not reference the same valid image ID')
                result['paired_config_sha256'] = listing((name, images[name + ':latest']) for name in latest)
            def inventory():
                entries = []
                for entry in member('var/lib/dpkg/status').decode().split('\n\n'):
                    fields = dict(line.split(': ', 1) for line in entry.splitlines() if line[:1].isalpha() and ': ' in line)
                    if fields.get('Status') == 'install ok installed':
                        entries.append((fields['Package'], fields['Architecture'], fields['Version']))
                result.update(packages=len(entries), package_inventory_sha256=listing(entries))
                check(any(item[0] == 'sonic-device-data' for item in entries), 'device data missing from dpkg inventory')
            def boot():
                from validate_sonic_version import validate_boot_bundle
                validate_boot_bundle(wrapper, payload, squash)
                result['verified_boot_bundle'] = True
            if result.get('squashfs_bytes'):
                for name, fn in [('release', release), ('image_tags', image_tags), ('dpkg', inventory), ('boot_bundle', boot)]:
                    rec.record('installer.' + name, fn)
            def platform():
                with payload.open('platform.tar.gz') as blob, tarfile.open(fileobj=blob, mode='r|gz') as archive:
                    for item in archive:
                        if item.name.lstrip('./') == 'common/' + PLATFORM.name and item.isfile():
                            with archive.extractfile(item) as source:
                                result['platform_sha256'] = fingerprint(source)
                            break
                check(result.get('platform_sha256') is not None and result['platform_sha256'] == rec.data['archive_sha256'].get(PLATFORM.name),
                      'installer platform archive missing or different from native DEB')
            rec.record('installer.platform', platform)

def compare(rec, a, b, evidence):
    def same(name, test):
        rec.record('compare.' + name, lambda: check(test(), name))
    ac, bc = a.get('package_candidates', {}), b['package_candidates']
    ca, cb = a.get('configured', {}), b.get('configured', {})
    sa, sb = ca.get('input_sha256', {}), cb.get('input_sha256', {})
    save(evidence / 'comparison.json', {
        'version_a': a.get('version'), 'version_b': b.get('version'),
        'base_key_a': a.get('base', {}).get('identity_sha256'), 'base_key_b': b.get('base', {}).get('identity_sha256'),
        'configured_key_a': ca.get('identity_sha256'), 'configured_key_b': cb.get('identity_sha256'),
        'configured_hit_b': cb.get('cache_hit'), 'different_sections': sorted(k for k in sa.keys() | sb.keys() if sa.get(k) != sb.get(k)),
        'different_candidate_files': sorted(k for k in ac.keys() | bc.keys() if ac.get(k) != bc.get(k)),
        'candidate_files_a': len(ac), 'candidate_files_b': len(bc)})
    same('seed evidence complete', lambda: a.get('validation_errors') == [] and a.get('verification_complete'))
    same('same commit and runner, different version', lambda: a['commit'] == b['commit'] and a['runner'] == b['runner'] and a['version'] != b['version'])
    for field in ('checkout', 'local_bazel', 'host_cache'):
        same('distinct ' + field, lambda: a[field] != b[field])
    same('distinct builder containers', lambda: a['builders'] and b['builders'] and set(a['builders']).isdisjoint(b['builders']))
    for key in RECEIPTS:
        same(key + ' cold then hit', lambda: a[key]['cache_hit'] is False and b[key]['cache_hit'] is True)
        same(key + ' stable identity and outputs', lambda: a[key]['identity_sha256'] == b[key]['identity_sha256'] and a[key]['outputs'] == b[key]['outputs'])
    same('configured inputs unchanged', lambda: a['configured']['input_sha256'] == b['configured']['input_sha256'])
    same('configured native build skipped', lambda: b['configured']['native_build_seconds'] == 0)
    same('matching native package producers', lambda: a['tested_producers'] == b['tested_producers'])
    same('matching device TAR', lambda: ac[str(DEVICE_TAR)] == bc[str(DEVICE_TAR)])
    for field in ('packages', 'package_inventory_sha256', 'selected_images', 'tags'):
        same('installed ' + field, lambda: a['installed'][field] == b['installed'][field])

def main(role):
    check(role in ('a', 'b'), 'invalid benchmark arm')
    pair = Path(os.environ['PAIR_ROOT']).resolve()
    evidence = pair / 'evidence'
    arm = evidence / role
    arm.mkdir(mode=0o700, parents=True, exist_ok=True)
    rec = Recorder(arm / 'result.json')
    for key in RECEIPTS:
        rec.record('receipt.' + key, lambda: load_receipt(rec, key))
    data = rec.data
    data.update(version=os.environ['SONIC_IMAGE_VERSION'], runner=os.environ['RUNNER_NAME'])
    rec.record('git.revision', lambda: data.update(commit=cmd('git', 'rev-parse', 'HEAD')))
    for key, path in [('checkout', ROOT), ('local_bazel', os.environ['SONIC_BAZEL_CACHE_SOURCE']),
                      ('host_cache', os.environ['SONIC_HOSTBASE_CACHE_SOURCE'])]:
        rec.record('workspace.' + key, lambda: data.update({key: str(Path(path).resolve().relative_to(pair))}))
    def builders():
        ids = cmd('docker', 'ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=' + os.environ['SONIC_CI_CONTAINER_LABEL']).split()
        check(all(re.fullmatch(HEX, value) for value in ids), 'invalid Docker identifier')
        data['builders'] = ids
        check(ids, 'missing retained installer container')
    rec.record('workspace.builders', builders)
    rec.record('make.inputs', lambda: packages(rec))
    for pkg in PRODUCERS:
        rec.record('producer.' + pkg.name, lambda: built(rec, pkg))
    rec.record('installer.container', lambda: installed(rec, data['version'], data.get('commit', '')))
    if role == 'a':
        for key in RECEIPTS:
            rec.record('seed.' + key, lambda: check(data[key]['cache_hit'] is False, 'seed was not cold'))
    else:
        rec.record('comparison', lambda: compare(rec, read(evidence / 'a/result.json'), data, evidence))
    data['verification_complete'] = True
    rec.flush()
    print(f"{role}: {len(data['package_candidates'])} candidate files, {len(data['validation_errors'])} validation errors", flush=True)
    return int(bool(data['validation_errors']))

if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
