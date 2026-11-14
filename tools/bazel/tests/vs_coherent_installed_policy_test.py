#!/usr/bin/env python3
"""Exercise the fixed cohort's ordering, existing-owner use and identity gates."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('coherent_caller', ROOT / 'tools/bazel/ci/build_vs_coherent_installed_policies.py')
caller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caller)
PLAN = ROOT / 'tools/bazel/ci/vs_coherent_installed_policy_inputs.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CohortTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        self.home = self.root / 'runner-home'
        self.home.mkdir()
        self.output = self.root / 'outputs'
        self.plan = caller.load_plan(PLAN)
        # Keep the original core-scope fixtures exact while the checked plan
        # additionally declares the fixed component refresh.
        self.plan.pop('component_refresh', None)
        self.plan.pop('component_families', None)
        self.plan.pop('component_profile', None)
        self.plan['exports'] = {name: target for name, target in self.plan['exports'].items()
                                if name in caller.PRIMARY or name.startswith(('swss--', 'syncd--'))}
        self.events = []
        self.problem = None
        self.payload = {name: ('original:' + name).encode() for name in self.plan['exports']}
        for name in self.plan['expected_native_archives']:
            raw = self.payload[name + '.gz']
            self.plan['expected_native_archives'][name] = {
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        self.enterContext(mock.patch.object(Path, 'home', return_value=self.home))
        self.enterContext(mock.patch.dict(os.environ, {
            'BAZEL_REAL': str(self.scratch / 'native-bazel'),
            'SONIC_BAZEL_REGISTRY_REVISION': self.plan['catalog_revision'],
            'USE_BAZEL_VERSION': '8.5.1', 'BAZELISK_HOME': str(self.scratch / 'bazelisk'),
            'SOURCE_INPUTS_RUN': str(self.plan['source_inputs']['run_id']),
            'TMPDIR': str(self.scratch / 'temporary'),
        }))
        self.start_home = os.environ.get('HOME')
        self.owners = self.make_owners()

    def make_owners(self):
        def identity():
            return {'revision': self.plan['source_revision'], 'tested_revision_parents': ['head', 'base']}
        def validate(family, directory):
            self.events.append('validate:' + family)
            value = json.loads((directory / 'build-receipt.json').read_bytes())
            self.assertTrue(value['cohort_collection'])
            self.assertEqual(value['revision'], self.plan['source_revision'])
            self.assertEqual(value['commands'][0]['name'], 'swss.manifests')
            for name, record in value['artifacts'].items():
                self.assertEqual(sha(directory / name), record['sha256'])
            if self.problem == 'validation':
                raise ValueError('source validation failure')
            (directory / 'validation.json').write_text(json.dumps({
                'status': 'failed' if self.problem == 'validation-status' else 'passed',
                'revision': self.plan['source_revision']}))
        families = {}
        for family, (prefix, images) in caller.FAMILIES.items():
            targets = {name + '.gz': prefix + name + '.gz' for name in images}
            targets.update({name[len(family)+2:]: target for name, target in self.plan['exports'].items()
                            if name.startswith(family + '--')})
            families[family] = SimpleNamespace(
                OWNER=prefix, BASE=caller.BASE, IMAGES=images, TARGETS=targets,
                OPTIONS=list(caller.OPTIONS), check=SimpleNamespace(sha=sha),
                clean_source=mock.Mock(), validate_images=lambda directory, f=family: validate(f, directory))
        families['swss'].identity = mock.Mock(return_value=self.plan['source_revision'])
        families['syncd'].identity = mock.Mock(side_effect=identity)
        families['syncd'].manifest_command = mock.Mock(return_value=['reviewed-syncd-manifests'])

        def execute(argv, directory, receipt, name, **kwargs):
            if name.endswith('.manifests'):
                self.events.append(name)
                receipt['commands'].append({'name': name, 'argv': argv})
                for image in ('docker-base-trixie', 'docker-config-engine-trixie'):
                    target = self.source / 'target/bazel-manifests' / image
                    target.mkdir(parents=True, exist_ok=True)
                    content = 'different' if self.problem == 'manifest' and name == 'syncd.manifests' else '{}'
                    (target / 'manifest.json').write_text(content)
                return ''
            self.assertEqual(argv[1], 'cquery')
            return 'bazel-out/' + argv[-1].rsplit(':', 1)[-1] + '\n'

        def audit(bazel, options, targets, *, workspace, output):
            self.events.append('audit')
            self.assertEqual(options, caller.OPTIONS)
            self.assertEqual(set(targets), set(self.plan['exports'].values()) |
                             {caller.BASE, *[p+n for p, names in caller.FAMILIES.values() for n in names]})
            if self.problem == 'audit':
                raise ValueError('DEB action')
            result = {'targets': targets, 'deb_outputs': [], 'packaging_wrappers': []}
            output.write_text(json.dumps(result))
            return result

        def collect(source, directory, receipt, targets, **kwargs):
            self.events.append('build')
            self.assertEqual(targets, self.plan['exports'])
            self.assertEqual(kwargs['options'], [*caller.OPTIONS, '--keep_going'])
            self.assertEqual(len(kwargs['additional_targets']), 5)
            paths = {}
            receipt['artifacts'] = {}
            for name, target in targets.items():
                if name in caller.POLICIES:
                    family = name.split('-', 1)[0]
                    names = ('docker-config-engine-trixie', *caller.FAMILIES[family][1])
                    rows = [{'name': image, 'archive': image + '.gz', 'tag': image + ':latest',
                             'archive_sha256': hashlib.sha256(self.payload[image + '.gz']).hexdigest()}
                            for image in names]
                    if self.problem == 'policy':
                        rows[0]['archive_sha256'] = '0' * 64
                    raw = json.dumps({'architecture': 'amd64', 'images': rows, 'symbol_pairs': []}).encode()
                else:
                    raw = self.payload[name]
                    if self.problem == 'native' and name == 'docker-syncd-vs.gz':
                        raw += b' changed'
                path = directory / name
                path.write_bytes(raw)
                paths[name] = path
                receipt['artifacts'][name] = {'target': target, 'bytes': len(raw), 'sha256': sha(path)}
            if self.problem == 'missing':
                paths.pop('swss-installed-images-policy.json')
            return paths

        def merge(policies):
            self.events.append('merge')
            self.assertEqual(len(policies), 2)
            if self.problem == 'merge':
                raise ValueError('existing merger: shared image differs')
            return {'architecture': 'amd64'}

        def export(source, destination):
            shutil.copyfile(source, destination)
            if self.problem == 'copy' and destination.parent.name == 'archives':
                destination.write_bytes(b'corrupt')
        return (families['swss'], families['syncd'], SimpleNamespace(audit_targets=audit),
                SimpleNamespace(collect_archives=collect), SimpleNamespace(execute=execute), export,
                SimpleNamespace(collect=mock.Mock(return_value={'actual_module_graph': True})), merge, mock.Mock())

    def run_cohort(self):
        return caller.build_cohort(self.plan, self.source, self.scratch, self.output, self.owners)

    def test_one_graph_uses_both_existing_validators_and_preserves_native_hashes(self):
        result = self.run_cohort()
        self.assertEqual(self.events, ['swss.manifests', 'syncd.manifests', 'audit', 'build', 'merge',
                                       'validate:swss', 'validate:syncd'])
        self.assertEqual(len(list((self.output / 'archives').iterdir())), 6)
        self.assertEqual(result['source_revision'], self.plan['source_revision'])
        self.assertTrue(result['accepted_native_archive_equality'])
        self.assertFalse(result['native_acceptance'])
        self.assertFalse(result['VS_acceptance'])
        self.assertEqual(os.environ.get('HOME'), self.start_home)
        public = json.loads((self.output / 'reports/swss-public-receipt.json').read_bytes())
        self.assertNotIn('commands', public)
        self.assertNotIn('layouts', public)
        self.owners[-1].assert_called()

    def test_changed_shared_make_manifest_stops_before_audit(self):
        self.problem = 'manifest'
        with self.assertRaisesRegex(ValueError, 'Shared Make manifests changed'): self.run_cohort()
        self.assertEqual(self.events, ['swss.manifests', 'syncd.manifests'])

    def test_no_deb_audit_failure_stops_before_build(self):
        self.problem = 'audit'
        with self.assertRaisesRegex(ValueError, 'DEB action'): self.run_cohort()
        self.assertNotIn('build', self.events)

    def test_different_native_archive_stops_before_policy_merge_and_publication(self):
        self.problem = 'native'
        with self.assertRaisesRegex(ValueError, 'Accepted native archive differs'): self.run_cohort()
        self.assertNotIn('merge', self.events)
        self.assertFalse(self.output.exists())

    def test_multiple_native_mismatches_retain_only_public_expected_and_observed_tuples(self):
        self.problem = 'native'
        self.plan['expected_native_archives']['docker-syncd-vs-dbg']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Accepted native archive differs') as raised:
            self.run_cohort()
        prefix = 'Accepted native archive differs: '
        self.assertTrue(str(raised.exception).startswith(prefix))
        details = json.loads(str(raised.exception)[len(prefix):])
        expected = {}
        for name in ('docker-syncd-vs', 'docker-syncd-vs-dbg'):
            raw = self.payload[name + '.gz']
            if name == 'docker-syncd-vs':
                raw += b' changed'
            expected[name] = {
                'target': caller.PRIMARY[name + '.gz'],
                'expected': self.plan['expected_native_archives'][name],
                'observed': {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()},
            }
        self.assertEqual(details, expected)
        self.assertNotIn('merge', self.events)
        self.assertNotIn('validate:swss', self.events)
        self.assertNotIn('validate:syncd', self.events)
        self.assertFalse(self.output.exists())

    def test_policy_must_bind_actual_same_graph_archive(self):
        self.problem = 'policy'
        with self.assertRaisesRegex(ValueError, 'Policy does not match'): self.run_cohort()
        self.assertFalse(self.output.exists())

    def test_existing_shared_policy_merge_failure_stops_validation_and_publication(self):
        self.problem = 'merge'
        with self.assertRaisesRegex(ValueError, 'existing merger'): self.run_cohort()
        self.assertNotIn('validate:swss', self.events)
        self.assertFalse(self.output.exists())

    def test_existing_source_validator_failure_stops_publication(self):
        self.problem = 'validation'
        with self.assertRaisesRegex(ValueError, 'source validation failure'): self.run_cohort()
        self.assertFalse(self.output.exists())

    def test_nonpassing_validation_result_stops_publication(self):
        self.problem = 'validation-status'
        with self.assertRaisesRegex(ValueError, 'did not pass'): self.run_cohort()
        self.assertFalse(self.output.exists())

    def test_source_identity_change_stops_publication(self):
        self.owners[0].identity.side_effect = [self.plan['source_revision'], 'different']
        with self.assertRaisesRegex(ValueError, 'source changed'): self.run_cohort()
        self.assertFalse(self.output.exists())

    def test_missing_export_stops_before_merge(self):
        self.problem = 'missing'
        with self.assertRaisesRegex(ValueError, 'Incomplete cohort'): self.run_cohort()
        self.assertNotIn('merge', self.events)

    def test_published_copy_must_match_the_collected_bytes(self):
        self.problem = 'copy'
        with self.assertRaisesRegex(ValueError, 'Published cohort copy differs'): self.run_cohort()
        self.assertFalse((self.output / 'reports/cohort.json').exists())

    def test_arbitrary_sidecar_target_is_rejected_before_make(self):
        self.owners[0].TARGETS['other.tar'] = '//not-reviewed:target'
        with self.assertRaisesRegex(ValueError, 'target closure'): self.run_cohort()
        self.assertEqual(self.events, [])

    def test_changed_syncd_options_are_rejected_before_make(self):
        self.owners[1].OPTIONS = ['--config=other']
        with self.assertRaisesRegex(ValueError, 'Syncd options'): self.run_cohort()
        self.assertEqual(self.events, [])

    def install_review_rc_owner(self):
        original = Path(os.environ.get('COHORT_REVIEW_RC_OWNER',
                        ROOT / 'tools/bazel/ci/review_bazel_rc.py'))
        self.assertEqual(sha(original), '104c74f0845a4a2fdcb6d2bfff7a03978051c66732b2a871fb2191039af09593')
        owner = self.source / 'tools/bazel/ci/review_bazel_rc.py'
        owner.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, owner)
        (self.source / 'tools/bazel/common.bazelrc').write_text(
            'build --compilation_mode=opt\ntry-import %workspace%/.bazelrc.user\n')
        self.source_check = self.enterContext(mock.patch.object(caller.setup, 'source_check'))

    def test_inherited_distdir_is_rejected_without_changing_home(self):
        self.install_review_rc_owner()
        (self.home / '.bazelrc').write_text('build --distdir=/unreviewed\n')
        with self.assertRaisesRegex(ValueError, 'Home Bazel rc changes'): self.run_cohort()
        self.assertEqual(self.events, [])
        self.assertEqual(os.environ.get('HOME'), self.start_home)

    def test_overridden_caches_and_ignored_home_settings_preserve_validation(self):
        self.install_review_rc_owner()
        raw = ('build --disk_cache=/existing --repository_cache=/existing-repository\n'
               'build --registry=https://ignored.invalid --define=ignored\n'
               'try-import /ignored-home-import\n')
        (self.home / '.bazelrc').write_text(raw)
        result = self.run_cohort()
        self.assertEqual(self.events, ['swss.manifests', 'syncd.manifests', 'audit', 'build', 'merge',
                                       'validate:swss', 'validate:syncd'])
        self.assertTrue(result['accepted_native_archive_equality'])
        self.source_check.assert_called_once_with(self.source, self.plan['source_revision'])
        self.assertEqual((self.home / '.bazelrc').read_text(), raw)
        self.assertEqual(os.environ.get('HOME'), self.start_home)

    def test_inherited_downloader_is_rejected_by_existing_owner_comparison(self):
        self.install_review_rc_owner()
        (self.home / '.bazelrc').write_text('common --experimental_downloader_config=/unreviewed\n')
        with self.assertRaisesRegex(ValueError, 'Home Bazel rc changes'): self.run_cohort()
        self.assertEqual(self.events, [])

    def test_unoverridden_hardlink_setting_is_not_silently_ignored(self):
        self.install_review_rc_owner()
        (self.home / '.bazelrc').write_text('build --experimental_repository_cache_hardlinks\n')
        with self.assertRaisesRegex(ValueError, 'Home Bazel rc changes'): self.run_cohort()
        self.assertEqual(self.events, [])

    def test_original_rc_owner_rejects_missing_option_value(self):
        self.install_review_rc_owner()
        (self.home / '.bazelrc').write_text('build --disk_cache\n')
        with self.assertRaisesRegex(ValueError, 'missing cache option value'): self.run_cohort()
        self.assertEqual(self.events, [])

    def test_source_identity_is_checked_before_loading_the_rc_owner(self):
        self.install_review_rc_owner()
        (self.home / '.bazelrc').write_text('# existing runner rc\n')
        self.source_check.side_effect = ValueError('different source identity')
        with mock.patch.object(caller.importlib.util, 'spec_from_file_location') as load:
            with self.assertRaisesRegex(ValueError, 'different source identity'): self.run_cohort()
        load.assert_not_called()
        self.assertEqual(self.events, [])

    def test_accepted_home_rc_does_not_relax_the_native_archive_gate(self):
        self.install_review_rc_owner()
        (self.home / '.bazelrc').write_text('build --disk_cache=/existing\n')
        self.problem = 'native'
        with self.assertRaisesRegex(ValueError, 'Accepted native archive differs'): self.run_cohort()
        self.assertEqual(self.events, ['swss.manifests', 'syncd.manifests', 'audit', 'build'])
        self.assertFalse(self.output.exists())

    def test_ambient_launcher_is_rejected_before_make(self):
        with mock.patch.dict(os.environ, {'BAZEL_REAL': 'bazel'}):
            with self.assertRaisesRegex(ValueError, 'prepared remote-only'): self.run_cohort()
        self.assertEqual(self.events, [])

    def test_unreviewed_policy_target_is_rejected_when_loading_plan(self):
        value = copy.deepcopy(self.plan)
        value['policy_targets']['swss-installed-images-policy.json'] = '//unreviewed:target'
        path = self.root / 'bad-plan.json'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'Different coherent policy scope'):
            caller.load_plan(path)

    def test_base_archive_is_required_in_the_fixed_export_plan(self):
        for replacement in (None, '//unreviewed:base.gz'):
            with self.subTest(replacement=replacement):
                value = copy.deepcopy(self.plan)
                if replacement is None:
                    del value['exports']['docker-base-trixie.gz']
                else:
                    value['exports']['docker-base-trixie.gz'] = replacement
                path = self.root / 'base-plan.json'
                path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'Different primary target'):
                    caller.load_plan(path)
        self.assertEqual(self.events, [])

    def test_tampered_base_archive_stops_before_merge_and_publication(self):
        collect = self.owners[3].collect_archives
        def tampered(*args, **kwargs):
            paths = collect(*args, **kwargs)
            paths['docker-base-trixie.gz'].write_bytes(b'changed base bytes')
            return paths
        self.owners[3].collect_archives = tampered
        with self.assertRaisesRegex(ValueError, 'Exported archive identity differs'):
            self.run_cohort()
        self.assertNotIn('merge', self.events)
        self.assertFalse(self.output.exists())

    def test_same_graph_base_is_copied_and_reported_without_expanding_policy_images(self):
        result = self.run_cohort()
        path = self.output / 'archives/docker-base-trixie.gz'
        self.assertEqual(path.read_bytes(), self.payload['docker-base-trixie.gz'])
        expected = {'bytes': path.stat().st_size, 'sha256': sha(path)}
        self.assertEqual(result['archives']['docker-base-trixie'], expected)
        report = json.loads((self.output / 'reports/cohort.json').read_bytes())
        self.assertEqual(report['archives']['docker-base-trixie'], expected)
        for family, (_, images) in caller.FAMILIES.items():
            policy = json.loads((self.output / 'reports' /
                                 (family + '-installed-images-policy.json')).read_bytes())
            self.assertEqual({row['name'] for row in policy['images']},
                             {'docker-config-engine-trixie', *images})



    def base_prefix_fixture(self, observed):
        private = self.scratch / 'base-check'
        private.mkdir()
        expected = ['sha256:' + str(index) * 64 for index in range(9)]
        plan = dict(self.plan, expected_native_base_diff_ids=expected)
        calls = []
        def collect(source, directory, receipt, targets, **kwargs):
            self.assertEqual(list(targets), ['docker-base-trixie.gz'])
            self.assertEqual(kwargs['additional_targets'], ['//dockers/docker-base-trixie:docker-base-trixie'])
            calls.append('base-build')
            archive = directory / 'docker-base-trixie.gz'
            archive.write_bytes(b'genuine fixture archive')
            return {'docker-base-trixie.gz': archive}
        def query(*args, **kwargs):
            calls.append('base-layout')
            return 'bazel-out/base-layout\n'
        audit = SimpleNamespace(audit_targets=lambda *a, **kw: calls.append('base-audit'))
        layout = SimpleNamespace(config={'rootfs': {'diff_ids': observed(expected)}})
        return plan, private, calls, audit, SimpleNamespace(collect_archives=collect), SimpleNamespace(execute=query), lambda *args: layout

    def test_base_prefix_gate_records_all_original_ids_without_admission(self):
        plan, private, calls, audit, builder, logs, reader = self.base_prefix_fixture(lambda value: value[:])
        receipt = {}
        result = caller.check_base_parent(plan, self.source, private, receipt, 'fixed-proxy',
                                         audit, builder, logs, reader, self.owners[0].check)
        self.assertEqual(calls, ['base-audit', 'base-build', 'base-layout'])
        self.assertEqual(result['expected_diff_ids'], result['observed_diff_ids'])
        self.assertFalse(result['stored_layer_or_full_archive_equivalence'])
        self.assertTrue(result['full_native_archive_gate_required'])
        self.assertEqual(receipt['base_parent_preflight'], result)

    def test_base_prefix_mismatch_preserves_full_expected_and_observed_ids(self):
        plan, private, calls, audit, builder, logs, reader = self.base_prefix_fixture(
            lambda value: value[:2] + ['sha256:' + 'f' * 64] + value[3:])
        receipt = {}
        with self.assertRaisesRegex(ValueError, 'BASE parent differs') as raised:
            caller.check_base_parent(plan, self.source, private, receipt, 'fixed-proxy',
                                     audit, builder, logs, reader, self.owners[0].check)
        original = json.loads((private / 'base-parent-preflight/prefix.json').read_bytes())
        self.assertEqual(original['status'], 'failed')
        self.assertEqual(len(original['expected_diff_ids']), 9)
        self.assertEqual(len(original['observed_diff_ids']), 9)
        for value in original['expected_diff_ids'] + original['observed_diff_ids']:
            self.assertIn(value, str(raised.exception))
        self.assertEqual(receipt['base_parent_preflight'], original)

    def test_base_prefix_layout_validation_failure_is_not_ignored(self):
        plan, private, calls, audit, builder, logs, reader = self.base_prefix_fixture(lambda value: value[:])
        def reject(*args):
            raise ValueError('original layout validation failed')
        receipt = {}
        with self.assertRaisesRegex(ValueError, 'original layout validation failed'):
            caller.check_base_parent(plan, self.source, private, receipt, 'fixed-proxy',
                                     audit, builder, logs, reject, self.owners[0].check)
        self.assertNotIn('base_parent_preflight', receipt)

    def test_component_proxy_forwards_final_options_and_preserves_arguments(self):
        source = self.root / 'source with spaces'
        owner = source / 'tools/bazel/ci/review_bazel.sh'
        owner.parent.mkdir(parents=True)
        owner.write_text('#!' + sys.executable + '\nimport json, os, sys\nprint(json.dumps([sys.argv[1:], os.environ.get("HOME")]))\n')
        owner.chmod(0o700)
        command = caller.shlex.join([str(owner), '--batch',
                                    '--output_user_root=' + str(self.scratch / 'output-user-root')])
        proxy = self.scratch / 'review-bazel'
        proxy.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
        caller.prepare_component_proxy(source, self.scratch)
        for action in ('build', 'test', 'aquery', 'cquery', 'info'):
            for supplied in ([], caller.COMPONENT_PROFILE[:1], caller.COMPONENT_PROFILE):
                with self.subTest(action=action, supplied=supplied):
                    arguments = [action, 'a b', "x'y", *supplied]
                    result = subprocess.run([str(proxy), *arguments],
                                            check=True, text=True, capture_output=True)
                    argv, child_home = json.loads(result.stdout)
                    expected = ['--batch', '--output_user_root=' + str(self.scratch / 'output-user-root'),
                                *arguments]
                    if action != 'info':
                        expected.extend(option for option in caller.COMPONENT_PROFILE if option not in supplied)
                    self.assertEqual(argv, expected)
                    self.assertEqual(child_home, self.start_home)
        self.assertEqual(os.environ.get('HOME'), self.start_home)

    def test_component_proxy_rejects_unreviewed_prepared_command(self):
        proxy = self.scratch / 'review-bazel'
        proxy.write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(ValueError, 'Different prepared review proxy'):
            caller.prepare_component_proxy(self.source, self.scratch)
        self.assertEqual(proxy.read_text(), '#!/bin/sh\nexit 0\n')

    def test_component_native_gate_stops_before_remaining_actions(self):
        private = self.scratch / 'preflight'
        private.mkdir()
        calls = []
        audit = SimpleNamespace(audit_targets=lambda *a, **kw: calls.append('audit') or {})
        builder = SimpleNamespace(collect_archives=lambda *a, **kw: calls.append('collect') or {})
        def reject(*args):
            calls.append('native-equality')
            raise ValueError('accepted native mismatch')
        with mock.patch.object(caller, 'bind_policies', side_effect=reject):
            with self.assertRaisesRegex(ValueError, 'accepted native mismatch'):
                caller.collect_with_native_anchor(self.plan, self.source, private, {},
                    {**caller.PRIMARY, 'additional.tar': '//component:archive'}, [],
                    'fixed-proxy', audit, builder, self.owners[0].check, self.owners[-2])
        self.assertEqual(calls, ['audit', 'collect', 'native-equality'])

    def test_component_native_gate_precedes_remaining_collection(self):
        private = self.scratch / 'preflight'
        private.mkdir()
        calls = []
        audit = SimpleNamespace(audit_targets=lambda *a, **kw: calls.append('audit') or {})
        def collect(source, directory, receipt, targets, **kwargs):
            calls.append(('collect', set(targets)))
            return {name: directory / name for name in targets}
        builder = SimpleNamespace(collect_archives=collect)
        def accept(*args):
            calls.append('native-equality')
            return {'genuine-native': True}, {'genuine-policies': True}
        receipt = {}
        exports = {**caller.PRIMARY, 'additional.tar': '//component:archive'}
        with mock.patch.object(caller, 'bind_policies', side_effect=accept):
            paths = caller.collect_with_native_anchor(self.plan, self.source, private, receipt,
                exports, [], 'fixed-proxy', audit, builder, self.owners[0].check, self.owners[-2])
        self.assertEqual(calls, ['audit', ('collect', set(caller.PRIMARY)), 'native-equality',
                                 'audit', ('collect', {'additional.tar'})])
        self.assertEqual(set(paths), set(exports))
        self.assertIs(receipt['native_parent_preflight']['passed'], True)

    def test_component_plan_requires_the_complete_fixed_family_set(self):
        value = json.loads(PLAN.read_bytes())
        self.assertTrue(value['component_refresh'])
        self.assertEqual(len(caller.component_primary()), 32)
        self.assertIn('docker-dash-engine.gz', caller.component_primary())
        self.assertNotIn('docker-dash-engine-dbg.gz', caller.component_primary())
        value['component_families'].remove('dash-engine')
        path = self.root / 'changed-plan.json'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'Different component refresh scope'):
            caller.load_plan(path)

    def test_component_archive_binding_rejects_changed_selected_bytes(self):
        paths, receipt = {}, {'artifacts': {}}
        for name, target in caller.component_primary().items():
            path = self.scratch / name
            path.write_bytes(('accepted:' + name).encode())
            paths[name] = path
            receipt['artifacts'][name] = {'target': target, 'bytes': path.stat().st_size, 'sha256': sha(path)}
        actual = caller.bind_component_archives(paths, receipt, self.owners[0].check)
        self.assertEqual(len(actual), 32)
        paths['docker-dash-engine.gz'].write_bytes(b'different archive')
        with self.assertRaisesRegex(ValueError, 'Component archive identity differs'):
            caller.bind_component_archives(paths, receipt, self.owners[0].check)

    def component_validation_fixture(self, family, incremental):
        private = self.scratch / 'component-validation'
        private.mkdir()
        spec = copy.deepcopy(caller.COMPONENTS[family])
        self.enterContext(mock.patch.object(caller, 'COMPONENTS', {family: spec}))
        targets = {name: spec['prefix'] + name for name in spec['selected_archives']}
        paths, receipt = {}, {'commands': [], 'artifacts': {}, 'action_audit': {}}
        for name, target in targets.items():
            path = private / name
            path.write_bytes(name.encode())
            paths[name] = path
            receipt['artifacts'][name] = {'target': target, 'bytes': path.stat().st_size, 'sha256': sha(path)}
        for name in ('build-events.jsonl', 'build.execution.json'):
            (private / name).write_text('{}\n')
        def validate(directory):
            (directory / 'validation.json').write_text(json.dumps({
                'status': 'passed', 'revision': self.plan['source_revision']}))
        owner = SimpleNamespace(TARGETS=targets, validate_images=validate, incremental=incremental)
        layouts = {label: 'bazel-out/example' for label in [spec['parent'],
                   *[spec['prefix'] + name for name in spec['images']]]}
        self.enterContext(mock.patch.object(caller, 'component_owner_checks', return_value={}))
        return (private, receipt, paths, layouts, owner)

    def run_component_validation_fixture(self, family, incremental):
        private, receipt, paths, layouts, owner = self.component_validation_fixture(family, incremental)
        return caller.validate_components(self.plan, self.source, private, receipt, paths, layouts,
            {'revision': self.plan['source_revision']}, {}, {family: owner}, 'fixed-proxy',
            self.owners[4], shutil.copyfile, self.owners[0].check, mock.Mock(), self.owners[2])

    def test_component_original_statusless_incremental_is_retained(self):
        def incremental(args, directory):
            self.assertEqual(args.bazel, 'fixed-proxy')
            value = {phase: {'sha256': {'image': 'a'}} for phase in ('baseline', 'noop', 'restore')}
            value['edit'] = {'sha256': {'image': 'b'}}
            (directory / 'incremental.json').write_text(json.dumps(value))
        _, records, _ = self.run_component_validation_fixture('database', incremental)
        self.assertIn('database', records)
        self.assertNotIn('status', json.loads((self.scratch / 'component-validation/database/incremental.json').read_bytes()))

    def test_component_owner_exception_never_becomes_success(self):
        def incremental(args, directory):
            (directory / 'incremental.json').write_text('{"status":"passed"}')
            raise ValueError('original owner failed restoration')
        with self.assertRaisesRegex(ValueError, 'original owner failed restoration'):
            self.run_component_validation_fixture('database', incremental)

    def test_component_failed_incremental_record_is_rejected(self):
        def incremental(args, directory):
            value = {phase: {'sha256': {'image': 'a'}} for phase in ('baseline', 'noop', 'restore')}
            value.update(edit={'sha256': {'image': 'b'}}, status='failed')
            (directory / 'incremental.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'Component incremental check did not pass'):
            self.run_component_validation_fixture('mgmt-framework', incremental)

    def test_component_incremental_changed_restore_is_rejected(self):
        def incremental(args, directory):
            value = dict(baseline={'image': 'a'}, unchanged={'image': 'a'},
                         changed={'image': 'b'}, restored={'image': 'c'}, status='passed')
            (directory / 'incremental.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'Component incremental restoration differs'):
            self.run_component_validation_fixture('snmp', incremental)




class BaseOwnerJoinTests(unittest.TestCase):
    def fixture(self, failure=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, private = root / 'source', root / 'private'
        source.mkdir(); private.mkdir(); (private / 'owner-checks').mkdir()
        revisions = {'base_images.py': '510da5e87c168fae4b9ccbaa491255610e237f855f976f8df3e184afa7e3762b',
                     'config_engine_rebuild.py': 'a77668728df8bd5f417541d9819a82e727dde8d73eb38c32ab93991857aa547b'}
        for name in revisions:
            path = source / 'tools/bazel/ci' / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text('source data only')
        files = ['docker-base-trixie.gz', 'docker-config-engine-trixie.gz',
                 'config-engine.startup.tar', 'config-engine.python-and-models.tar',
                 'docker-config-engine-trixie.source.tar']
        artifacts, collected_paths = {}, {}
        for name in files:
            stage = private / ('native-parent-preflight' if name.endswith('.gz') else 'component-remainder')
            stage.mkdir(exist_ok=True)
            path = stage / name; path.write_text('original-' + name)
            collected_paths[name] = path
            artifacts[name] = {'target': '//fixed:' + name, 'bytes': path.stat().st_size, 'sha256': sha(path)}
        receipt = {'source_revision': 'c' * 40, 'artifacts': artifacts}
        self.calls = []
        def check(path):
            return revisions[path.name] if path.parent == source / 'tools/bazel/ci' else sha(path)
        def execute(argv, directory, outer, label, **kwargs):
            self.calls.append(label)
            self.assertEqual(kwargs['cwd'], source)
            if label == 'original-base-owner':
                self.assertIn('--config-engine-acceptance', argv)
                for name in files: shutil.copyfile(collected_paths[name], directory / name)
                policy = directory / 'installed-images-policy.json'; policy.write_text('{"images": []}')
                images = {}
                for image in ('docker-base-trixie', 'docker-config-engine-trixie'):
                    images[image] = {'manifest_digest': 'sha256:' + 'a' * 64}
                    (directory / (image + '.validation.json')).write_text(json.dumps({
                        **images[image], 'config': {'Env': ['PATH=/usr/bin']}}))
                value = {'status': 'passed', 'architecture': 'amd64', 'revision': 'c' * 40,
                         'images': images, 'artifacts': artifacts, 'installed_images_policy_sha256': sha(policy)}
                (directory / 'receipt.json').write_text(json.dumps(value))
                (directory / 'action-audit.json').write_text('{"status": "passed"}')
                if failure == 'archive': (directory / files[0]).write_text('changed')
            else:
                self.assertEqual(label, 'original-config-engine-rebuild')
                selected = dict(zip(('base', 'image', 'startup', 'python_and_models', 'native'), files))
                baseline = {name: artifacts[file]['sha256'] for name, file in selected.items()}
                edited = {**baseline, 'startup': 'e' * 64, 'image': 'e' * 64}
                restored = {**baseline, 'native': 'f' * 64} if failure == 'restore' else baseline
                (directory / 'config-engine-rebuild.json').write_text(json.dumps({'status': 'passed',
                    'phases': {name: {'sha256': value} for name, value in (
                        ('baseline', baseline), ('noop', baseline), ('template-edit', edited), ('restore', restored))}}))
        self.enterContext(mock.patch.object(caller, 'manifest_snapshot', return_value={'manifest': 'fixed'}))
        return source, private, receipt, 'fixed-bazel', SimpleNamespace(execute=execute), shutil.copyfile, SimpleNamespace(sha=check), (lambda value: None), collected_paths

    def test_original_base_and_rebuild_results_bind_exact_cohort_bytes(self):
        args = self.fixture()
        result = caller.component_base_owner_checks(*args)
        self.assertEqual(self.calls, ['original-base-owner', 'original-config-engine-rebuild'])
        self.assertEqual(result['status'], 'passed')
        self.assertNotIn('Env', json.dumps(result))
        self.assertFalse(result['diagnostic_validation_public_admission_claimed'])
        self.assertTrue((args[1] / 'owner-checks/base/installed-images-policy.json').is_file())

    def test_base_join_uses_native_first_paths_and_preserves_remainder_outputs(self):
        args = self.fixture()
        before = {name: sha(path) for name, path in args[-1].items()}
        self.assertFalse((args[1] / 'docker-base-trixie.gz').exists())
        self.assertEqual(args[-1]['docker-base-trixie.gz'].parent.name, 'native-parent-preflight')
        self.assertEqual(args[-1]['config-engine.startup.tar'].parent.name, 'component-remainder')
        caller.component_base_owner_checks(*args)
        self.assertEqual({name: sha(path) for name, path in args[-1].items()}, before)

    def test_base_archive_mismatch_stops_before_rebuild_and_runtime(self):
        args = self.fixture('archive')
        with self.assertRaisesRegex(ValueError, 'archive differs'):
            caller.component_base_owner_checks(*args)
        self.assertEqual(self.calls, ['original-base-owner'])

    def test_config_engine_restoration_must_match_original_collected_outputs(self):
        args = self.fixture('restore')
        with self.assertRaisesRegex(ValueError, 'did not restore'):
            caller.component_base_owner_checks(*args)
        self.assertFalse((args[1] / 'owner-checks/base/source-owner.json').exists())

class RuntimeWiringTests(unittest.TestCase):
    def test_root_phase_accepts_only_context_and_seal_without_build_arguments(self):
        with mock.patch.object(sys, 'argv', ['caller', 'runtime-root', '--runtime-context',
                '/fixed/context.json', '--runtime-context-sha256', 'a' * 64]), \
                mock.patch.object(caller, 'component_runtime_root', return_value=0) as root:
            self.assertEqual(caller.main(), 0)
        root.assert_called_once_with(Path('/fixed/context.json'), 'a' * 64)

    def test_root_phase_rejects_general_build_arguments(self):
        with mock.patch.object(sys, 'argv', ['caller', 'runtime-root', '--runtime-context',
                '/fixed/context.json', '--runtime-context-sha256', 'a' * 64, '--source', '/fixed/source']), \
                mock.patch.object(caller, 'component_runtime_root') as root:
            with self.assertRaisesRegex(ValueError, 'only its sealed context'):
                caller.main()
        root.assert_not_called()

    def test_runtime_phase_uses_distinct_control_source_native_and_output_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dirs = {name: root / name for name in ('source', 'native', 'scratch', 'output', 'control')}
            for path in dirs.values():
                path.mkdir()
            plan = caller.load_plan(PLAN)
            argv = ['caller', 'runtime', '--plan', str(PLAN), '--archives', str(root),
                    '--mirrors', str(root)]
            for name, path in dirs.items():
                argv.extend(['--' + name, str(path)])
            with mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(caller, 'environment_check') as environment, \
                    mock.patch.object(caller, 'run_component_runtimes', return_value={'status': 'pending'}) as run:
                self.assertEqual(caller.main(), 0)
            environment.assert_called_once_with(plan, dirs['scratch'], dirs['source'])
            run.assert_called_once_with(plan, PLAN.resolve(), dirs['source'], dirs['native'],
                                        dirs['scratch'], dirs['output'], dirs['control'])

    def test_child_command_preserves_home_presence_without_replacement(self):
        present = caller.component_root_command(Path('/fixed/context'), 'a' * 64, True)
        absent = caller.component_root_command(Path('/fixed/context'), 'a' * 64, False)
        self.assertEqual(present[:3], ['sudo', '-n', '--preserve-env=HOME'])
        self.assertEqual(absent[:5], ['sudo', '-n', '/usr/bin/env', '-u', 'HOME'])
        for value in (present, absent):
            self.assertIn('runtime-root', value)
            self.assertNotIn('--source', value)
            self.assertNotIn('--plan', value)

    def test_group_paths_are_distinct_for_both_state_and_exec(self):
        row = {'run': '123', 'attempt': '1'}
        owner = SimpleNamespace(state_paths=lambda row, temporary: {
            'state': Path('/tmp/fixed'), 'exec': Path('/run/fixed')})
        rows = [caller.component_runtime_paths(owner, row, '/tmp', family)
                for family in caller.component_runtime_families()]
        self.assertEqual(len(rows), 19)
        self.assertEqual(len({item['state'] for item in rows}), 19)
        self.assertEqual(len({item['exec'] for item in rows}), 19)
        self.assertTrue(all(item['component_runtime'] is True for item in rows))
        self.assertTrue(all(item['docker_data'].name == 'source-image-docker-123-1' for item in rows))

    def test_public_projection_excludes_complete_private_supervision(self):
        identity = {'bytes': 5, 'sha256': 'b' * 64}
        original = {'schema': 1, 'status': 'passed', 'source_revision': 'c' * 40,
            'source_tree': 'd' * 40, 'operator': {'run': '123'}, 'cohort_sha256': 'e' * 64,
            'host_kernel_configuration_changed': False, 'full_vs_admission_claimed': False,
            'prerequisites': {'private_path': '/private/worker'}, 'archives': {'image': identity},
            'groups': {'base': {'status': 'passed', 'daemon_started': True, 'daemon_closed': True,
                'native_stop': {'private_data_removed': True, 'diagnostic_path': '/private/cleanup'},
                'component_runtime': {'revision': 'c' * 40, 'archives': {'image': identity},
                    'reports': {'runtime.json': {**identity, 'path': '/private/report',
                        'original_path': '/private/original'}},
                    'original_owner_results': {'environment': {'HOME': '/private/home'}}}}}}
        snapshot = copy.deepcopy(original)
        public = caller.public_component_runtime_result(original)
        self.assertEqual(original, snapshot)
        self.assertNotIn('/private', json.dumps(public))
        self.assertEqual(public['groups']['base']['reports'], {'runtime.json': identity})
        self.assertTrue(public['groups']['base']['private_data_removed'])

    def test_runtime_staging_uses_distinct_component_owner_and_preserves_observer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, native, control, scratch = [root / name for name in ('source', 'native', 'control', 'scratch')]
            for path in (source, native, control, scratch):
                path.mkdir()
            name = 'tools/bazel/ci/vs_runtime_check.py'
            component = 'tools/bazel/ci/vs_component_runtime_owner.py'
            original = control / name
            original.parent.mkdir(parents=True)
            original.write_bytes(b'original observer and its pinned jobs')
            raw = b'qualified component owner'
            (control / component).write_bytes(raw)
            entry = {'origin': 'control', 'source_path': component, 'bytes': len(raw),
                     'sha256': hashlib.sha256(raw).hexdigest()}
            staged = caller.stage_component_runtime({'runtime_control_files': {name: entry}},
                source, native, scratch, control)
            self.assertEqual((staged / name).read_bytes(), raw)
            self.assertEqual(original.read_bytes(), b'original observer and its pinned jobs')
            rejected = root / 'rejected'
            rejected.mkdir()
            with self.assertRaisesRegex(ValueError, 'Different runtime control source path'):
                caller.stage_component_runtime({'runtime_control_files': {name: dict(entry, source_path=name)}},
                    source, native, rejected, control)

    def test_runtime_staging_uses_native_preflight_without_consumer_substitution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, native, control, scratch = [root / name for name in ('source', 'native', 'control', 'scratch')]
            for path in (source, native, control, scratch):
                path.mkdir()
            name = 'tools/bazel/ci/source_worker_preflight.py'
            for path, data in ((source, b'different consumer API'), (native, b'qualified native API')):
                target = path / name
                target.parent.mkdir(parents=True)
                target.write_bytes(data)
            raw = (native / name).read_bytes()
            plan = {'runtime_control_files': {name: {'origin': 'native', 'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest()}}}
            staged = caller.stage_component_runtime(plan, source, native, scratch, control)
            self.assertEqual((staged / name).read_bytes(), raw)

if __name__ == '__main__':
    unittest.main()
