#!/usr/bin/env python3
"""Exercise the fixed cohort's ordering, existing-owner use and identity gates."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
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



if __name__ == '__main__':
    unittest.main()
