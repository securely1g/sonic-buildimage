"""Publish and verify only the VS producer inputs needed by P4RT's separate source tests."""

import argparse
import json
from pathlib import Path

from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import p4rt_handoff
from tools.bazel.ci.p4rt_image import PREPARATION_TARGETS, write
from tools.bazel.ci.public_artifacts import safe_json
from tools.bazel.ci.vs_validation_bundles import copy_output

PREPARATION_FIELDS = ('schema', 'status', 'architecture', 'revision', 'owner_revision',
                      'artifacts', 'action_audit', 'owner_handoff', 'parent_common')
BUNDLE_FIXTURES = ('config-engine.source.tar', 'handoff.json')


def read(path):
    check.require(path.is_file() and not path.is_symlink(), 'Missing P4RT input: ' + str(path))
    return json.loads(path.read_bytes())


def read_inputs(directory, revision, owner_revision):
    receipt = read(directory / 'vs-preparation.json')
    bundle = read(directory / 'vs-bundle.json')
    preparation = directory / 'owner-preparation'
    owner = directory / 'owner-output'
    check.require(receipt['schema'] == 1 and receipt['status'] == 'prepared-for-make' and
                  receipt['architecture'] == bundle['architecture'] == 'amd64' and
                  receipt['revision'] == bundle['revision'] == revision and
                  receipt['owner_revision'] == owner_revision and bundle['image'] == 'docker-sonic-p4rt',
                  'P4RT inputs belong to a different VS build or owner')
    source = read(owner / 'source.json')
    check.require(source['commit'] == owner_revision and source['bazel'] == '6.4.0' and
                  source['architecture'] == 'amd64', 'Unexpected VS owner source record')
    artifacts = receipt['artifacts']
    check.require(set(artifacts) == set(PREPARATION_TARGETS), 'Incomplete VS owner preparation inputs')
    for name, target in PREPARATION_TARGETS.items():
        item, path = artifacts[name], preparation / name
        check.require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0 and
                      item == {'target': target, 'bytes': path.stat().st_size, 'sha256': check.sha(path)},
                      'Changed VS owner preparation input: ' + name)
    audit = read(preparation / 'action-audit.json')
    check.require(audit == receipt['action_audit'] and set(audit['targets']) == set(PREPARATION_TARGETS.values()) and
                  audit['deb_outputs'] == [] and audit['packaging_wrappers'] == [],
                  'VS owner preparation action audit differs')
    handoff = owner / 'handoff.json'
    check.require(read(handoff) == receipt['owner_handoff'] and
                  bundle['fixtures']['handoff.json'] == check.sha(handoff) and
                  bundle['fixtures']['config-engine.source.tar'] == artifacts['config-engine.source.tar']['sha256'],
                  'Owner handoff or parent differs from the selected VS image')
    for name in p4rt_handoff.FILES:
        path = owner / name
        check.require(path.is_file() and not path.is_symlink() and
                      receipt['owner_handoff']['files'][name] ==
                      {'bytes': path.stat().st_size, 'sha256': check.sha(path)},
                      'Changed VS owner output: ' + name)
    return receipt


def copy_inputs(source, target):
    # Both legs copy only the declared files and source build metadata.
    # Options contain private producer paths. Prior test results never travel.
    for subdir, names in (('owner-preparation', (*PREPARATION_TARGETS, 'action-audit.json')),
                          ('owner-output', (*p4rt_handoff.FILES, 'action-audit.json'))):
        dest = target / subdir
        dest.mkdir(parents=True, exist_ok=False)
        for name in names:
            path = source / subdir / name
            check.require(path.is_file() and not path.is_symlink(), 'Missing VS producer input: ' + name)
            if name.endswith('.json'):
                safe_json(read(path))
            copy_output(path, dest / name)
    owner = read(source / 'owner-output/source.json')
    record = {key: owner[key] for key in p4rt_handoff.SOURCE_FIELDS}
    safe_json(record)
    write(target / 'owner-output/source.json', record)


def export_inputs(source, bundle_dir, output):
    """Materialize the allowlist after the selected Make build, including cache hits."""
    prepared = read(source / 'owner-preparation.json')
    receipt = {key: prepared[key] for key in PREPARATION_FIELDS}
    bundle = read(bundle_dir / 'bundle.json')
    image = {key: bundle[key] for key in ('image', 'revision', 'architecture')}
    image['fixtures'] = {key: bundle['fixtures'][key] for key in BUNDLE_FIXTURES}
    safe_json(receipt)
    safe_json(image)
    output.mkdir(parents=True, exist_ok=False)
    copy_inputs(source, output)
    copy_output(bundle_dir / 'handoff.json', output / 'owner-output/handoff.json')
    write(output / 'vs-preparation.json', receipt)
    write(output / 'vs-bundle.json', image)
    read_inputs(output, image['revision'], receipt['owner_revision'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    export_inputs(options.source, options.bundle, options.output)
