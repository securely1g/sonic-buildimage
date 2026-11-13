#!/usr/bin/env python3
"""Hash specifically selected existing cache archives without restoring them."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import tarfile
import time
import zlib

from tools.bazel.ci import vs_cache_inspection as observation

ROOT = '/dpkg_cache'
CHUNK = 1024 * 1024
# The largest selected original Docker object is 191,626,678 bytes.
MAX_ARCHIVE = 256 * CHUNK
MAX_EXPANDED = 512 * CHUNK
MAX_TOTAL_EXPANDED = 2 * 1024 * CHUNK
MAX_MEMBERS = 512
MAX_OUTPUT = 4 * CHUNK


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'Duplicate JSON key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


class Budget:
    def __init__(self, seconds=240, expanded=MAX_TOTAL_EXPANDED):
        self.end = time.monotonic() + seconds
        self.remaining = expanded

    def check(self):
        if time.monotonic() >= self.end:
            raise TimeoutError('Cache inspection deadline')


class BoundedReader:
    def __init__(self, source, budget, limit=MAX_EXPANDED):
        self.source, self.budget, self.remaining = source, budget, limit

    def read(self, size=-1):
        self.budget.check()
        require(0 <= size <= MAX_ARCHIVE, 'Unbounded or oversized archive read')
        require(size <= self.remaining and size <= self.budget.remaining,
                'Expanded archive byte limit')
        value = self.source.read(size)
        self.remaining -= len(value)
        self.budget.remaining -= len(value)
        return value


def member_name(name):
    require(isinstance(name, str) and 0 < len(name) <= 1024 and
            not any(c in name for c in '\x00\r\n\\') and not name.startswith('/'),
            'Unsafe archive member name')
    while name.startswith('./'):
        name = name[2:]
    path = PurePosixPath(name)
    require(name and '..' not in path.parts and str(path) == name and
            path.parts[0] == 'target', 'Unexpected archive member path')
    return name


class StrictTarInfo(tarfile.TarInfo):
    @classmethod
    def fromtarfile(cls, archive):
        try:
            return super().fromtarfile(archive)
        except tarfile.EOFHeaderError:
            # A zero block is the actual TAR end marker.
            raise
        except tarfile.HeaderError as error:
            # TarFile.next otherwise treats some malformed later headers and
            # truncated tails as EOF, hiding their already-consumed bytes.
            raise ValueError('Malformed or truncated TAR header') from error


def inspect_stream(raw, expected_paths, budget):
    members, names = [], set()
    with gzip.GzipFile(fileobj=raw, mode='rb') as compressed:
        decoded = BoundedReader(compressed, budget)
        with tarfile.open(fileobj=decoded, mode='r|', tarinfo=StrictTarInfo) as archive:
            for item in archive:
                budget.check()
                require(len(members) < MAX_MEMBERS, 'Archive member limit')
                name = member_name(item.name)
                require(name not in names, 'Duplicate archive member')
                names.add(name)
                require(item.isreg() and not item.issparse() and
                        0 <= item.size <= MAX_ARCHIVE, 'Nonregular or oversized archive member')
                stream = archive.extractfile(item)
                require(stream is not None, 'Unreadable regular archive member')
                digest, count = hashlib.sha256(), 0
                with stream:
                    while True:
                        budget.check()
                        block = stream.read(CHUNK)
                        if not block:
                            break
                        count += len(block)
                        require(count <= item.size, 'Archive member size mismatch')
                        digest.update(block)
                require(count == item.size, 'Truncated archive member')
                members.append({'path': name, 'bytes': count, 'sha256': digest.hexdigest(),
                                'selected_package_or_wheel': name in expected_paths,
                                'content_retained': False})
            # Read through TAR's stream, including bytes already buffered by
            # tarfile. Another TAR or nonzero tail must not be silently omitted
            # from this inventory. Reading to EOF also checks the gzip CRC.
            while True:
                tail = archive.fileobj.read(CHUNK)
                if not tail:
                    break
                require(not tail.strip(b'\0'), 'Nonzero data after TAR end marker')
    return {'members': members, 'missing_expected_paths': sorted(set(expected_paths) - names),
            'archive_semantics': 'Member and byte observation only; no extraction or package acceptance'}


def stable(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def inspect_object(directory, item, budget):
    result = {'name': item['name'], 'status': 'not_observed'}
    fd = None
    try:
        budget.check()
        fd = os.open(item['name'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size == item['prior_metadata']['bytes']
                and 0 < before.st_size <= MAX_ARCHIVE, 'Cache object type or size changed')
        result['metadata'] = observation.metadata(before)
        result['matches_prior_metadata'] = result['metadata'] == item['prior_metadata']
        digest, compressed_count = hashlib.sha256(), 0
        with os.fdopen(os.dup(fd), 'rb') as raw:
            while True:
                budget.check()
                block = raw.read(CHUNK)
                if not block:
                    break
                compressed_count += len(block)
                require(compressed_count <= before.st_size, 'Cache object grew while hashing')
                digest.update(block)
            require(compressed_count == before.st_size, 'Cache object shrank while hashing')
            raw.seek(0)
            result.update(inspect_stream(raw, item['expected_paths'], budget))
        require(stable(before) == stable(os.fstat(fd)), 'Cache object changed during observation')
        result['sha256'] = digest.hexdigest()
        result['status'] = 'CURRENT_BYTES_OBSERVED_NOT_ADMITTED'
    except (OSError, ValueError, TimeoutError, tarfile.TarError, EOFError, zlib.error) as error:
        result = {'name': item['name'], 'status': 'not_fully_observed',
                  'error_type': type(error).__name__}
    finally:
        if fd is not None:
            os.close(fd)
    return result


WORKSPACE_DEBS = ('sonic-device-data_1.0-1_all.deb',
                  'sonic-platform-vs_1.0_amd64.deb')
BUILD_HOOKS_DEB = 'sonic-build-hooks_1.0_all.deb'
BUILD_HOOKS_WORKSPACE_PATH = 'src/sonic-build-hooks/buildinfo/' + BUILD_HOOKS_DEB
BUILD_HOOKS_INSTALLED_BASE = '/usr/local/share/buildinfo'


def _inspect_declared_deb(base, relative, mounts, budget):
    """Hash one exact existing path; retain descriptors and never follow links."""
    require(relative in tuple('target/debs/trixie/' + name for name in WORKSPACE_DEBS)
            + (BUILD_HOOKS_WORKSPACE_PATH, BUILD_HOOKS_DEB), 'Unexpected package path')
    relative_path = PurePosixPath(relative)
    filename = relative_path.name
    path = PurePosixPath(base)
    require(isinstance(base, str) and path.is_absolute() and str(path) == base
            and 1 < len(path.parts) <= 32 and '..' not in path.parts
            and len(base) <= 4096 and not any(c in base for c in '\x00\r\n'),
            'Unexpected current base path')
    result = {'base': base, 'relative_path': relative,
              'status': 'not_observed', 'contents_retained': False}
    descriptors, links = [], []
    identity = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    try:
        budget.check()
        directory = os.open('/', observation.DIRECTORY_FLAGS)
        descriptors.append(directory)
        base_parts = path.parts[1:]
        for index, component in enumerate(base_parts + relative_path.parts[:-1]):
            budget.check()
            named = os.stat(component, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(named.st_mode):
                result['status'] = 'symlink_rejected'; return result
            if not stat.S_ISDIR(named.st_mode):
                result['status'] = 'non_directory'; return result
            child = os.open(component, observation.DIRECTORY_FLAGS, dir_fd=directory)
            descriptors.append(child)
            opened = os.fstat(child)
            require(identity(named) == identity(opened), 'Base directory path changed')
            links.append((directory, component, identity(opened)))
            directory = child
            if index + 1 == len(base_parts):
                result['base_metadata'] = observation.metadata(opened)
                result['base_mount'] = observation.covering_mount(base, mounts)
                require(result['base_mount']['device'] == result['base_metadata']['device'],
                        'Base directory mount changed')
        named = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        result['metadata'] = observation.metadata(named)
        if stat.S_ISLNK(named.st_mode):
            result['status'] = 'symlink_rejected'; return result
        if not stat.S_ISREG(named.st_mode):
            result['status'] = 'non_regular'; return result
        if not 0 <= named.st_size <= MAX_ARCHIVE:
            result['status'] = 'size_limit'; return result
        budget.check()
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
        descriptors.append(fd)
        before = os.fstat(fd)
        require(stable(named) == stable(before) and stat.S_ISREG(before.st_mode),
                'Base directory file changed before reading')
        result['mount'] = observation.covering_mount(base + '/' + relative, mounts)
        require(result['mount']['device'] == result['metadata']['device'], 'File mount changed')
        digest, count = hashlib.sha256(), 0
        while count < before.st_size:
            budget.check()
            block = os.read(fd, min(CHUNK, before.st_size - count))
            require(block, 'Base directory file shrank while hashing')
            count += len(block); digest.update(block)
        require(stable(before) == stable(os.fstat(fd)) ==
                stable(os.stat(filename, dir_fd=directory, follow_symlinks=False)),
                'Base directory file changed during observation')
        for parent, component, expected in links:
            budget.check()
            require(identity(os.stat(component, dir_fd=parent, follow_symlinks=False)) == expected,
                    'Base directory path binding changed during observation')
        result.update(status='CURRENT_BYTES_OBSERVED_NOT_ADMITTED',
                      sha256=digest.hexdigest(), observed_bytes=count)
    except FileNotFoundError:
        result['status'] = 'missing_at_this_exact_path'
    except PermissionError:
        result['status'] = 'permission_denied'
    except (OSError, ValueError, TimeoutError) as error:
        result['status'] = 'not_fully_observed'
        result['error_type'] = type(error).__name__
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return result


def inspect_workspace_deb(workspace, filename, mounts, budget):
    require(filename in WORKSPACE_DEBS, 'Unexpected workspace filename')
    result = _inspect_declared_deb(workspace, 'target/debs/trixie/' + filename, mounts, budget)
    for suffix in ('', '_metadata', '_mount'):
        if 'base' + suffix in result:
            result['workspace' + suffix] = result.pop('base' + suffix)
    if result['status'] == 'CURRENT_BYTES_OBSERVED_NOT_ADMITTED':
        result['status'] = 'CURRENT_WORKSPACE_BYTES_OBSERVED_NOT_ADMITTED'
    return result


def inspect_build_hook_debs(workspace, mounts, budget):
    return {'workspace_copy': _inspect_declared_deb(
                workspace, BUILD_HOOKS_WORKSPACE_PATH, mounts, budget),
            'installed_buildinfo_copy': _inspect_declared_deb(
                BUILD_HOOKS_INSTALLED_BASE, BUILD_HOOKS_DEB, mounts, budget),
            'scope': 'Two exact build-hooks package paths on this assigned worker; no directory scan',
            'maximum_bytes_each': MAX_ARCHIVE,
            'missing_is_global_absence': False,
            'historical_producer_provenance': 'not_established',
            'source_compatibility': 'not_established',
            'package_format_validated': False,
            'contents_copied_or_extracted': False}


def inspect_workspace_debs(workspace, mounts, budget):
    return {'workspace': workspace,
            'paths': [inspect_workspace_deb(workspace, name, mounts, budget)
                      for name in WORKSPACE_DEBS],
            'scope': 'Only two exact paths in this assigned worker workspace; no directory scan',
            'maximum_bytes_each': MAX_ARCHIVE,
            'missing_is_global_absence': False,
            'historical_producer_provenance': 'not_established',
            'source_compatibility': 'not_established',
            'package_format_validated': False,
            'contents_copied_or_extracted': False}


def manifest(path, expected_sha256):
    require(re.fullmatch(r'[0-9a-f]{64}', expected_sha256), 'Invalid selection hash')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        initial = os.fstat(fd)
        require(stat.S_ISREG(initial.st_mode) and initial.st_size <= CHUNK, 'Invalid selection file')
        raw = os.read(fd, CHUNK + 1)
        require(len(raw) == initial.st_size and stable(initial) == stable(os.fstat(fd)) and
                hashlib.sha256(raw).hexdigest() == expected_sha256, 'Selection changed or hash differs')
    finally:
        os.close(fd)
    value = strict_json(raw)
    require(value['schema'] == 1 and value['root'] == ROOT and
            0 < len(value['objects']) <= 128, 'Unexpected cache selection')
    names = set()
    for row in value['objects']:
        name = row['name']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+._-]*-[0-9a-f]{23}-[0-9a-f]{23}\.tgz', name)
                and name not in names, 'Invalid or duplicate selected cache name')
        names.add(name)
        require(type(row['prior_metadata']['bytes']) is int and
                0 < row['prior_metadata']['bytes'] <= MAX_ARCHIVE, 'Invalid expected cache size')
        require(row['expected_paths'] and len(row['expected_paths']) <= 64 and
                all(member_name(p) == p for p in row['expected_paths']), 'Invalid selected output paths')
    require(sum(r['prior_metadata']['bytes'] for r in value['objects']) <= 512 * CHUNK,
            'Compressed archive selection limit')
    return value


def collect(value, identity):
    budget = Budget()
    directory = os.open(ROOT, observation.DIRECTORY_FLAGS)
    try:
        opened = os.fstat(directory)
        with open('/proc/self/mountinfo', 'rb') as source:
            raw = source.read(2 * CHUNK + 1)
        require(len(raw) <= 2 * CHUNK, 'Mount metadata limit')
        mounts = observation.parse_mounts(raw.decode())
        mount = observation.covering_mount(ROOT, mounts)
        workspace = inspect_workspace_debs(os.environ['GITHUB_WORKSPACE'], mounts, budget)
        hooks = inspect_build_hook_debs(os.environ['GITHUB_WORKSPACE'], mounts, budget)
        require(mount['device'] == observation.metadata(opened)['device'], 'Cache mount changed')
        results = [inspect_object(directory, item, budget) for item in value['objects']]
        require((opened.st_dev, opened.st_ino) == (os.fstat(directory).st_dev, os.fstat(directory).st_ino),
                'Cache directory changed')
    finally:
        os.close(directory)
    return {'schema': 1, 'status': 'CACHE_PAYLOAD_OBSERVATION_NOT_ADMISSION', **identity,
            'root': ROOT, 'mount': mount, 'objects': results,
            'current_workspace_host_debs': workspace,
            'bootstrap_build_hooks_debs': hooks,
            'observed_objects': sum(r['status'] == 'CURRENT_BYTES_OBSERVED_NOT_ADMITTED' for r in results),
            'incomplete_objects': sum(r['status'] != 'CURRENT_BYTES_OBSERVED_NOT_ADMITTED' for r in results),
            'scope': 'Only selected existing cache names, two current-workspace host DEB paths and two build-hooks package paths; hashes and metadata, no raw logs or payload copies retained',
            'cache_writes': False, 'payloads_extracted': False, 'package_generation': False,
            'source_key_compatibility': 'not_established', 'complete_VS_acceptance': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--selection-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    identity = observation.identity(args.worker, os.environ, observation.marker())
    selected = manifest(args.selection, args.selection_sha256)
    require(args.output.parent == Path(os.environ['RUNNER_TEMP']) and
            not args.output.exists() and not args.output.is_symlink(), 'Output must be a fresh runner-temp file')
    def deadline(_signum, _frame):
        raise TimeoutError('Cache payload observation deadline')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(250)
    try:
        result = collect(selected, identity)
        result['selection_sha256'] = args.selection_sha256
        raw = (json.dumps(result, indent=2, sort_keys=True) + '\n').encode()
        require(len(raw) <= MAX_OUTPUT, 'Observation output limit')
        with args.output.open('xb') as output:
            output.write(raw)
        print(json.dumps({'status': result['status'], 'objects': len(result['objects']),
                          'sha256': hashlib.sha256(raw).hexdigest()}))
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    main()
