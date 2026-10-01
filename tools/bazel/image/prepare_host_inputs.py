#!/usr/bin/env python3
"""Freeze post-boundary native host inputs after a capture-only Make render.

This preparation step consumes an existing declared pre-container snapshot.
It does not run Make, rebuild services, or take files from a completed installer.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host import digest, image_names, load_config, validate_archive


def prepare(source, captured, snapshot, output):
    source, captured, snapshot, output = map(Path, (source, captured, snapshot, output))
    env = json.loads(captured.read_text())
    # Captured values must be literal strings; refuse credential-bearing fields
    # rather than writing secrets to an otherwise public build-input directory.
    for key, value in env.items():
        if not isinstance(value, str):
            raise ValueError('environment values must be strings')
        if (key.upper() in {'PASSWORD', 'BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD'} or key.upper().endswith(('_TOKEN', '_SECRET'))) and value:
            raise ValueError('capture contains credentials: ' + key)
    env = {k: v for k, v in env.items() if k not in {'_', 'SONIC_BAZEL_BUILD_STAGE', 'SONIC_BAZEL_IMAGE_STAGE', 'SONIC_BAZEL_REQUESTED_STAGE'}}
    identity = {'arch': env['CONFIGURED_ARCH'], 'platform': env['CONFIGURED_PLATFORM'],
                'machine': env['TARGET_MACHINE'], 'image_type': env['IMAGE_TYPE'],
                'distro': env['IMAGE_DISTRO'], 'image_version': env['SONIC_IMAGE_VERSION'],
                'source_commit': env['SONIC_BAZEL_SOURCE_COMMIT'],
                'source_branch': env['SONIC_BAZEL_SOURCE_BRANCH'],
                'source_date_epoch': env['SOURCE_DATE_EPOCH']}
    output.mkdir(parents=True, exist_ok=True)
    config = output / 'host-config.json'
    config.write_text(json.dumps({'schema': 1, 'identity': identity, 'environment': env}, sort_keys=True, indent=2) + '\n')
    load_config(config)
    image_names(env)
    selected = set()
    def add(name):
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('source input escapes native tree: ' + name)
        local = source / path
        if not local.exists() and not local.is_symlink():
            raise ValueError('required native input missing: ' + name)
        selected.add(path.as_posix())
    for name in ('.arch', '.platform', 'functions.sh', 'onie-image.conf', 'files', 'scripts', 'src/sonic-build-hooks'):
        add(name)
    for variable in ('installer_start_scripts', 'installer_services'):
        for name in env[variable].split():
            # Native templates intentionally leave a few optional services absent.
            for candidate in (name, name.replace('@', ''), env['TARGET_MACHINE'] + '_' + name):
                if (source / candidate).is_file():
                    add(candidate)
    for variable in ('installer_debs', 'installer_python_debs'):
        for name in env.get(variable, '').split():
            add(name)
    for name in env.get('installer_extra_files', '').split():
        add(name.split(':', 1)[0])
    for path in (source / env['debs_path']).glob('syslog-counter_*.deb'):
        add(path.relative_to(source).as_posix())
    epoch = int(identity['source_date_epoch'])
    def normalize(info):
        if '.git' in PurePosixPath(info.name).parts or '__pycache__' in PurePosixPath(info.name).parts:
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mtime = epoch
        info.pax_headers = {}
        return info
    archive = output / 'host-source.tar'
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as result:
        for name in sorted(selected):
            result.add(source / name, arcname=name, recursive=True, filter=normalize)
    validate_archive(archive)
    snapshot_output = output / 'host-onie.squashfs'
    if snapshot.resolve() != snapshot_output.resolve():
        shutil.copyfile(snapshot, snapshot_output)
    receipt = {'schema': 1, 'source_boundary': 'before-container-loading', 'identity': identity,
               'source_members': sorted(selected),
               'artifacts': {p.name: {'size': p.stat().st_size, 'sha256': digest(p)} for p in (archive, config, snapshot_output)}}
    (output / 'host-input-provenance.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'status': 'prepared', 'artifacts': receipt['artifacts']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'captured-environment', 'snapshot', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    prepare(args.source, args.captured_environment, args.snapshot, args.output)
