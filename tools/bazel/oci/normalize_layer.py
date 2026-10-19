"""Normalize declared payload paths against a checked OCI base's directory links."""

import argparse
import copy
import json
from pathlib import Path, PurePosixPath
import tarfile

from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci.oci_layout import validate_layout


DIRECTORY_ALIASES = {name: {'linkname': target, 'target': target} for name, target in {
    'bin': 'usr/bin', 'lib': 'usr/lib', 'lib64': 'usr/lib64', 'sbin': 'usr/sbin',
}.items()}
DIRECTORY_ALIASES['var/run'] = {'linkname': '/run', 'target': 'run'}


def path_name(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and '..' not in path.parts, 'unsafe package payload path: ' + value)
    return str(path)


def normalized_path(value, *, directory_aliases=DIRECTORY_ALIASES):
    """Rewrite an image-relative path without following any build-host links."""
    name = path_name(value)
    for alias in sorted(directory_aliases, key=len, reverse=True):
        entry = directory_aliases[alias]
        if name == alias or name.startswith(alias + '/'):
            return entry['target'] + name[len(alias):]
    return name


def same_alias_link(value, expected):
    """Accept leading ./ on relative alias targets without resolving parent paths."""
    if value == expected:
        return True
    if PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or PurePosixPath(expected).is_absolute():
        return False
    while value.startswith('./'):
        value = value[2:]
    return value == expected


def normalized_member(member, *, directory_aliases=DIRECTORY_ALIASES, root_owned=False, modes=None):
    """Copy one supported member, preserving imported owners unless explicitly changed."""
    name = path_name(member.name)
    require(not PurePosixPath(name).name.startswith('.wh.'), 'package payload uses a reserved OCI whiteout path: ' + name)
    require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
            'unsupported package payload member: ' + name)
    require(member.sparse is None, 'sparse package payload member requires review: ' + name)
    output = copy.copy(member)
    output.pax_headers = dict(member.pax_headers)
    if root_owned:
        output.uid = output.gid = 0
        output.uname = output.gname = ''
        for field in ('uid', 'gid', 'uname', 'gname'):
            output.pax_headers.pop(field, None)
        if output.issym():
            output.mode = 0o777
    if name in directory_aliases:
        require(output.isdir() or (output.issym() and same_alias_link(output.linkname, directory_aliases[name]['linkname'])),
                'package payload changes a directory alias: ' + name)
        require(output.uid == 0 and output.gid == 0 and output.mode == (0o755 if output.isdir() else 0o777),
                'package payload changes directory alias metadata: ' + name)
        return None
    normalized = normalized_path(member.name, directory_aliases=directory_aliases)
    output.name = './' + normalized if normalized != '.' else './'
    output.pax_headers.pop('path', None)
    output.pax_headers.pop('linkpath', None)
    if member.islnk():
        target = path_name(member.linkname)
        require(target not in directory_aliases, 'package hardlink targets a directory alias: ' + name)
        output.linkname = './' + normalized_path(member.linkname, directory_aliases=directory_aliases)
    if modes and normalized in modes:
        value = modes[normalized]
        output.mode = int(value, 8) if isinstance(value, str) else value
        require(isinstance(output.mode, int) and 0 <= output.mode <= 0o7777, 'invalid mode for ' + normalized)
    return output


def base_layers(base, expected_platform):
    """Read an OCI layout or the real installed filesystem used by apt_install."""
    if base.is_dir():
        descriptor, _, _, layers = validate_layout(base, expected_platform)
        return descriptor['digest'], layers
    require(base.is_file(), 'base is neither an OCI layout nor an installed rootfs TAR')
    return 'sha256:' + sha(base), [base]


def base_aliases(base, *, expected_platform, directory_aliases=DIRECTORY_ALIASES):
    """Require aliases and their target directories to exist in the final checked base."""
    digest, layers = base_layers(base, expected_platform)
    targets = set(directory_aliases) | {entry['target'] for entry in directory_aliases.values()}
    observed = {}
    for layer in layers:
        additions, removals = {}, set()
        with tarfile.open(layer, 'r:*') as archive:
            for member in archive:
                name = path_name(member.name)
                pure = PurePosixPath(name)
                if name in targets:
                    linkname = member.linkname
                    if member.issym() and name in directory_aliases and same_alias_link(linkname, directory_aliases[name]['linkname']):
                        linkname = directory_aliases[name]['linkname']
                    additions[name] = {'kind': 'symlink' if member.issym() else 'directory' if member.isdir() else 'other',
                                       'linkname': linkname, 'uid': member.uid, 'gid': member.gid, 'mode': member.mode}
                elif pure.name == '.wh..wh..opq':
                    parent = str(pure.parent)
                    removals.update(target for target in targets if parent == '.' or target.startswith(parent + '/'))
                elif pure.name.startswith('.wh.'):
                    hidden = str(pure.parent / pure.name[4:])
                    removals.update(target for target in targets if target == hidden or target.startswith(hidden + '/'))
        for name in removals:
            observed.pop(name, None)
        observed.update(additions)
    for name, entry in directory_aliases.items():
        require(observed.get(name) == {'kind': 'symlink', 'linkname': entry['linkname'], 'uid': 0, 'gid': 0, 'mode': 0o777} and
                observed.get(entry['target']) == {'kind': 'directory', 'linkname': '', 'uid': 0, 'gid': 0, 'mode': 0o755},
                'OCI base has an unsupported directory alias: ' + name)
    return digest


def normalize(payload_path, base, output_path, *, expected_platform, directory_aliases=DIRECTORY_ALIASES,
              root_owned=False, modes=None, preserve_metadata=()):
    """Produce an ordinary TAR and a receipt for the exact source/base and requested changes."""
    for alias, entry in directory_aliases.items():
        require(path_name(alias) == alias and path_name(entry['target']) == entry['target'], 'invalid directory alias')
    base_digest = base_aliases(base, expected_platform=expected_platform, directory_aliases=directory_aliases)
    inherited = {}
    if preserve_metadata:
        require(len(set(preserve_metadata)) == len(preserve_metadata), 'duplicate metadata preservation path')
        require(not set(preserve_metadata).intersection(modes or {}), 'mode override conflicts with preserved metadata')
        _, layers = base_layers(base, expected_platform)
        from tools.bazel.oci.oci_inventory import apply_layer
        files = {}
        for layer in layers:
            apply_layer(layer, files)
        for path in preserve_metadata:
            require(path_name(path) == path, 'unsafe metadata preservation path')
            item = files.get(path, {})
            require(item.get('kind') == 'file', 'baseline must supply a regular file for metadata preservation: ' + path)
            inherited[path] = {key: item[key] for key in ('mode', 'uid', 'gid')}
    retained = set()
    counts = {'input_members': 0, 'output_members': 0, 'rewritten_members': 0, 'skipped_alias_entries': 0}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(payload_path, 'r:*') as source, tarfile.open(output_path, 'w', format=tarfile.PAX_FORMAT) as output:
        for member in source:
            counts['input_members'] += 1
            normalized = normalized_member(member, directory_aliases=directory_aliases, root_owned=root_owned, modes=modes)
            if normalized is None:
                counts['skipped_alias_entries'] += 1
                continue
            name = path_name(normalized.name)
            if name in inherited:
                require(normalized.isfile(), 'source must supply a regular file for metadata preservation: ' + name)
                for key, value in inherited[name].items():
                    setattr(normalized, key, value)
                normalized.uname = normalized.gname = ''
                for key in ('uid', 'gid', 'uname', 'gname', 'mode'):
                    normalized.pax_headers.pop(key, None)
                retained.add(name)
            counts['output_members'] += 1
            counts['rewritten_members'] += str(PurePosixPath(normalized.name)) != str(PurePosixPath(member.name))
            output.addfile(normalized, source.extractfile(member) if member.isfile() else None)
    require(retained == set(preserve_metadata), 'source TAR lacks a metadata preservation path')
    return {'base_manifest_digest': base_digest, 'input_sha256': sha(payload_path), 'output_sha256': sha(output_path),
            'directory_aliases': directory_aliases, 'root_owned': root_owned, 'modes': modes or {}, 'preserved_metadata': inherited, **counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src', required=True, type=Path)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--policy', required=True, type=Path)
    args = parser.parse_args()
    result = normalize(args.src, args.base, args.output, **json.loads(args.policy.read_text()))
    args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
