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

    def test_component_source_setup_precedes_manifests_and_bazel_gates(self):
        (self.scratch / 'review-bazel').write_text(caller.component_proxy_text(self.source, self.scratch))
        with mock.patch.object(caller, 'declarations', return_value=({}, [])), \
                mock.patch.object(caller, 'prepare_component_gnoi',
                                  side_effect=ValueError('fixture setup stopped')) as initialize, \
                mock.patch.object(caller, 'check_base_parent') as base:
            with self.assertRaisesRegex(ValueError, 'fixture setup stopped'):
                caller.build_cohort(self.plan, self.source, self.scratch, self.output,
                                    self.owners, components={})
        initialize.assert_called_once()
        base.assert_not_called()
        self.assertEqual(self.events, [])

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
        with mock.patch.object(caller, 'bind_policies', side_effect=accept), \
                mock.patch.object(caller, 'collect_batched_component_exports', side_effect=collect):
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

class ExportAliasTests(unittest.TestCase):
    def test_exact_label_aliases_keep_all_files_and_fresh_receipts(self):
        targets = {'first--source.tar': '//first:source.tar',
                   'second--source.tar': '//first:source.tar',
                   'third--source.tar': '//third:source.tar'}
        original_targets = dict(targets)
        options = ['--config=syncd-dash', '--keep_going']
        layouts = ['//parent:image']
        previous = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for phase in range(2):
                directory = root / str(phase)
                directory.mkdir()
                calls, copies = [], []
                receipt = {'artifacts': {'native.gz': {'untouched': True}}}
                execution_log = directory / 'build.execution.json'

                def collect(source, output, record, selected, **kwargs):
                    calls.append((dict(selected), kwargs))
                    paths = {}
                    for name, target in selected.items():
                        path = output / name
                        path.write_bytes((str(phase) + ':' + target).encode())
                        record['artifacts'][name] = {
                            'target': target, 'bytes': path.stat().st_size,
                            'sha256': sha(path)}
                        paths[name] = path
                    return paths

                def export(source, destination):
                    copies.append((source.name, destination.name))
                    shutil.copyfile(source, destination)

                builder = SimpleNamespace(collect_archives=collect, export_archive=export)
                paths = caller.collect_component_exports(
                    root, directory, receipt, targets, builder=builder,
                    check=SimpleNamespace(sha=sha), bazel='fixed-proxy', options=options,
                    additional_targets=layouts, execution_log=execution_log)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], {name: targets[name] for name in
                                              ('first--source.tar', 'third--source.tar')})
                self.assertEqual(calls[0][1], {'bazel': 'fixed-proxy', 'options': options,
                    'additional_targets': layouts, 'execution_log': execution_log})
                self.assertEqual(copies, [('first--source.tar', 'second--source.tar')])
                self.assertEqual(list(paths), list(targets))
                self.assertEqual(targets, original_targets)
                self.assertEqual(receipt['artifacts']['native.gz'], {'untouched': True})
                for name, target in targets.items():
                    self.assertEqual(receipt['artifacts'][name], {
                        'target': target, 'bytes': paths[name].stat().st_size,
                        'sha256': sha(paths[name])})
                self.assertEqual(sha(paths['first--source.tar']), sha(paths['second--source.tar']))
                self.assertNotEqual(sha(paths['first--source.tar']), sha(paths['third--source.tar']))
                if previous is not None:
                    self.assertNotEqual(previous, sha(paths['first--source.tar']))
                previous = sha(paths['first--source.tar'])

    def test_missing_outputs_and_changed_alias_copies_fail_closed(self):
        for problem, message in (
                ('missing-output', 'collector outputs differ'),
                ('missing-file', 'representative file differs'),
                ('changed-copy', 'alias copy differs'),
                ('invalid-alias', 'plain output filenames')):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                targets = {'first.tar': '//same:source', 'alias.tar': '//same:source'}
                if problem == 'invalid-alias':
                    targets['../outside.tar'] = targets.pop('alias.tar')
                receipt = {'artifacts': {'native.gz': {'untouched': True}}}
                calls, copies = [], []

                def collect(source, directory, record, selected, **kwargs):
                    calls.append(dict(selected))
                    if problem == 'missing-output':
                        return {}
                    path = directory / 'first.tar'
                    raw = b'original payload'
                    record['artifacts']['first.tar'] = {
                        'target': '//same:source', 'bytes': len(raw),
                        'sha256': hashlib.sha256(raw).hexdigest()}
                    if problem != 'missing-file':
                        path.write_bytes(raw)
                    return {'first.tar': path}

                def export(source, destination):
                    copies.append(destination)
                    destination.write_bytes(b'changed payload')

                builder = SimpleNamespace(collect_archives=collect, export_archive=export)
                with self.assertRaisesRegex(ValueError, message):
                    caller.collect_component_exports(
                        root, root, receipt, targets, builder=builder,
                        check=SimpleNamespace(sha=sha), bazel='fixed-proxy', options=[],
                        additional_targets=[], execution_log=root / 'build.execution.json')
                self.assertEqual(receipt['artifacts']['native.gz'], {'untouched': True})
                self.assertNotIn('alias.tar', receipt['artifacts'])
                if problem != 'changed-copy':
                    self.assertEqual(copies, [])
                if problem == 'invalid-alias':
                    self.assertEqual(calls, [])


class BatchedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.labels = sorted(caller.BATCH_MAIN_LABELS)
        self.a, self.b = self.labels[:2]

    def event(self, label, configuration='opaque-build-id', **changes):
        value = {'id': {'targetCompleted': {'label': label,
                 'configuration': {'id': configuration}}}, 'completed': {'success': True}}
        value.update(changes)
        return value

    def events(self, rows):
        path = self.root / 'build-events.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def metadata(self, labels, checksum='1' * 64):
        # Pinned8.5.1 analysis_v2.proto184-209/249-271 and build.proto285-296/470-497:
        # uint32 IDs are numbers; required RULE/name/ruleClass are present.
        # Default false isTool and empty repeated fields may be omitted.
        return {'results': [{'target': {'type': 'RULE', 'rule': {
            'name': label, 'ruleClass': 'pkg_tar'}},
            'configuration': {'checksum': checksum}, 'configurationId': 1}
            for label in labels], 'configurations': [{'id': 1, 'checksum': checksum}]}

    def test_completed_groups_use_only_same_stream_success_without_aspects(self):
        aspect = self.event(self.a, 'other')
        aspect['id']['targetCompleted']['aspect'] = 'actual-aspect'
        rows = [self.event(self.b, 'opaque-B'), aspect, self.event(self.a, 'opaque-A')]
        self.assertEqual(caller.batch_completed_groups(self.events(rows), [self.a, self.b]),
                         [[self.a], [self.b]])
        # No arbitrary four-configuration performance gate.
        rows = [self.event(label, 'opaque-' + str(i)) for i, label in enumerate(self.labels[:5])]
        self.assertEqual(len(caller.batch_completed_groups(self.events(rows), self.labels[:5])), 5)

    def test_missing_duplicate_failed_and_unconfigured_build_labels_are_rejected(self):
        failed = self.event(self.a, completed={'success': False})
        missing_config = self.event(self.a)
        missing_config['id']['targetCompleted'].pop('configuration')
        cases = [([], 'Incomplete'), ([self.event(self.a), self.event(self.a)], 'Repeated'),
                 ([failed], 'successfully'), ([missing_config], 'configuration')]
        for rows, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                caller.batch_completed_groups(self.events(rows), [self.a])

    def test_metadata_uses_full_actual_checksum_and_exact_labels(self):
        value = self.metadata([self.b, self.a], 'abcdef0123456789' * 4)
        self.assertEqual(caller.batch_configuration(value, [self.a, self.b]),
                         'abcdef0123456789' * 4)
        for checksum in ('a' * 7, 'a' * 32, 'a' * 63, 'a' * 65, 'a' * 128, 'A' * 64, 'g' * 64):
            with self.subTest(checksum=checksum), self.assertRaisesRegex(ValueError, 'configuration identity'):
                caller.batch_configuration(self.metadata([self.a, self.b], checksum), [self.a, self.b])
        for problem in ('missing', 'duplicate', 'alias', 'non-rule', 'changed-label',
                        'missing-config', 'duplicate-config', 'ambiguous-config', 'null-config', 'error-row'):
            value = self.metadata([self.a, self.b])
            if problem == 'missing': value['results'].pop()
            if problem == 'duplicate': value['results'].append(copy.deepcopy(value['results'][0]))
            if problem == 'alias': value['results'][0]['target']['rule']['ruleClass'] = 'alias'
            if problem == 'non-rule': value['results'][0] = {'target': {'type': 'SOURCE_FILE', 'sourceFile': {'name': self.a}}, 'configuration': {'checksum': 'null'}}
            if problem == 'changed-label': value['results'][0]['target']['rule']['name'] = '//other:actual'
            if problem == 'missing-config': value['results'][0]['configurationId'] = 99
            if problem == 'null-config':
                value['results'][0].pop('configurationId')
                value['results'][0]['configuration'] = {'checksum': 'null'}
            if problem == 'error-row': value['results'][0] = {'error': 'original query error'}
            if problem == 'duplicate-config': value['configurations'].append(value['configurations'][0])
            if problem == 'ambiguous-config':
                value['configurations'].append({'id': 2, 'checksum': '2' * 64})
                value['results'][1]['configurationId'] = 2
                value['results'][1]['configuration']['checksum'] = '2' * 64
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                caller.batch_configuration(value, [self.a, self.b])

    def test_file_rows_are_labeled_complete_unique_and_single_relative_outputs(self):
        rows = [{'label': self.b, 'files': ['bazel-out/b']},
                {'label': self.a, 'files': ['bazel-out/a']}]
        encode = lambda values: '\n'.join(json.dumps(x) for x in values)
        self.assertEqual(caller.batch_output_paths(encode(rows), [self.a, self.b]),
                         {self.a: 'bazel-out/a', self.b: 'bazel-out/b'})
        for problem in ('missing', 'duplicate', 'unknown', 'empty', 'multiple', 'absolute', 'parent'):
            changed = copy.deepcopy(rows)
            if problem == 'missing': changed.pop()
            if problem == 'duplicate': changed.append(changed[0])
            if problem == 'unknown': changed[0]['label'] = '//unknown:target'
            if problem == 'empty': changed[0]['files'] = []
            if problem == 'multiple': changed[0]['files'].append('bazel-out/c')
            if problem == 'absolute': changed[0]['files'] = ['/outside']
            if problem == 'parent': changed[0]['files'] = ['../outside']
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                caller.batch_output_paths(encode(changed), [self.a, self.b])
        with self.assertRaisesRegex(ValueError, 'Repeated discovery JSON key'):
            caller.batch_json('{"results":[],"results":[]}')

    def fixture(self, problem=None):
        directory = self.root / 'private'
        directory.mkdir()
        execution = self.root / 'execroot'
        execution.mkdir()
        targets = {f'item-{i}.tar': label for i, label in enumerate(self.labels)}
        targets.update({'external.tar': '@existing//:runtime', 'external-alias.tar': '@existing//:runtime'})
        receipt = {'commands': [], 'artifacts': {'native.gz': {'original': True}}}
        calls = []
        groups = {label: i % 2 for i, label in enumerate(self.labels)}
        filenames = {label: 'file-' + str(i) for i, label in enumerate(self.labels)}
        for label, filename in filenames.items():
            (execution / filename).write_bytes(label.encode())
        if problem == 'missing-file': (execution / filenames[self.a]).unlink()
        if problem == 'empty-file': (execution / filenames[self.a]).write_bytes(b'')

        def collect(source, output, record, selected, **kwargs):
            calls.append((dict(selected), kwargs))
            record['commands'].append({'argv': ['fixed-proxy', 'build'], 'returncode': 0})
            rows = [self.event(label, 'opaque-' + str(groups[label])) for label in self.labels]
            (output / 'build-events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in rows))
            paths = {}
            for name, label in selected.items():
                path = output / name
                path.write_bytes(b'original external payload')
                paths[name] = path
                record['artifacts'][name] = {'target': label, 'bytes': path.stat().st_size, 'sha256': sha(path)}
                record['commands'].append({'argv': ['fixed-proxy', 'cquery', label], 'returncode': 0})
            return paths

        def execute(argv, output, record, name, *, cwd, output_path):
            # Only the execution boundary is mocked: actual mapping/guards/export
            # calls in the production helper run over the complete174 allowlist.
            expression = argv[-1]
            labels = [x for x in self.labels if x in expression.split('set(', 1)[1].split(')', 1)[0].split()]
            checksum = str(groups[labels[0]] + 1) * 64
            if '--output=jsonproto' in argv:
                raw = json.dumps(self.metadata(labels, checksum))
            else:
                self.assertTrue(expression.endswith(', ' + checksum + ')'))
                self.assertNotIn('opaque-', expression)
                rows = [{'label': label, 'files': [filenames[label]]} for label in reversed(labels)]
                if problem == 'missing-row': rows.pop()
                raw = '\n'.join(json.dumps(x) for x in rows)
            output_path.write_text(raw)
            record['commands'].append({'argv': argv, 'returncode': 1 if problem == 'query-failure' else 0,
                                        'log': name + '.log'})
            if problem == 'query-failure':
                raise subprocess.CalledProcessError(1, argv)

        def export(source, destination):
            if problem == 'changed-copy' and destination.name.startswith('item-'):
                destination.write_bytes(b'changed')
            else:
                shutil.copyfile(source, destination)

        builder = SimpleNamespace(collect_archives=collect, export_archive=export,
            execution_root=lambda events: execution, command_log=SimpleNamespace(execute=execute))
        return directory, targets, receipt, calls, builder

    def test_one_original_build_and_real_group_queries_preserve_all_receipts_and_aliases(self):
        directory, targets, receipt, calls, builder = self.fixture()
        options = ['--config=syncd-dash', '--keep_going']
        layouts = ['//existing:image']
        paths = caller.collect_batched_component_exports(self.root, directory, receipt, targets,
            builder=builder, check=SimpleNamespace(sha=sha), bazel='fixed-proxy', options=options,
            additional_targets=layouts, execution_log=directory / 'execution.json')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], {'external.tar': '@existing//:runtime'})
        self.assertEqual(set(calls[0][1]['additional_targets']), set(self.labels + layouts))
        self.assertEqual(calls[0][1]['options'], options)
        self.assertEqual(set(paths), set(targets))
        self.assertEqual(receipt['artifacts']['native.gz'], {'original': True})
        self.assertEqual(receipt['archive_discovery']['group_count'], 2)
        self.assertEqual(receipt['archive_discovery']['query_count'], 4)
        self.assertEqual(len(receipt['commands']), 6)  # one build, one fallback, four actual group queries
        self.assertEqual(receipt['artifacts']['external.tar'], receipt['artifacts']['external-alias.tar'])
        for name, target in targets.items():
            self.assertEqual(receipt['artifacts'][name], {'target': target,
                'bytes': paths[name].stat().st_size, 'sha256': sha(paths[name])})
        for row in receipt['archive_discovery']['groups']:
            for query in row['queries']:
                original = directory / (query['name'] + '.json')
                self.assertEqual(query['sha256'], sha(original))
                self.assertEqual(query['bytes'], original.stat().st_size)
                self.assertEqual(original.stat().st_mode & 0o777, 0o600)
                self.assertEqual(receipt['commands'][query['command_index']]['log'], query['name'] + '.log')

    def test_missing_changed_or_ambiguous_outputs_preserve_prior_export_records(self):
        for problem in ('missing-file', 'empty-file', 'missing-row', 'changed-copy'):
            with self.subTest(problem=problem):
                directory, targets, receipt, calls, builder = self.fixture(problem)
                with self.assertRaises(ValueError):
                    caller.collect_batched_component_exports(self.root, directory, receipt, targets,
                        builder=builder, check=SimpleNamespace(sha=sha), bazel='fixed-proxy', options=[],
                        additional_targets=[], execution_log=directory / 'execution.json')
                self.assertEqual(receipt['artifacts']['native.gz'], {'original': True})
                self.assertEqual((directory / 'external.tar').read_bytes(), b'original external payload')
                self.assertNotIn('item-0.tar', receipt['artifacts'])
                shutil.rmtree(directory)
                shutil.rmtree(self.root / 'execroot')

    def test_failed_genuine_query_is_recorded_without_export_or_retry(self):
        directory, targets, receipt, calls, builder = self.fixture('query-failure')
        with self.assertRaises(subprocess.CalledProcessError):
            caller.collect_batched_component_exports(self.root, directory, receipt, targets,
                builder=builder, check=SimpleNamespace(sha=sha), bazel='fixed-proxy', options=[],
                additional_targets=[], execution_log=directory / 'execution.json')
        self.assertEqual(len(calls), 1)
        self.assertEqual(receipt['archive_discovery']['query_count'], 1)
        self.assertEqual(len(receipt['commands']), 3)
        self.assertEqual(receipt['commands'][-1]['returncode'], 1)
        self.assertNotIn('item-0.tar', receipt['artifacts'])
        self.assertEqual(receipt['artifacts']['native.gz'], {'original': True})

    def test_changed_scope_or_export_names_stop_before_build(self):
        for problem in ('missing-scope', 'invalid-name'):
            with self.subTest(problem=problem):
                directory, targets, receipt, calls, builder = self.fixture()
                if problem == 'missing-scope': targets.pop('item-0.tar')
                else: targets['../outside.tar'] = targets.pop('item-0.tar')
                with self.assertRaises(ValueError):
                    caller.collect_batched_component_exports(self.root, directory, receipt, targets,
                        builder=builder, check=SimpleNamespace(sha=sha), bazel='fixed-proxy', options=[],
                        additional_targets=[], execution_log=directory / 'execution.json')
                self.assertEqual(calls, [])
                shutil.rmtree(directory)
                shutil.rmtree(self.root / 'execroot')



class GnoiSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'source'
        self.source.mkdir()
        self.directory = Path(temporary.name) / 'private'
        self.directory.mkdir()
        self.relative = 'src/sonic-sysmgr/gnoi'
        self.submodule = self.source / self.relative
        self.revision = '2' * 40
        self.payloads = {name: ('pinned fixture ' + name).encode() for name in
                         ('types/types.proto', 'common/common.proto', 'system/system.proto')}
        self.events = []
        self.problem = None
        self.receipt = {'commands': []}
        self.logs = SimpleNamespace(execute=self.execute)

    def execute(self, argv, directory, receipt, name, *, cwd):
        self.assertEqual((directory, cwd), (self.directory, self.source))
        self.assertIs(receipt, self.receipt)
        self.events.append(name)
        receipt['commands'].append({'name': name, 'argv': argv})
        if name == 'gnoi.gitlink':
            mode = '100644 blob' if self.problem == 'not-gitlink' else '160000 commit'
            return mode + ' ' + self.revision + '\t' + self.relative + '\n'
        if name == 'gnoi.initialize':
            self.assertEqual(argv, ['git', '-c', 'safe.directory=' + str(self.source),
                                   'submodule', 'update', '--init', '--', self.relative])
            self.assertFalse(self.submodule.exists())
            for key, data in self.payloads.items():
                p = self.submodule / key
                p.parent.mkdir(parents=True, exist_ok=True)
                if not (self.problem == 'missing' and key == 'types/types.proto'):
                    p.write_bytes(data + (b'changed' if self.problem == 'changed' and
                                         key == 'types/types.proto' else b''))
            return ''
        if name == 'gnoi.revision':
            return ('3' * 40 if self.problem == 'wrong-head' else self.revision) + '\n'
        if name == 'gnoi.proto-tree':
            return ''.join('100644 blob ' + hashlib.sha1(b'blob ' + str(len(data)).encode() +
                           b'\0' + data).hexdigest() + '\t' + key + '\n'
                           for key, data in self.payloads.items())
        if name == 'gnoi.clean':
            self.assertIn('--no-ext-diff', argv)
            self.assertIn('--no-textconv', argv)
            if self.problem == 'dirty':
                raise subprocess.CalledProcessError(1, argv)
            return ''
        self.fail('Unexpected command boundary: ' + name)

    def run_setup(self):
        return caller.prepare_component_gnoi(self.source, self.directory, self.receipt, self.logs)

    def test_missing_checkout_is_initialized_at_its_actual_gitlink(self):
        self.assertFalse(self.submodule.exists())
        result = self.run_setup()
        self.assertEqual(result['revision'], self.revision)
        self.assertEqual(result['path'], self.relative)
        self.assertEqual(set(result['files']), set(self.payloads))
        self.assertEqual(self.events, ['gnoi.gitlink', 'gnoi.initialize', 'gnoi.revision',
                                       'gnoi.proto-tree', 'gnoi.clean'])
        for key, data in self.payloads.items():
            self.assertEqual(result['files'][key]['sha256'], hashlib.sha256(data).hexdigest())

    def test_non_gitlink_rejects_before_initialization(self):
        self.problem = 'not-gitlink'
        with self.assertRaisesRegex(ValueError, 'unique pinned gitlink'):
            self.run_setup()
        self.assertEqual(self.events, ['gnoi.gitlink'])

    def test_different_nested_head_rejects(self):
        self.problem = 'wrong-head'
        with self.assertRaisesRegex(ValueError, 'differs from its source gitlink'):
            self.run_setup()
        self.assertNotIn('gnoi.proto-tree', self.events)

    def test_missing_or_changed_proto_and_dirty_checkout_reject(self):
        for problem in ('missing', 'changed', 'dirty'):
            with self.subTest(problem=problem):
                if self.submodule.exists():
                    shutil.rmtree(self.submodule)
                self.problem = problem
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.run_setup()


class ComponentHeapTests(unittest.TestCase):
    def fixture(self, memory_gib=20):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, scratch = root / 'source', root / 'scratch'
        source.mkdir()
        scratch.mkdir()
        binary = root / 'qualified-bazel'
        binary.write_bytes(b'qualified installed launcher')
        profile = {'name': 'two_actions', 'status': 'pilot', 'minimum_memory_bytes': 20 * 1024**3,
                   'minimum_cpu': 4, 'measured_memory_limit_bytes': memory_gib * 1024**3,
                   'measured_cpu_limit': 6.0, 'jvm_heap_gib': 3, 'jobs': 2, 'cpu': 2,
                   'scheduler_memory_mb': 8000, 'loaded_container_memory_gib': 4}
        resources = {'architecture': 'amd64', 'memory_max': memory_gib * 1024**3,
                     'memory_current': 1024**3, 'cpu_limit': 6.0,
                     'owner_profile': copy.deepcopy(profile), 'selected_profile': copy.deepcopy(profile),
                     'scratch_free': 560 * 1024**3, 'disk_cache': False, 'repository_cache': False,
                     'source_inputs': [{'sha256': 'a' * 64, 'bytes': 17}]}
        configuration = {'provider': 'fixture_remote_only', 'endpoint': 'fixture-endpoint'}

        def render(executable, base, selected, cache):
            return json.dumps({'binary': executable, 'base': str(base),
                               'profile': selected, 'cache': cache}, sort_keys=True) + '\n'

        worker = SimpleNamespace(remote_cache_configuration=mock.Mock(return_value=configuration),
                                 launcher_text=mock.Mock(side_effect=render))
        preflight = SimpleNamespace(select_build_profile=mock.Mock(return_value=profile))
        plan = {'component_refresh': True,
                'component_heap_recovery': {'jvm_heap_gib': 6, 'scheduler_memory_mb': 4000},
                'bazel_launcher_sha256': sha(binary)}
        (scratch / 'resources.json').write_text(json.dumps(resources) + '\n')
        (scratch / 'native-bazel').write_text(render(str(binary), scratch / 'output-base', profile, configuration))
        return source, scratch, binary, profile, resources, worker, preflight, plan

    def invoke(self, fixture):
        source, scratch, binary, profile, resources, worker, preflight, plan = fixture
        with mock.patch.object(caller.shutil, 'which', return_value=str(binary)):
            caller.prepare_component_heap(plan, source, scratch, worker, preflight)

    def test_reallocation_preserves_owner_bytes_home_and_larger_worker_capacity(self):
        for memory_gib in (20, 24):
            with self.subTest(memory_gib=memory_gib):
                fixture = self.fixture(memory_gib)
                source, scratch, binary, profile, resources, worker, preflight, plan = fixture
                old_resources = (scratch / 'resources.json').read_bytes()
                old_launcher = (scratch / 'native-bazel').read_bytes()
                original_home = os.environ.get('HOME')
                self.invoke(fixture)
                actual = json.loads((scratch / 'resources.json').read_bytes())
                selected = dict(profile, jvm_heap_gib=6, scheduler_memory_mb=4000)
                self.assertEqual(actual['owner_profile'], profile)
                self.assertEqual(actual['selected_profile'], selected)
                self.assertEqual(selected['measured_memory_limit_bytes'], memory_gib * 1024**3)
                self.assertEqual((selected['jobs'], selected['cpu']), (2, 2))
                self.assertEqual((scratch / 'resources.original.json').read_bytes(), old_resources)
                self.assertEqual((scratch / 'native-bazel.original').read_bytes(), old_launcher)
                self.assertEqual(actual['component_heap_recovery']['original_resources_sha256'],
                                 hashlib.sha256(old_resources).hexdigest())
                self.assertEqual(actual['component_heap_recovery']['selected_launcher_sha256'],
                                 sha(scratch / 'native-bazel'))
                self.assertFalse(actual['component_heap_recovery']['measured_peak_guarantee'])
                self.assertEqual({key: value for key, value in actual.items()
                                  if key not in ('selected_profile', 'component_heap_recovery')},
                                 {key: value for key, value in resources.items() if key != 'selected_profile'})
                worker.remote_cache_configuration.assert_called_once_with(source)
                self.assertEqual(worker.launcher_text.call_count, 2)
                first, second = worker.launcher_text.call_args_list
                self.assertEqual(first.args[:2], second.args[:2])
                self.assertEqual(first.args[3], second.args[3])
                self.assertEqual(first.args[2], profile)
                self.assertEqual(second.args[2], selected)
                preflight.select_build_profile.assert_called_once_with(memory_gib * 1024**3, 6.0)
                self.assertEqual(os.environ.get('HOME'), original_home)
                self.assertEqual((scratch / 'native-bazel').stat().st_mode & 0o777, 0o700)
                self.assertEqual((scratch / 'resources.json').stat().st_mode & 0o777, 0o600)

    def test_different_allocation_or_scope_stops_without_cache_or_file_changes(self):
        for problem in ('heap', 'memory', 'extra', 'scope'):
            with self.subTest(problem=problem):
                fixture = self.fixture()
                source, scratch, binary, profile, resources, worker, preflight, plan = fixture
                if problem == 'heap': plan['component_heap_recovery']['jvm_heap_gib'] = 7
                elif problem == 'memory': plan['component_heap_recovery']['scheduler_memory_mb'] = 8000
                elif problem == 'extra': plan['component_heap_recovery']['jobs'] = 3
                else: plan['component_refresh'] = False
                before = {p.name: p.read_bytes() for p in scratch.iterdir()}
                with self.assertRaisesRegex(ValueError, 'Different fixed component heap recovery'):
                    self.invoke(fixture)
                self.assertEqual({p.name: p.read_bytes() for p in scratch.iterdir()}, before)
                worker.remote_cache_configuration.assert_not_called()
                worker.launcher_text.assert_not_called()

    def test_unadmitted_or_changed_original_profile_stops_before_rendering(self):
        for problem in ('small', 'owner', 'already-selected', 'local-cache'):
            with self.subTest(problem=problem):
                fixture = self.fixture(19 if problem == 'small' else 20)
                source, scratch, binary, profile, resources, worker, preflight, plan = fixture
                if problem == 'owner': resources['owner_profile']['cpu'] = 1
                elif problem == 'already-selected': resources['selected_profile']['jvm_heap_gib'] = 6
                elif problem == 'local-cache': resources['disk_cache'] = True
                (scratch / 'resources.json').write_text(json.dumps(resources) + '\n')
                before = {p.name: p.read_bytes() for p in scratch.iterdir()}
                with self.assertRaises(ValueError): self.invoke(fixture)
                self.assertEqual({p.name: p.read_bytes() for p in scratch.iterdir()}, before)
                worker.remote_cache_configuration.assert_not_called()
                worker.launcher_text.assert_not_called()

    def test_different_binary_or_complete_launcher_stops_before_writes(self):
        for problem in ('binary', 'launcher'):
            with self.subTest(problem=problem):
                fixture = self.fixture()
                source, scratch, binary, profile, resources, worker, preflight, plan = fixture
                if problem == 'binary': binary.write_bytes(b'changed installed launcher')
                else: (scratch / 'native-bazel').write_text('changed cache or output base\n')
                before = {p.name: p.read_bytes() for p in scratch.iterdir()}
                with self.assertRaises(ValueError): self.invoke(fixture)
                self.assertEqual({p.name: p.read_bytes() for p in scratch.iterdir()}, before)
                self.assertLessEqual(worker.launcher_text.call_count, 1)

    def test_existing_recovery_outputs_and_symlink_inputs_are_preserved(self):
        for problem in ('existing-backup', 'symlink-input'):
            with self.subTest(problem=problem):
                fixture = self.fixture()
                source, scratch, binary, profile, resources, worker, preflight, plan = fixture
                if problem == 'existing-backup':
                    (scratch / 'native-bazel.original').write_bytes(b'preserve prior evidence')
                else:
                    launcher = scratch / 'native-bazel'
                    moved = source / 'outside-launcher'
                    launcher.rename(moved)
                    launcher.symlink_to(moved)
                before = {p.name: p.read_bytes() for p in scratch.iterdir()}
                with self.assertRaises(ValueError): self.invoke(fixture)
                self.assertEqual({p.name: p.read_bytes() for p in scratch.iterdir()}, before)
                worker.remote_cache_configuration.assert_not_called()


if __name__ == '__main__':
    unittest.main()
