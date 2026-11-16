#!/usr/bin/env python3
"""Select public P4RT outputs without publishing private SDK/runtime paths."""

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci.public_artifacts import bep_summary, dependency_json, safe_json


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    public = directory / 'public'
    # Never upload leftovers from an earlier attempt as current evidence.
    public.mkdir(exist_ok=False)

    def write(name, value):
        safe_json(value)
        (public / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')

    receipt_path = directory / 'build-receipt.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        write('public-receipt.json', {key: receipt[key] for key in
              ('status', 'architecture', 'revision', 'owner_revision', 'owner_handoff', 'artifacts') if key in receipt})
        for name, record in receipt.get('artifacts', {}).items():
            source = directory / name
            check.require(Path(name).name == name and not source.is_symlink(), 'Unexpected public artifact path')
            check.require(check.sha(source) == record['sha256'], 'Public artifact changed: ' + name)
            if source.suffix == '.json':
                safe_json(json.loads(source.read_text()))
            shutil.copyfile(source, public / name)
    for name in ('validation.json', 'action-audit.json', 'incremental.json'):
        source = directory / name
        if source.is_file():
            write(name, json.loads(source.read_text()))
    for source in directory.glob('*.validation.json'):
        write(source.name, json.loads(source.read_text()))
    for name, kind in (('MODULE.bazel.lock', 'lock'), ('module-graph.json', 'graph')):
        source = directory / name
        if source.is_file():
            dependency_json(json.loads(source.read_text()), kind)
            shutil.copyfile(source, public / name)
    owner = directory / 'owner-output'
    source_receipt = owner / 'source.json'
    if source_receipt.exists():
        source = json.loads(source_receipt.read_text())
        write('owner-source.json', {key: source[key] for key in
              ('commit', 'bazel', 'architecture', 'test_targets', 'sdk_inventory', 'tar_sha256', 'contract_sha256')})
        for name, expected in source['contract_sha256'].items():
            check.require(Path(name).name == name and check.sha(owner / name) == expected,
                          'Owner contract changed before publication')
            write(name, json.loads((owner / name).read_text()))
    for name in ('action-audit.json', 'test-action-audit.json', 'test-results.json', 'installed-packages.json'):
        source = owner / name
        if source.is_file():
            write('owner-' + name, json.loads(source.read_text()))
    tests_path = directory / 'owner-tests.json'
    if tests_path.exists():
        source = json.loads(tests_path.read_text())
        write('owner-tests.json', {key: source[key] for key in ('status', 'owner_revision')})
    summary = {str(path.relative_to(directory)): bep_summary(path.read_text().splitlines(), partial=True)
               for path in directory.rglob('*events.json*') if not path.is_relative_to(public)}
    write('build-summary.json', summary)
    return public


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.directory.resolve()))
