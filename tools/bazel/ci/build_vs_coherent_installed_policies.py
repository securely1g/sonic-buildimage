#!/usr/bin/env python3
"""Collect two existing installed-policy targets in one reviewed source graph.

The existing native cache setup, Make owners, action audit, exporter, image
validators and policy merger remain authoritative. This caller neither changes
crypto sealing nor confers native/full-VS acceptance on a new archive.
"""
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import shlex
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'cohort_setup', Path(__file__).with_name('build_vs_swss_installed_policy.py'))
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)
require = setup.require

BASE = '//dockers/docker-config-engine-trixie:docker-config-engine-trixie'
FAMILIES = {
    'swss': ('//dockers/docker-orchagent:', ('docker-orchagent', 'docker-orchagent-dbg')),
    'syncd': ('//platform/vs/docker-syncd-vs:', ('docker-syncd-vs', 'docker-syncd-vs-dbg')),
}
POLICIES = {name + '-installed-images-policy.json':
            '//tools/bazel/tests:' + name + '_installed_images_policy' for name in FAMILIES}
OPTIONS = ['--jobs=2', '--local_resources=cpu=2', '--local_resources=memory=8000', '--config=syncd-dash']
PRIMARY = {'docker-base-trixie.gz': '//dockers/docker-base-trixie:docker-base-trixie.gz',
           'docker-config-engine-trixie.gz': BASE + '.gz', **POLICIES,
           **{name + '.gz': owner + name + '.gz'
              for owner, images in FAMILIES.values() for name in images}}


def load_plan(path):
    value = json.loads(path.read_bytes())
    require(value['schema'] == 1 and value['architecture'] == 'amd64' and
            value['scope'] == 'coherent-swss-syncd' and value['policy_targets'] == POLICIES,
            'Different coherent policy scope')
    require(set(value['expected_native_archives']) == set(FAMILIES['syncd'][1]),
            'Different accepted native archive set')
    require(all(value['exports'].get(name) == target for name, target in PRIMARY.items()),
            'Different primary target')
    return value


def environment_check(plan, scratch, source):
    expected = {
        'BAZEL_REAL': str(scratch / 'native-bazel'),
        'SONIC_BAZEL_REGISTRY_REVISION': plan['catalog_revision'],
        'USE_BAZEL_VERSION': '8.5.1', 'BAZELISK_HOME': str(scratch / 'bazelisk'),
        'SOURCE_INPUTS_RUN': str(plan['source_inputs']['run_id']),
        'TMPDIR': str(scratch / 'temporary'),
    }
    require(all(os.environ.get(key) == value for key, value in expected.items()),
            'Build environment differs from the prepared remote-only launcher')
    # Reuse the current source owner: it filters user rc files before Bazel runs.
    # The sealed native launcher explicitly disables only these two local caches.
    home_rc = Path.home() / '.bazelrc'
    if not home_rc.exists() and not home_rc.is_symlink():
        return
    setup.source_check(source, plan['source_revision'])
    owner_path = source / 'tools/bazel/ci/review_bazel_rc.py'
    spec = importlib.util.spec_from_file_location('_cohort_review_bazel_rc', owner_path)
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    isolated_home = scratch / 'home'
    require(not (isolated_home / '.bazelrc').exists() and
            not (isolated_home / '.bazelrc').is_symlink(), 'Private comparison rc must be absent')

    def effective(configuration):
        result = []
        for line in configuration.splitlines():
            tokens = shlex.split(line, comments=True)
            if (len(tokens) == 2 and tokens[0] in ('common', 'build', 'test') and
                    tokens[1].split('=', 1)[0] in ('--disk_cache', '--repository_cache')):
                continue
            result.append(line)
        return result

    require(effective(owner.configuration(source, Path.home())) ==
            effective(owner.configuration(source, isolated_home)),
            'Home Bazel rc changes effective source inputs or cache options')


def declarations(plan, owners):
    exports, layouts = dict(PRIMARY), {BASE}
    for family, owner in owners.items():
        prefix, images = FAMILIES[family]
        require(owner.OWNER == prefix and tuple(owner.IMAGES) == images and owner.BASE == BASE,
                'Different image owner scope')
        for name, target in owner.TARGETS.items():
            require(Path(name).name == name and name not in ('', '.', '..'), 'Different export name')
            if name in PRIMARY:
                require(PRIMARY[name] == target, 'Different primary image target')
            else:
                exports[family + '--' + name] = target
        layouts.update(prefix + name for name in images)
    require(owners['syncd'].OPTIONS == OPTIONS, 'Different Syncd options')
    require(exports == plan['exports'], 'Different reviewed target closure')
    return exports, sorted(layouts)


def manifest_snapshot(source):
    result = {}
    for image in ('docker-base-trixie', 'docker-config-engine-trixie'):
        directory = source / 'target/bazel-manifests' / image
        require(directory.is_dir() and not directory.is_symlink(), 'Missing shared Make manifest')
        entries = list(directory.iterdir())
        require(1 <= len(entries) <= 2 and any(p.name == 'manifest.json' for p in entries),
                'Different shared manifest files')
        for path in entries:
            require(path.name in ('manifest.json', 'image.env') and path.is_file() and
                    not path.is_symlink() and 0 < path.stat().st_size <= 1024**2,
                    'Different shared manifest member')
            raw = path.read_bytes()
            result[str(path.relative_to(source))] = {
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    return result


def bind_policies(paths, receipt, plan, check, merge):
    actual = {}
    for name in PRIMARY:
        if name.endswith('.gz'):
            path = paths[name]
            row = receipt['artifacts'][name]
            require(path.is_file() and not path.is_symlink() and
                    path.stat().st_size == row['bytes'] and check.sha(path) == row['sha256'],
                    'Exported archive identity differs')
            actual[name[:-3]] = {'bytes': row['bytes'], 'sha256': row['sha256']}
    mismatches = [name for name, expected in plan['expected_native_archives'].items()
                  if actual[name] != expected]
    require(not mismatches, 'Accepted native archive differs: ' + json.dumps({
        name: {
            'target': PRIMARY[name + '.gz'],
            'expected': {key: plan['expected_native_archives'][name][key]
                         for key in ('bytes', 'sha256')},
            'observed': actual[name],
        } for name in mismatches}, sort_keys=True))
    policies, identities = [], {}
    for family, (_, images) in FAMILIES.items():
        name = family + '-installed-images-policy.json'
        expected = {image: actual[image] for image in ('docker-config-engine-trixie', *images)}
        identities[name] = setup.validate_policy(paths[name], {'expected_archives': expected})
        policies.append(json.loads(paths[name].read_bytes()))
    # Existing schema/complete shared-image equality, without a replacement merger.
    merge(policies)
    return actual, identities


def build_cohort(plan, source, scratch, output, owners):
    orch, syncd, audit, builder, logs, exporter, resolution, merge, public_json = owners
    environment_check(plan, scratch, source)
    images = {'swss': orch, 'syncd': syncd}
    exports, layouts = declarations(plan, images)
    private = scratch / 'cohort-build'
    private.mkdir(mode=0o700)
    receipt = {'schema': 1, 'status': 'preparing', 'commands': [],
               'source_revision': plan['source_revision'], 'catalog_revision': plan['catalog_revision'],
               'policy_targets': POLICIES, 'options': OPTIONS}
    try:
        require(orch.identity(private) == plan['source_revision'], 'Orchagent source revision differs')
        syncd.clean_source(private)
        identity = syncd.identity()
        require(identity['revision'] == plan['source_revision'], 'Syncd source revision differs')
        logs.execute(setup.MAKE_ARGUMENTS, private, receipt, 'swss.manifests', cwd=source)
        manifests = manifest_snapshot(source)
        logs.execute(syncd.manifest_command(), private, receipt, 'syncd.manifests', cwd=source)
        require(manifest_snapshot(source) == manifests, 'Shared Make manifests changed between owners')
        receipt['shared_manifests'] = manifests
        receipt['status'] = 'auditing'
        bazel = str(scratch / 'review-bazel')
        receipt['action_audit'] = audit.audit_targets(
            bazel, OPTIONS, [*exports.values(), *layouts],
            workspace=source, output=private / 'action-audit.json')
        receipt['status'] = 'building'
        paths = builder.collect_archives(source, private, receipt, exports, bazel=bazel,
                                         options=[*OPTIONS, '--keep_going'], additional_targets=layouts)
        require(set(paths) == set(exports), 'Incomplete cohort exports')
        receipt['status'] = 'binding'
        actual, policy_ids = bind_policies(paths, receipt, plan, orch.check, merge)
        layout_paths = {}
        for target in layouts:
            values = logs.execute([bazel, 'cquery', *OPTIONS, '--output=files', target],
                                  private, receipt, 'cohort.layout', cwd=source).splitlines()
            require(len(values) == 1, 'Different layout output count')
            layout_paths[target] = values[0]
        resolved = resolution.collect(source, private, bazel=[bazel])
        receipt['status'] = 'validating'
        validations = {}
        for family, owner in images.items():
            directory = private / family
            directory.mkdir(mode=0o700)
            records = {}
            for name in owner.TARGETS:
                exported = name if name in PRIMARY else family + '--' + name
                exporter(paths[exported], directory / name)
                records[name] = receipt['artifacts'][exported]
            # This is a view of this successful collection, not an old producer receipt.
            family_receipt = {**identity, 'status': 'built', 'architecture': 'amd64',
                              'commands': receipt['commands'], 'artifacts': records,
                              'action_audit': receipt['action_audit'], 'resolution': resolved,
                              'review_registry_revision': plan['catalog_revision'],
                              'invocation': {'bazel': bazel, 'review_registry_revision': plan['catalog_revision']},
                              'base_layout': layout_paths[BASE],
                              'layouts': {name: layout_paths[owner.OWNER + name] for name in owner.IMAGES},
                              'cohort_collection': True}
            (directory / 'build-receipt.json').write_text(json.dumps(family_receipt, indent=2) + '\n')
            owner.validate_images(directory)
            validation = directory / 'validation.json'
            value = json.loads(validation.read_bytes())
            require(value.get('status') == 'passed' and value.get('revision') == plan['source_revision'],
                    'Source image validation did not pass')
            public_json(value)
            # Reuse the existing workflow's public-receipt projection. Private
            # command lines and local layout paths remain in private scratch.
            public_receipt = {key: family_receipt[key] for key in (
                'status', 'revision', 'architecture', 'artifacts',
                'review_registry_revision', 'tested_revision_parents') if key in family_receipt}
            public_json(public_receipt)
            (directory / 'public-receipt.json').write_text(json.dumps(public_receipt, indent=2) + '\n')
            validations[family] = {'bytes': validation.stat().st_size, 'sha256': orch.check.sha(validation)}
        require(orch.identity(private) == plan['source_revision'], 'Orchagent source changed')
        syncd.clean_source(private)
        require(syncd.identity() == identity, 'Syncd source identity changed')
        require(manifest_snapshot(source) == manifests, 'Shared Make manifests changed after build')
        output.mkdir(mode=0o700)
        archives, reports = output / 'archives', output / 'reports'
        archives.mkdir(mode=0o700)
        reports.mkdir(mode=0o700)
        for name in PRIMARY:
            copied = (archives if name.endswith('.gz') else reports) / name
            exporter(paths[name], copied)
            expected = receipt['artifacts'][name]
            require(copied.is_file() and not copied.is_symlink() and
                    copied.stat().st_size == expected['bytes'] and orch.check.sha(copied) == expected['sha256'],
                    'Published cohort copy differs')
        for family in images:
            for name in ('validation.json', 'public-receipt.json'):
                exporter(private / family / name, reports / (family + '-' + name))
        public_json(json.loads((private / 'action-audit.json').read_bytes()))
        exporter(private / 'action-audit.json', reports / 'action-audit.json')
        result = {'schema': 1, 'status': 'COHERENT_POLICIES_AND_SOURCE_IMAGES_VALIDATED',
                  'source_revision': plan['source_revision'], 'source_head': plan['source_head'],
                  'source_tree': plan['source_tree'], 'catalog_revision': plan['catalog_revision'],
                  'native_cache_controller': plan['native_revision'], 'archives': actual,
                  'policies': policy_ids, 'source_validations': validations, 'shared_manifests': manifests,
                  'accepted_native_archive_equality': True, 'native_acceptance': False, 'VS_acceptance': False,
                  'operator': {key: os.environ.get(key) for key in
                               ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA',
                                'GITHUB_JOB', 'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME')}}
        public_json(result)
        (reports / 'cohort.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
        receipt['status'] = 'passed'
        return result
    finally:
        (private / 'private-build-receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('cache', 'build'))
    for name in ('plan', 'source', 'native', 'scratch', 'output', 'archives', 'mirrors'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    plan = load_plan(args.plan)
    source, native, scratch = (p.resolve(strict=True) for p in (args.source, args.native, args.scratch))
    require(len({source, native, scratch}) == 3 and not any(a.is_relative_to(b)
            for a in (source, native, scratch) for b in (source, native, scratch) if a != b),
            'Source and private scratch must be separate')
    environment_check(plan, scratch, source)
    if args.phase == 'cache':
        setup.prepare_cache(plan, source, native, scratch,
                            args.archives.resolve(strict=True), args.mirrors.resolve(strict=True))
    else:
        setup.source_check(source, plan['source_revision'])
        orch = setup.import_from(source, 'tools.bazel.ci.orchagent_image')
        modules = [importlib.import_module('tools.bazel.ci.' + name) for name in
                   ('syncd_image', 'bazel_commands', 'build', 'command_log', 'resolution', 'validate_runtime')]
        syncd, audit, builder, logs, resolution, runtime = modules
        exporter = importlib.import_module('tools.bazel.build_helpers').export_archive
        public_json = importlib.import_module('tools.bazel.ci.public_artifacts').safe_json
        build_cohort(plan, source, scratch, args.output,
                     (orch, syncd, audit, builder, logs, exporter, resolution, runtime.merge_images_policies, public_json))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
