#!/usr/bin/env python3
"""Select public P4RT outputs without publishing private SDK/runtime paths."""

import argparse
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci.public_artifacts import bep_summary, dependency_json, label, safe_json
from tools.bazel.ci.python_packages import test_result


def owner_test_summary(directory):
    """Summarize the owner's retained XML and BEP statuses without copying logs."""
    owner = directory / 'owner-output'
    source = json.loads((owner / 'source.json').read_text())
    receipt = json.loads((directory / 'owner-tests.json').read_text())
    check.require(receipt['status'] == 'passed' and receipt['owner_revision'] == source['commit'],
                  'Owner test receipt differs from the built source')
    targets = source['test_targets']
    check.require(isinstance(targets, list) and targets and len(set(targets)) == len(targets),
                  'Expected a unique nonempty owner test selection')
    check.require((owner / 'test-targets.txt').read_text().splitlines() == targets,
                  'Owner test selection changed after build')
    statuses = json.loads((owner / 'test-results.json').read_text())
    check.require(set(statuses) == set(targets) and all(x == 'PASSED' for x in statuses.values()),
                  'Every selected owner test needs a passing BEP status')
    results, total_bytes = {}, 0
    for target in targets:
        label(target)
        check.require(target.startswith(('//p4rt_app/', '//p4rt_app:')) and target.count(':') == 1,
                      'Unexpected owner test target')
        relative = Path(target.removeprefix('//').replace(':', '/')) / 'test.xml'
        path = owner / 'test-results' / relative
        check.require(path.is_file() and not path.is_symlink() and
                      path.resolve().is_relative_to((owner / 'test-results').resolve()),
                      'Owner test XML escaped its retained directory')
        size = path.stat().st_size
        total_bytes += size
        check.require(0 < size <= 2 * 1024**2 and total_bytes <= 16 * 1024**2,
                      'Owner test XML exceeds the summary size limit')
        counts = test_result(path)
        check.require(counts['skipped'] == 0 and not ElementTree.parse(path).getroot().findall('.//skipped'),
                      'Owner tests must execute without skipped cases')
        results[target] = {'status': statuses[target], 'xml_sha256': check.sha(path), **counts}
    return {'status': 'passed', 'owner_revision': source['commit'],
            'target_count': len(results), 'testcase_count': sum(x['tests'] for x in results.values()),
            'tests': results}


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
    for name in ('validation.json', 'loaded-validation.json', 'action-audit.json', 'incremental.json', 'worker.json'):
        source = directory / name
        if source.is_file():
            write(name, json.loads(source.read_text()))
    for source in directory.glob('*.validation.json'):
        write(source.name, json.loads(source.read_text()))
    for pattern in ('worker-*.json', '*.runtime.json'):
        for source in directory.glob(pattern):
            if source.name != 'worker-private.json':
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
        if source['status'] == 'passed':
            write('owner-test-summary.json', owner_test_summary(directory))
    summary = {str(path.relative_to(directory)): bep_summary(path.read_text().splitlines(), partial=True)
               for path in directory.rglob('*events.json*') if not path.is_relative_to(public)}
    write('build-summary.json', summary)
    return public


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.directory.resolve()))
