"""Bind source provenance to binary layers and genuine Debian replacements.

The maintained declaration names source revisions and required files. Only
Debian replacements obtain package controls and payload hashes from a DEB.
Upstream BSD tar reads archive members; this tool writes only validation JSON.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

from sonic_apt import dependencies
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci.package_contract import CONTROL_FIELDS


def describe(spec, packages, targets, tar_tool):
    document = copy.deepcopy(spec)
    names = {item['package'] for item in document['packages']}
    deb_names = {item['package'] for item in document['packages'] if item.get('kind') != 'runtime-layer'}
    require(set(packages) == deb_names and set(targets) == names,
            'source package inputs differ from the declaration')
    with tempfile.TemporaryDirectory(prefix='source-package-controls-') as temporary:
        for item in document['packages']:
            forbidden = {'version', 'architecture', 'control_fields', 'deb'}
            if item.get('kind') != 'runtime-layer':
                forbidden.update(('path_modes', 'path_owners'))
            require(not forbidden.intersection(item),
                    'Maintained source declarations must not copy or override Debian metadata')
            name = item['package']
            if item.get('kind') == 'runtime-layer':
                require('deb_target' not in item and targets[name] == item['source']['target'],
                        'Runtime layer target differs from source declaration')
                item.update(version=item['source']['version'], architecture=document['architecture'])
                continue
            require(targets[name] == item['deb_target'] == item['source']['target'],
                    'Actual DEB target differs from source package declaration')
            archive = Path(packages[name])
            before = sha(archive)
            members = subprocess.check_output([tar_tool, '-tf', str(archive)], text=True).splitlines()
            require(len(members) == 3 and members[0] == 'debian-binary' and
                    members[1].startswith('control.tar') and members[2].startswith('data.tar'),
                    'DEB must contain one control archive and one data archive')
            require(subprocess.check_output([tar_tool, '-xOf', str(archive), 'debian-binary']) == b'2.0\n',
                    'Unsupported Debian archive version')
            path = Path(temporary) / 'control.tar'
            with path.open('wb') as output:
                subprocess.run([tar_tool, '-xOf', str(archive), members[1]], stdout=output, check=True)
            with tarfile.open(path) as stream:
                control = [entry for entry in stream if entry.name.removeprefix('./') == 'control']
                require(len(control) == 1 and control[0].isfile(),
                        'DEB must contain exactly one package control')
                package = dependencies.package_from_control(stream.extractfile(control[0]).read(), origin=str(archive))
            require(package.name == name and package.architecture == document['architecture'],
                    'Actual DEB differs from source package declaration')
            data = Path(temporary) / 'data.archive'
            with data.open('wb') as output:
                subprocess.run([tar_tool, '-xOf', str(archive), members[2]], stdout=output, check=True)
            require(sha(archive) == before, 'Source DEB changed while reading its contract')
            fields = dependencies.control_fields(package)
            item.update(version=package.version, architecture=package.architecture,
                        control_fields={field: fields.get(field, '') for field in CONTROL_FIELDS},
                        deb={'target': targets[name], 'sha256': before, 'data_sha256': sha(data)})
    document['_generated'] = 'AUTO-GENERATED from source declarations and actual Debian controls. DO NOT EDIT MANUALLY.'
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tar', required=True)
    parser.add_argument('--package', action='append', default=[])
    parser.add_argument('--package-target', action='append', default=[])
    args = parser.parse_args()
    packages = dict(value.split('=', 1) for value in args.package)
    targets = dict(value.split('=', 1) for value in args.package_target)
    require(len(packages) == len(args.package) and len(targets) == len(args.package_target),
            'Duplicate source package input')
    result = describe(json.loads(args.spec.read_text()), packages, targets, args.tar)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
