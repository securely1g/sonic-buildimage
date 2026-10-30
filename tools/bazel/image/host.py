#!/usr/bin/env python3
"""Finalize a declared VS host snapshot independently of service image layers.

This local privileged action restores only its declared pre-container snapshot
and runs native post-boundary scripts in private mount/PID/network namespaces.
The source tar must contain rendered native service files and their inputs;
config.json records the template environment and snapshot identity. Its Docker
store is temporary and never becomes an output of this action.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metadata import staging_archive

STATE_FIELDS = ('build_version', 'debian_version', 'kernel_version', 'asic_type',
                'asic_subtype', 'commit_id', 'branch', 'release', 'build_date',
                'build_number', 'built_by', 'sonic_os_version')
IDENTITY_FIELDS = ('arch', 'platform', 'machine', 'image_type', 'distro',
                   'image_version', 'source_commit', 'source_branch', 'source_date_epoch')
# The upstream extension skeleton only parses arguments and prints two lines.
# Custom hooks can inspect service files and would invalidate the metadata-only
# dependency boundary; accept the reviewed no-op script, not arbitrary hooks.
NOOP_ORGANIZATION_EXTENSION_SHA256 = '3df60a480aefbb2bea60326c787b2b1fe43dee1a6ed0c0d3ec608f25c13f3e25'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def load_config(path):
    config = json.loads(Path(path).read_text())
    if set(config) != {'schema', 'environment', 'identity'} or config['schema'] != 1:
        raise ValueError('invalid host configuration schema')
    identity = config['identity']
    if set(identity) != set(IDENTITY_FIELDS) or any(not isinstance(v, str) or '\0' in v for v in identity.values()):
        raise ValueError('invalid snapshot identity')
    required = {'arch': 'amd64', 'platform': 'vs', 'machine': 'vs', 'image_type': 'onie', 'distro': 'trixie'}
    if any(identity[k] != v for k, v in required.items()):
        raise ValueError('host finalization supports amd64 Trixie VS ONIE only')
    env = config['environment']
    if not isinstance(env, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', k) or not isinstance(v, str) or '\0' in v for k, v in env.items()):
        raise ValueError('host environment must contain string variables')
    if env.get('SONIC_IMAGE_VERSION') != identity['image_version']:
        raise ValueError('SONIC_IMAGE_VERSION does not match snapshot identity')
    identity_environment = {
        'CONFIGURED_ARCH': 'arch', 'CONFIGURED_PLATFORM': 'platform',
        'TARGET_MACHINE': 'machine', 'IMAGE_TYPE': 'image_type',
        'IMAGE_DISTRO': 'distro', 'SOURCE_DATE_EPOCH': 'source_date_epoch',
        'SONIC_BAZEL_SOURCE_COMMIT': 'source_commit',
        'SONIC_BAZEL_SOURCE_BRANCH': 'source_branch',
    }
    for variable, field in identity_environment.items():
        if variable in env and env[variable] != identity[field]:
            raise ValueError(variable + ' does not match snapshot identity')
    # Snapshot provisioning owns login state; credentials are unnecessary here.
    if any((k.upper() in {'PASSWORD', 'BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD'} or k.upper().endswith(('_TOKEN', '_SECRET'))) and v for k, v in env.items()):
        raise ValueError('credentials must not be stored in host configuration')
    for key in ('include_kubernetes', 'include_kubernetes_master', 'ENABLE_SBOM', 'BUILD_REDUCE_IMAGE_SIZE', 'MULTIARCH_QEMU_ENVIRON', 'CROSS_BUILD_ENVIRON', 'DEBUG_IMG'):
        if env.get(key, '') not in ('', 'n'):
            raise ValueError('unsupported host option: ' + key)
    if env.get('sonic_packages', '').strip():
        raise ValueError('remote packages are not supported by the offline host action')
    if env.get('SECURE_UPGRADE_MODE', 'no_sign') != 'no_sign':
        raise ValueError('host action supports unsigned images only')
    if not re.fullmatch(r'[0-9]+', identity['source_date_epoch']):
        raise ValueError('source_date_epoch must be nonnegative')
    return config


def image_names(env):
    installed = []
    for value in env.get('installer_images', '').split():
        _package, _path, machine, image = value.split('|')
        if machine and machine != 'vs':
            continue
        path = image.rsplit(':', 1)[0]
        if PurePosixPath(path).parent != PurePosixPath('target'):
            raise ValueError('builtin image must be immediately below target/')
        installed.append(PurePosixPath(path).name)
    local = []
    for value in env.get('sonic_local_packages', '').split():
        _name, path, _owner, _enabled = value.split('|')
        if PurePosixPath(path).parent != PurePosixPath('target'):
            raise ValueError('local image must be immediately below target/')
        local.append(PurePosixPath(path).name)
    if not installed or len(installed) != len(set(installed)) or len(local) != len(set(local)) or set(installed) & set(local):
        raise ValueError('invalid or overlapping host image names')
    return set(installed), set(local)


def mappings(values):
    result = {}
    for value in values:
        name, path = value.split('=', 1)
        if PurePosixPath(name).name != name or not re.fullmatch(r'[A-Za-z0-9_.-]+\.gz', name) or name in result:
            raise ValueError('invalid or duplicate image mapping: ' + name)
        result[name] = str(Path(path).resolve(strict=True))
    return result


def validate_archive(path):
    # The tar is a source bundle, not a root filesystem. Only source-like
    # entries are accepted, and link targets must remain inside the bundle.
    with tarfile.open(path, 'r:*') as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            p = PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError('unsafe source archive member: ' + member.name)
            if member.issym() or member.islnk():
                target = member.linkname
                joined = posixpath.normpath(posixpath.join(str(p.parent) if member.issym() else '', target))
                if target.startswith('/') or joined == '..' or joined.startswith('../'):
                    raise ValueError('source archive link escapes the bundle: ' + member.name)


def snapshot_state(path, expected):
    state = json.loads(Path(path).read_text())
    if set(state) != {'format_version', 'boundary', 'identity', 'state'} or state['format_version'] != 1 or state['boundary'] != 'before-container-loading':
        raise ValueError('unsupported pre-container snapshot state')
    if state['identity'] != expected:
        raise ValueError('snapshot identity does not match declared host configuration')
    values = state['state']
    if set(values) != set(STATE_FIELDS) or any(not isinstance(v, str) or '\0' in v for v in values.values()):
        raise ValueError('invalid snapshot state fields')
    return values


def validate_source_identity(root, expected):
    for name, field in (('.arch', 'arch'), ('.platform', 'platform')):
        path = Path(root) / name
        if path.is_symlink() or not path.is_file() or path.read_text().strip() != expected[field]:
            raise ValueError('source ' + name + ' does not match snapshot identity')


def validate_organization_hooks(root):
    templates = Path(root) / 'files/build_templates'
    if (templates / 'build_debian.organization.sh').exists():
        raise ValueError('organization build hooks are unsupported')
    extension = templates / 'organization_extensions.sh'
    if extension.exists() and digest(extension) != NOOP_ORGANIZATION_EXTENSION_SHA256:
        raise ValueError('custom organization extensions are unsupported')


def worker(request_path):
    request = json.loads(Path(request_path).read_text())
    # Native start-stop-daemon double-forks dockerd. A real init must adopt and
    # reap it; Python as namespace PID 1 leaves a zombie that makes stop fail.
    if os.getppid() != 1 or not os.path.samefile('/proc/1/exe', request['init']):
        raise ValueError('host worker requires the pinned init as namespace PID 1')
    config = load_config(request['config'])
    expected = json.loads(Path(request['execution_environment']).read_text())
    actual = json.loads(Path('/run/sonic-image-worker.json').read_text())
    if actual != expected:
        raise ValueError('image worker does not match the declared execution environment')
    root = Path(request['work']) / 'source'
    root.mkdir()
    subprocess.run(['mount', '--make-rprivate', '/'], check=True)
    # Namespace isolation removes access to the network; native finalization
    # must use already installed packages or declared staged package files.
    subprocess.run(['tar', '-xf', request['source'], '-C', str(root), '--no-same-owner'], check=True)
    validate_source_identity(root, config['identity'])
    validate_organization_hooks(root)
    shutil.copyfile(request['build_script'], root / 'build_debian.sh')
    env = dict(config['environment'])
    env.update({'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                'LC_ALL': 'C', 'LANG': 'C', 'PWD': str(root), 'HOME': '/root',
                'SONIC_BAZEL_HOST_FINALIZE': 'y', 'RFS_SPLIT_LAST_STAGE': 'y',
                'RFS_SPLIT_FIRST_STAGE': 'n', 'SONIC_BAZEL_BUILD_STAGE': '',
                'SOURCE_DATE_EPOCH': config['identity']['source_date_epoch'],
                'SONIC_BAZEL_BOOT_TAR': str(root / 'boot.tar'),
                'SONIC_BAZEL_PLATFORM_TAR': str(root / 'platform.tar.gz')})
    for name in ('PASSWORD', 'BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD'):
        env.pop(name, None)
    env.update({'CONFIGURED_ARCH': 'amd64', 'CONFIGURED_PLATFORM': 'vs', 'TARGET_MACHINE': 'vs', 'IMAGE_TYPE': 'onie', 'IMAGE_DISTRO': 'trixie'})
    fsroot = root / 'fsroot-vs'
    if fsroot.exists() or fsroot.is_symlink():
        raise ValueError('source bundle must not contain a restored host root')
    subprocess.run(['unsquashfs', '-processors', '8', '-d', str(fsroot), request['snapshot']], check=True)
    docker_version = subprocess.check_output(['chroot', str(fsroot), 'dockerd', '--version'], text=True)
    if expected['docker_version'] not in docker_version:
        raise ValueError('snapshot Docker version does not match the declared execution environment')
    state_path = fsroot / '.sonic-bazel-host-state.json'
    env.update(snapshot_state(state_path, config['identity']))
    state_path.unlink()
    target = root / 'target'
    target.mkdir(exist_ok=True)
    for name, path in request['metadata'].items():
        destination = target / name
        destination.unlink(missing_ok=True)
        staging_archive(json.loads(Path(path).read_text()), name, destination)
    for name, path in request['local_images'].items():
        destination = target / name
        destination.unlink(missing_ok=True)
        shutil.copyfile(path, destination)
    # Render the current template against precisely the declared environment,
    # even when the source bundle was prepared with an earlier template.
    template = root / 'files/build_templates/sonic_debian_extension.j2'
    shutil.copyfile(request['extension_template'], template)
    with (root / 'sonic_debian_extension.sh').open('wb') as output:
        subprocess.run(['j2', str(template)], cwd=root, env=env, stdout=output, check=True)
    os.chmod(root / 'sonic_debian_extension.sh', 0o755)
    subprocess.run(['bash', '-n', 'sonic_debian_extension.sh'], cwd=root, env=env, check=True)
    subprocess.run(['bash', 'build_debian.sh'], cwd=root, env=env, check=True)
    for key, relative in (('fs', 'fs.squashfs'), ('boot', 'boot.tar'), ('platform', 'platform.tar.gz')):
        source = root / relative
        if not source.is_file() or not source.stat().st_size:
            raise ValueError('host output is missing: ' + relative)
        shutil.copyfile(source, request[key])
        os.chown(request[key], request['uid'], request['gid'])
        os.chmod(request[key], 0o644)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '_worker':
        worker(sys.argv[2])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source', 'snapshot', 'config', 'build-script', 'extension-template', 'execution-environment', 'fs', 'boot', 'platform', 'receipt'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--metadata', action='append', default=[])
    parser.add_argument('--local-image', action='append', default=[])
    args = parser.parse_args()
    started = time.monotonic()
    config = load_config(args.config)
    metadata = mappings(args.metadata)
    local = mappings(args.local_image)
    names = image_names(config['environment'])
    if names != (set(metadata), set(local)):
        raise ValueError('declared image inputs do not match the native service configuration')
    validate_archive(args.source)
    request = {key: str(Path(getattr(args, key)).resolve()) for key in ('source', 'snapshot', 'config', 'build_script', 'extension_template', 'execution_environment', 'fs', 'boot', 'platform')}
    request.update({'metadata': metadata, 'local_images': local, 'uid': os.getuid(), 'gid': os.getgid()})
    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
    # A private PID namespace guarantees that daemon descendants disappear
    # before the action returns, including on build failure.
    output_parent = Path(request['fs']).parent
    output_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sonic-bazel-host-', dir=output_parent) as temp:
        request['work'] = temp
        # The helper ships in the declared snapshot and is therefore pinned by
        # the same action key, unlike Docker's host-injected /sbin/docker-init.
        init = Path(temp) / 'docker-init'
        with init.open('wb') as output:
            subprocess.run(['unsquashfs', '-cat', request['snapshot'], 'usr/libexec/docker/docker-init'], stdout=output, check=True)
        init.chmod(0o755)
        request['init'] = str(init)
        request_path = Path(temp) / 'request.json'
        request_path.write_text(json.dumps(request))
        try:
            subprocess.run(prefix + ['unshare', '--mount', '--pid', '--fork', '--kill-child', '--mount-proc', '--net', str(init), '--', sys.executable, str(Path(__file__).resolve()), '_worker', str(request_path)], check=True)
        finally:
            subprocess.run(prefix + ['rm', '-rf', '--', str(Path(temp) / 'source')], check=True)
    receipt = {'schema': 1, 'status': 'passed', 'seconds': time.monotonic() - started,
               'scope': 'Finalized native host from the pre-container snapshot; temporary metadata-only Docker store discarded',
               'snapshot_identity': config['identity'], 'builtin_metadata_images': sorted(metadata),
               'local_package_images': sorted(local),
               'outputs': {key: {'size': Path(request[key]).stat().st_size, 'sha256': digest(request[key])} for key in ('fs', 'boot', 'platform')}}
    Path(args.receipt).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
