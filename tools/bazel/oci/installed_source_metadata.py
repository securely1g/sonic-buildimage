"""Verify source-package provenance against a filesystem installed by APT.

This is read-only validation. Debian's own installer supplies the package
status, controls and file ownership; this tool never writes dpkg state.
"""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

from sonic_apt import dependencies
from tools.bazel.oci.oci_inventory import apply_layer, resolve_path


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def selection_provenance(path, controls, architecture):
    """Keep the runtime Make/source identity only after the planned set matches."""
    selection = json.loads(path.read_text())
    # The current selection receipt records Architecture in every package
    # control, rather than requiring a separate top-level architecture field.
    require(isinstance(selection, dict) and type(selection.get('schema')) is int and selection['schema'] == 1 and
            selection.get('architecture', architecture) == architecture and
            selection.get('variant') == 'runtime', 'invalid runtime package selection identity')
    planned = selection.get('dependency_check', {})
    require(isinstance(planned, dict) and planned.get('status') == 'satisfied' and planned.get('packages') == controls,
            'runtime package selection differs from actual installed dependency controls')
    fields = {'variant': 'runtime'}
    for name in ('make_manifest_sha256', 'source_receipt_sha256'):
        value = selection.get(name)
        # Source-only selection has no Make envelope. Its source receipt hash
        # remains mandatory and binds the same inputs as the APT selector.
        require((name == 'make_manifest_sha256' and value is None) or
                (isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)),
                'runtime package selection lacks ' + name)
        fields[name] = value
    fields['selection_sha256'] = sha(path)
    return fields


def path_location(path, files):
    """Resolve directory aliases without following the file's own symlink."""
    value = PurePosixPath(path)
    parent = resolve_path(str(value.parent), files)
    require(parent is not None, 'runtime layer has an unsafe parent: ' + path)
    return str(PurePosixPath(parent) / value.name)


def package_state_path(path):
    return any(path == prefix or path.startswith(prefix + '/')
               for prefix in ('var/lib/dpkg', 'var/lib/apt'))


def source_specification(path):
    specification = json.loads(path.read_text())
    if isinstance(specification.get('packages'), list):
        specification['packages'] = {item['package']: {
            'target': item['source'].get('target'),
            'source_revision': item['source'].get('commit', item['source'].get('release')),
            'source_version': item['source'].get('version'),
            'required_paths': list(item['required_paths']),
        } for item in specification['packages']}
    require(specification.get('schema') == 1 and isinstance(specification.get('packages'), dict),
            'invalid source declarations')
    require(all(isinstance(name, str) and re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name)
                for name in specification['packages']), 'invalid source package names')
    return specification


def apt_preferences(specification):
    """Protect source payloads using APT's standard version-selection policy."""
    names = {name for name in specification['packages']}
    return ('# Generated from the source overlay declarations; do not edit.\n'
            '# dpkg retains the real Debian baseline versions. Source provenance\n'
            '# is recorded separately under /usr/share/sonic/bazel.\n'
            'Package: ' + ' '.join(sorted(names)) + '\nPin: version *\nPin-Priority: -1\n')


def describe(rootfs, installation, spec, architecture, inherited_metadata=None, inherited_metadata_path=None,
             selection=None, payloads=None, runtime_layer=None, path_filters=None, allow_same_owner_overlays=False):
    payloads = payloads or {}
    receipt = json.loads(installation.read_text())
    require(receipt.get('status') == 'passed' and receipt.get('architecture') == architecture and
            receipt.get('rootfs_sha256') == sha(rootfs), 'installation receipt does not match the rootfs')
    specification = source_specification(spec)
    require(set(specification['packages']) == set(payloads),
            'source declarations differ from actual source inputs')
    files = {}
    apply_layer(rootfs, files)
    owned = {}
    with tarfile.open(rootfs) as stream:
        entries = [entry for entry in stream if entry.name.removeprefix('./') == 'var/lib/dpkg/status']
        require(len(entries) == 1, 'rootfs must contain one real dpkg status file')
        status = stream.extractfile(entries[0]).read()
        for entry in stream:
            name = entry.name.removeprefix('./')
            if entry.isfile() and name.startswith('var/lib/dpkg/info/') and name.endswith('.list'):
                owner = Path(name).name.removesuffix('.list').split(':', 1)[0]
                for path in stream.extractfile(entry).read().decode().splitlines():
                    path = path.lstrip('/')
                    # dpkg owns the named symlink, not its referent. Only
                    # canonicalize parent-directory aliases (for example
                    # /lib -> /usr/lib), keeping development .so links
                    # separate from the runtime libraries they reference.
                    owned.setdefault(path_location(path, files), set()).add(owner)
    verified_overlays = {}
    if runtime_layer:
        with tarfile.open(runtime_layer) as stream:
            require(not any(Path(entry.name).name.startswith('.wh.') for entry in stream),
                    'source runtime layers must not contain OCI whiteouts')
        additions = {}
        apply_layer(runtime_layer, additions)
        supplied = {}
        if allow_same_owner_overlays:
            for name, archive in payloads.items():
                entries = {}
                apply_layer(archive, entries)
                for path, entry in entries.items():
                    if entry.get('kind') != 'directory':
                        supplied.setdefault(path_location(path, files), {})[name] = entry
        for path, entry in additions.items():
            target = path_location(path, files)
            require(not package_state_path(target) or
                    (entry.get('kind') == 'directory' and entry == files.get(target)),
                    'runtime layer must not change package-manager state: ' + path)
            if entry.get('kind') == 'directory' and files.get(target, {}).get('kind') in (None, 'directory'):
                continue
            owners = owned.get(target, set())
            if owners:
                providers = supplied.get(target, {})
                require(allow_same_owner_overlays and owners <= providers.keys(),
                        'runtime layer overlaps a Debian-owned file: ' + path)
                for owner in owners:
                    original = providers[owner]
                    require(entry['kind'] == original['kind'] and
                            all(entry.get(key) == original.get(key) for key in ('sha256', 'size', 'linkname')),
                            'same-owner overlay differs from its source payload: ' + path)
                # The normalized TAR declares the installed modes and owners.
                # Record its full entries, including symlink targets, so final
                # image validation permits only these exact replacements.
                verified_overlays[path] = {'owners': sorted(owners), 'file': entry}
        apply_layer(runtime_layer, files)
    installed = dependencies.installed_packages(status, origin='installed filesystem')
    check = dependencies.validate(installed, architecture=architecture)
    controls = {name: dependencies.control_fields(value) for name, value in sorted(installed.items())}
    check.update(packages=controls, base_package_inventory_sha256=hashlib.sha256(
        json.dumps(controls, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    runtime_provenance = selection_provenance(selection, controls, architecture) if selection else {}
    inherited_hash = None
    if inherited_metadata is not None:
        # Debug APT adds tools to an already configured runtime filesystem.
        # Bind the previous source proof to its exact bytes in this filesystem,
        # then recheck every actual ELF and the new complete dpkg status below.
        inherited_hash = sha(inherited_metadata)
        require(inherited_metadata_path and files.get(inherited_metadata_path, {}).get('sha256') == inherited_hash,
                'inherited source metadata differs from the configured filesystem')
        previous = json.loads(inherited_metadata.read_text())
        require(previous.get('architecture') == architecture and
                {item['package'] for item in previous['source_packages']} == set(payloads),
                'inherited source metadata names different packages or architecture')
    provenance = []
    for name, archive in sorted(payloads.items()):
        item = specification['packages'][name]
        require('control_fields' not in item, 'runtime-layer declarations must not invent Debian controls')
        payload = {}
        apply_layer(archive, payload)
        native = []
        runtime_files = []
        runtime_links = []
        for path, entry in payload.items():
            if entry.get('kind') == 'symlink':
                target = path_location(path, files)
                actual = files.get(target, {})
                included = True
                if path_filters:
                    for line in path_filters.read_text().splitlines():
                        fields = line.split()
                        if len(fields) == 2 and fields[0] in ('path-include', 'path-exclude') and fnmatch.fnmatchcase('/' + path, fields[1]):
                            included = fields[0] == 'path-include'
                if not included and not actual:
                    continue
                require(actual.get('kind') == 'symlink' and actual.get('linkname') == entry['linkname'],
                        'image changed a runtime-layer symlink: ' + path)
                runtime_links.append({'path': target, **{key: actual[key]
                    for key in ('kind', 'linkname', 'mode', 'uid', 'gid')}})
                continue
            if entry.get('kind') != 'file':
                continue
            target = resolve_path(path, files)
            actual = files.get(target, {})
            included = True
            if path_filters:
                for line in path_filters.read_text().splitlines():
                    fields = line.split()
                    if len(fields) == 2 and fields[0] in ('path-include', 'path-exclude') and fnmatch.fnmatchcase('/' + path, fields[1]):
                        included = fields[0] == 'path-include'
            if not included and 'elf_machine' not in entry and not actual:
                continue
            require(actual.get('sha256') == entry['sha256'], 'image changed a runtime-layer file: ' + path)
            record = {'path': target, **{key: actual[key] for key in ('sha256', 'mode', 'uid', 'gid')}}
            runtime_files.append(record)
            if 'elf_machine' in entry:
                native.append(record)
        triplet = {'amd64': 'x86_64-linux-gnu', 'arm64': 'aarch64-linux-gnu'}[architecture]
        require(all(resolve_path(path.format(multiarch=triplet), files) in files for path in item['required_paths']),
                'image lacks a required runtime-layer path: ' + name)
        excluded_baseline_paths = []
        for path in item.get('excluded_baseline_paths', []):
            path = path.format(multiarch=triplet)
            require(path and not path.startswith('/') and '..' not in PurePosixPath(path).parts and
                    str(PurePosixPath(path)) == path, 'invalid excluded baseline path: ' + path)
            target = path_location(path, files)
            require(target not in files, 'image retains an excluded baseline file: ' + path)
            excluded_baseline_paths.append(target)
        if inherited_metadata is not None:
            prior = next(entry for entry in previous['source_packages'] if entry['package'] == name)
            require(prior.get('payload_sha256') == sha(archive), 'inherited runtime-layer source changed: ' + name)
        provenance.append({'package': name, 'packaging': 'runtime-layer', 'target': item['target'],
            'source_revision': item['source_revision'], 'payload_sha256': sha(archive),
            'source_version': item.get('source_version'),
            'debian_baseline': {owner: {'version': installed[owner].version,
                'architecture': installed[owner].architecture}
                for owner in [name] if owner in installed},
            'runtime_files': sorted(runtime_files, key=lambda entry: entry['path']),
            'runtime_links': sorted(runtime_links, key=lambda entry: entry['path']),
            'excluded_baseline_paths': sorted(excluded_baseline_paths),
            'native_files': sorted(native, key=lambda entry: entry['path'])})
    overlay_proof = {}
    if verified_overlays:
        sources = {owner for record in verified_overlays.values() for owner in record['owners']}
        overlay_proof['verified_overlays'] = {
            'runtime_layer_sha256': sha(runtime_layer), 'paths': verified_overlays,
            'payload_sha256': {name: sha(payloads[name]) for name in sorted(sources)}}
    elif inherited_metadata is not None and previous.get('verified_overlays'):
        proof = previous['verified_overlays']
        require(all(files.get(path) == entry['file'] for path, entry in proof['paths'].items()),
                'debug installation changed a same-owner source overlay')
        overlay_proof['verified_overlays'] = proof
    return {'_generated': 'AUTO-GENERATED by installed_source_metadata.py. DO NOT EDIT MANUALLY.',
        'schema': 1, 'architecture': architecture, 'source_packages': provenance,
        'installation_sha256': sha(installation), 'rootfs_sha256': sha(rootfs), 'dependency_check': check,
        'inherited_metadata_sha256': inherited_hash,
        'runtime_layer_sha256': sha(runtime_layer) if runtime_layer else None,
        **runtime_provenance,
        **overlay_proof,
        'info_files': {path: {key: entry[key] for key in ('sha256', 'mode')}
                       for path, entry in files.items()
                       if path.startswith('var/lib/dpkg/') and entry.get('kind') == 'file'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('rootfs', 'installation', 'spec', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--inherited-metadata', type=Path)
    parser.add_argument('--inherited-metadata-path')
    parser.add_argument('--selection', type=Path)
    parser.add_argument('--architecture', required=True)
    parser.add_argument('--payload', action='append', default=[])
    parser.add_argument('--runtime-layer', type=Path)
    parser.add_argument('--path-filters', type=Path)
    parser.add_argument('--allow-same-owner-overlays', action='store_true')
    parser.add_argument('--apt-preferences', type=Path)
    args = parser.parse_args()
    payloads = dict(value.split('=', 1) for value in args.payload)
    require(len(payloads) == len(args.payload), 'duplicate source runtime layer')
    result = describe(args.rootfs, args.installation, args.spec, args.architecture,
                      args.inherited_metadata, args.inherited_metadata_path, args.selection, payloads, args.runtime_layer,
                      args.path_filters, args.allow_same_owner_overlays)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    if args.apt_preferences:
        args.apt_preferences.write_text(apt_preferences(source_specification(args.spec)))


if __name__ == '__main__':
    main()
