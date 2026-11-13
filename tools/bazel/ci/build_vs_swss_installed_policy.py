#!/usr/bin/env python3
"""Build the existing AMD64 SWSS installed policy and bind its archive hashes.

This one-time caller reuses the native managed-cache setup, the existing image
owner's Make/options, the no-DEB action audit, and the existing artifact exporter.
It does not convert images or replace native/full-VS acceptance.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess
import sys

TARGET = "//tools/bazel/tests:swss_installed_images_policy"
POLICY_NAME = "swss-installed-images-policy.json"
MAX_POLICY = 2 * 1024**2
MAKE_ARGUMENTS = [
    "make", "-f", "tools/bazel/prepare_manifests.mk",
    "MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-orchagent.mk",
    "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "BLDENV=trixie", "CONFIGURED_PLATFORM=vs", "CONFIGURED_ARCH=amd64",
    "INCLUDE_FIPS=y", "ENABLE_ASAN=n", "DBG_IMAGE_MARK=dbg", "DOCKERS_PATH=dockers",
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_plan(path):
    value = json.loads(path.read_bytes())
    require(value['schema'] == 1 and value['target'] == TARGET and
            value['architecture'] == 'amd64' and value['policy_name'] == POLICY_NAME,
            'Different policy scope')
    require(set(value['expected_archives']) == {
        'docker-config-engine-trixie', 'docker-orchagent', 'docker-orchagent-dbg'},
        'Different archive set')
    return value


def source_check(source, revision):
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    require(actual == revision, 'Source revision differs')
    subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', '--ignore-submodules=none', 'HEAD'], check=True)


def import_from(source, name):
    require(not any(key == 'tools' or key.startswith('tools.') for key in sys.modules),
            'Owner import requires a fresh process')
    sys.path.insert(0, str(source))
    module = importlib.import_module(name)
    for key, value in tuple(sys.modules.items()):
        if key.startswith('tools.') and getattr(value, '__file__', None):
            require(Path(value.__file__).resolve().is_relative_to(source), 'Mixed source owners')
    return module


def prepare_cache(plan, source, native, scratch, archives, mirrors):
    source_check(source, plan['source_revision'])
    source_check(native, plan['native_revision'])
    for name, expected in plan['native_files'].items():
        path = native / name
        require(not path.is_symlink() and path.is_file(), 'Different native source type')
        raw = path.read_bytes()
        require(len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'],
                'Native source seal differs')
    worker = import_from(native, 'tools.bazel.ci.source_image_worker')
    preflight = importlib.import_module('tools.bazel.ci.source_worker_preflight')
    require(platform.machine() == 'x86_64' and
            platform.freedesktop_os_release().get('VERSION_CODENAME') == 'trixie',
            'Expected native AMD64/Trixie')
    memory = int(Path('/sys/fs/cgroup/memory.max').read_text())
    used = int(Path('/sys/fs/cgroup/memory.current').read_text())
    quota = preflight.cpu_limit(Path('/sys/fs/cgroup/cpu.max').read_text())
    measured_profile = preflight.select_build_profile(memory, quota)
    require(0 <= used < memory and shutil.disk_usage(scratch).free >= plan['minimum_scratch_bytes'],
            'Insufficient private scratch or memory')
    require(not (source / '.bazelrc.user').exists(), 'Unexpected local source rc')
    selected = shutil.which('bazel', path='/usr/local/bin:/usr/bin:/bin')
    if selected is None:
        selected = shutil.which('bazelisk', path='/usr/local/bin:/usr/bin:/bin')
    require(selected is not None, 'Installed Bazel launcher unavailable')
    binary = Path(selected).resolve(strict=True)
    info = binary.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and info.st_mode & 0o111 and info.st_size <= 128 * 1024**2,
            'Installed Bazel launcher ownership differs')
    require(hashlib.sha256(binary.read_bytes()).hexdigest() == plan['bazel_launcher_sha256'],
            'Installed Bazel launcher differs from qualified source')
    cache = scratch / 'cache-setup'
    cache.mkdir(mode=0o700)
    require(os.environ.get('SOURCE_INPUTS_RUN') == str(plan['source_inputs']['run_id']),
            'Different original source-input run')
    require(mirrors.name == 'source_mirrors.cfg' and mirrors.parent.name == 'ci',
            'Different source mirror owner path')
    source_check(mirrors.parent.parent, plan['source_mirror_revision'])
    distdirs, verified_downloads = worker.prepare_source_downloads(archives, cache)
    mirror = scratch / 'source-mirrors.cfg'
    mirror.write_bytes(worker.mirror_configuration(mirrors.read_bytes()))
    mirror.chmod(0o600)
    (source / '.bazelrc.user').write_text('\n'.join([
        'common --repository_cache=', 'common --disk_cache=',
        'common --experimental_downloader_config=' + str(mirror),
        *['common --distdir=' + str(directory) for directory in distdirs],
    ]) + '\n')
    configuration = worker.prepare_remote_cache(source, cache)
    launcher = scratch / 'native-bazel'
    launcher.write_text(worker.launcher_text(str(binary), scratch / 'output-base', measured_profile, configuration))
    launcher.chmod(0o700)
    proxy = scratch / 'review-bazel'
    proxy.write_text('#!/bin/sh\nexec ' + shlex.join([
        str(source / 'tools/bazel/ci/review_bazel.sh'), '--batch',
        '--output_user_root=' + str(scratch / 'output-user-root')]) + ' "$@"\n')
    proxy.chmod(0o700)
    (scratch / 'resources.json').write_text(json.dumps({
        'architecture': 'amd64', 'memory_max': memory, 'memory_current': used,
        'cpu_limit': quota, 'owner_profile': measured_profile,
        'selected_profile': measured_profile, 'scratch_free': shutil.disk_usage(scratch).free,
        'disk_cache': False, 'repository_cache': False, 'source_inputs': verified_downloads,
    }, sort_keys=True) + '\n')


def validate_policy(path, plan):
    require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_POLICY,
            'Policy file is absent or outside bounds')
    raw = path.read_bytes()
    value = json.loads(raw)
    require(value.get('architecture') == 'amd64', 'Policy architecture differs')
    rows = value.get('images')
    require(isinstance(rows, list) and len(rows) == 3, 'Policy image set differs')
    seen = set()
    mismatches = []
    for row in rows:
        name = row.get('name')
        require(name in plan['expected_archives'] and name not in seen, 'Policy image differs or repeats')
        require(row.get('archive') == name + '.gz' and row.get('tag') == name + ':latest',
                'Policy archive or tag differs')
        actual = row.get('archive_sha256')
        expected = plan['expected_archives'][name]['sha256']
        require(all(isinstance(value, str) and len(value) == 64 and
                    all(c in '0123456789abcdef' for c in value)
                    for value in (actual, expected)), 'Invalid policy archive hash')
        if actual != expected:
            mismatches.append({'name': name, 'expected_sha256': expected, 'actual_sha256': actual})
        seen.add(name)
    require(not mismatches, 'Policy does not match the frozen original archive: ' +
            json.dumps(mismatches, sort_keys=True, separators=(',', ':')))
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def build_policy(plan, source, scratch, output, owners):
    """The audit must succeed before the existing build/export helper runs."""
    image_owner, audit, builder, command_log, exporter = owners
    expected_environment = {
        'BAZEL_REAL': str(scratch / 'native-bazel'),
        'SONIC_BAZEL_REGISTRY_REVISION': plan['catalog_revision'],
        'USE_BAZEL_VERSION': '8.5.1', 'BAZELISK_HOME': str(scratch / 'bazelisk'),
        'SOURCE_INPUTS_RUN': str(plan['source_inputs']['run_id']),
        'HOME': str(scratch / 'home'), 'TMPDIR': str(scratch / 'temporary'),
    }
    require(all(os.environ.get(key) == value for key, value in expected_environment.items()),
            'Build environment differs from the prepared remote-only launcher')
    private = scratch / 'policy-build'
    private.mkdir(mode=0o700)
    receipt = {'schema': 1, 'commands': [], 'status': 'preparing', 'target': TARGET,
               'source_revision': plan['source_revision'], 'catalog_revision': plan['catalog_revision']}
    try:
        require(image_owner.identity(private) == plan['source_revision'], 'Image owner source revision differs')
        command_log.execute(MAKE_ARGUMENTS, private, receipt, 'manifests', cwd=source)
        receipt['status'] = 'auditing'
        receipt['action_audit'] = audit.audit_targets(
            str(scratch / 'review-bazel'), image_owner.OPTIONS, [TARGET],
            workspace=source, output=private / 'action-audit.json')
        receipt['status'] = 'building'
        paths = builder.collect_archives(source, private, receipt, {POLICY_NAME: TARGET},
                                         bazel=str(scratch / 'review-bazel'), options=image_owner.OPTIONS)
        receipt['status'] = 'binding'
        identity = validate_policy(paths[POLICY_NAME], plan)
        require(image_owner.identity(private) == plan['source_revision'], 'Image owner source changed')
        output.mkdir(mode=0o700)
        exporter(paths[POLICY_NAME], output / POLICY_NAME)
        require(validate_policy(output / POLICY_NAME, plan) == identity, 'Published policy copy differs')
        result = {'schema': 1, 'status': 'POLICY_MATCHES_FROZEN_ORIGINAL_ARCHIVES',
                  'target': TARGET, 'source_revision': plan['source_revision'],
                  'catalog_revision': plan['catalog_revision'], 'native_cache_controller': plan['native_revision'],
                  'policy': identity, 'original_archives': plan['expected_archives'],
                  'action_audit': receipt['action_audit'], 'native_acceptance': False, 'VS_acceptance': False}
        (output / 'policy-reuse.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
        receipt['status'] = 'passed'
        return result
    finally:
        (private / 'private-build-receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('cache', 'build'))
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--native', required=True, type=Path)
    parser.add_argument('--scratch', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--archives', required=True, type=Path)
    parser.add_argument('--mirrors', required=True, type=Path)
    args = parser.parse_args()
    plan = load_plan(args.plan)
    source, native, scratch = (path.resolve(strict=True) for path in (args.source, args.native, args.scratch))
    require(len({source, native, scratch}) == 3 and not any(a.is_relative_to(b)
            for a in (source, native, scratch) for b in (source, native, scratch) if a != b),
            'Source and private scratch must be separate')
    if args.phase == 'cache':
        prepare_cache(plan, source, native, scratch, args.archives.resolve(strict=True), args.mirrors.resolve(strict=True))
    else:
        source_check(source, plan['source_revision'])
        image_owner = import_from(source, 'tools.bazel.ci.orchagent_image')
        audit = importlib.import_module('tools.bazel.ci.bazel_commands')
        builder = importlib.import_module('tools.bazel.ci.build')
        logs = importlib.import_module('tools.bazel.ci.command_log')
        exporter = importlib.import_module('tools.bazel.build_helpers').export_archive
        build_policy(plan, source, scratch, args.output,
                     (image_owner, audit, builder, logs, exporter))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
