"""Bind source revisions and public runtime TAR targets without Debian packaging.

The image keeps genuine Debian baseline records separately from source-layer
provenance. This tool writes only validation JSON.
"""
import argparse
import copy
import json
from pathlib import Path

from tools.bazel.ci.artifact_validation import require


def describe(spec, targets):
    """Bind source-owned TAR declarations without creating Debian identities."""
    document = copy.deepcopy(spec)
    names = {item['package'] for item in document['packages']}
    require(set(targets) == names and len(names) == len(document['packages']),
            'source package inputs differ from the declaration')
    for item in document['packages']:
        require(item.get('kind') == 'runtime-layer', 'source components must supply runtime TARs')
        require(not {'version', 'architecture', 'control_fields', 'deb', 'deb_target'}.intersection(item),
                'Maintained source declarations must not copy or override Debian metadata')
        name = item['package']
        require(targets[name] == item['source']['target'],
                'Runtime layer target differs from source declaration')
        item.update(version=item['source']['version'], architecture=document['architecture'])
    document['_generated'] = 'AUTO-GENERATED from source runtime TAR declarations. DO NOT EDIT MANUALLY.'
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--package-target', action='append', default=[])
    args = parser.parse_args()
    targets = dict(value.split('=', 1) for value in args.package_target)
    require(len(targets) == len(args.package_target),
            'Duplicate source package input')
    result = describe(json.loads(args.spec.read_text()), targets)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
