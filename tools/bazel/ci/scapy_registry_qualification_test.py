"""Boundary tests for the fixed Scapy qualification caller."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

path = Path(__file__).with_name('qualify_scapy_registry_module.py')
spec = importlib.util.spec_from_file_location('scapy_qualification', path)
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)

class ScapyQualificationTest(unittest.TestCase):
    def test_only_original_two_test_targets_are_forwarded(self):
        work = Path('/work')
        prefix = ['/launcher', '--ignore_all_rc_files', '--output_user_root=/work/bazel', '--batch']
        command = prefix + ['test', '--test_env=HOME=', *owner.TEST]
        self.assertEqual(owner.command_parts(command, Path('/launcher'), work)[3], owner.TEST)
        with self.assertRaises(ValueError):
            owner.command_parts(command + ['@scapy//:other'], Path('/launcher'), work)
        with self.assertRaises(ValueError):
            owner.command_parts(prefix + ['test', '--execution_log_json_file=/outside', *owner.TEST],
                                Path('/launcher'), work)

    def test_unpublished_registry_placeholder_is_not_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'plan.json'
            plan = {'module': owner.MODULE, 'version': owner.VERSION, 'architecture': 'amd64',
                    'build_targets': owner.BUILD, 'test_targets': owner.TEST,
                    'registry_revision': 'PENDING'}
            path.write_text(json.dumps(plan))
            with self.assertRaises(ValueError):
                owner.plan_data(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_failed_upstream_xml_is_retained_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            artifacts = scratch / 'private'
            path = artifacts / 'testlogs/external/scapy+/upstream_test/test.outputs/upstream-xunit.xml'
            path.parent.mkdir(parents=True)
            raw = b'<testsuite><error><![CDATA[original failure]]></error></testsuite>'
            path.write_bytes(raw)
            with mock.patch.object(owner, 'operator', return_value={'run': 'original'}):
                owner.retain_originals(scratch, artifacts, False)
            self.assertEqual((scratch / 'reports' / path.relative_to(artifacts)).read_bytes(), raw)
            result = json.loads((scratch / 'reports/originals.json').read_bytes())
            self.assertFalse(result['original_runner_completed'])
            self.assertTrue(result['retention_is_not_test_success'])
            self.assertFalse((scratch / 'reports/qualification.json').exists())

    def test_linked_test_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            artifacts = scratch / 'private'
            (artifacts / 'testlogs').mkdir(parents=True)
            original = scratch / 'outside'
            original.write_text('outside original')
            (artifacts / 'testlogs/test.log').symlink_to(original)
            with self.assertRaises(ValueError):
                owner.retain_originals(scratch, artifacts, False)
            self.assertEqual(original.read_text(), 'outside original')

    def test_original_test_failure_propagates_and_runner_hook_is_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            launcher = scratch / 'pass-1-bazel'
            launcher.write_text('original launcher')
            identity = {'bytes': launcher.stat().st_size, 'sha256': owner.sha(launcher.read_bytes())}
            setup = {'operator': {}, 'registry_revision': '1' * 40,
                     'home_sha256': owner.sha(os.environ['HOME'].encode()),
                     'launchers': {'pass-1': identity}, 'distdirs': []}
            (scratch / 'setup.json').write_text(json.dumps(setup))
            plan = {'source_revision': '2' * 40, 'registry_revision': '1' * 40,
                    'registry_files': {}, 'registry_runner': {}, 'action_audit': {}}
            config = {'architectures': ['amd64'], 'build_targets': owner.BUILD, 'test_targets': owner.TEST,
                      'consumer_deps': [], 'platforms': {'amd64': '@scapy//:linux_amd64'},
                      'host_platforms': {'amd64': '@scapy//:linux_amd64'}, 'generated_module_overlay': True}
            original = mock.Mock()
            original.side_effect = [None, RuntimeError('genuine test failure')]
            runner = SimpleNamespace(run_logged=original, sync_module=mock.Mock(),
                                     validate_entry=mock.Mock(return_value=config))
            def execute(*args, **kwargs):
                work, artifacts = args[4:6]
                artifacts.mkdir(parents=True)
                prefix = [str(launcher), '--ignore_all_rc_files', '--output_user_root=' + str(work / 'bazel'), '--batch']
                runner.run_logged(prefix + ['build', *owner.BUILD], work / 'consumer', artifacts / 'build.log')
                runner.run_logged(prefix + ['test', *owner.TEST], work / 'consumer', artifacts / 'test.log')
            runner.run = execute
            audit = SimpleNamespace(audit_targets=mock.Mock())
            support = SimpleNamespace(source_check=mock.Mock())
            with mock.patch.object(owner, 'load_owner', side_effect=[runner, audit]), mock.patch.object(owner, 'operator', return_value={}):
                with self.assertRaisesRegex(RuntimeError, 'genuine test failure'):
                    owner.run(plan, scratch / 'source', scratch / 'registry', scratch, support)
            self.assertIs(runner.run_logged, original)
            self.assertEqual(audit.audit_targets.call_count, 2)
            self.assertFalse((scratch / 'run-result.json').exists())
            self.assertFalse(json.loads((scratch / 'reports/originals.json').read_bytes())['original_runner_completed'])

    def test_verification_rejects_collector_success_without_test_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            (scratch / 'setup.json').write_text(json.dumps({'operator': {}, 'registry_revision': '1' * 40}))
            (scratch / 'run-result.json').write_text(json.dumps({'operator': {}, 'registry_revision': '1' * 40,
                         'status': 'COLLECTION_SUCCEEDED', 'passed_tests': []}))
            support = SimpleNamespace(source_check=mock.Mock())
            with mock.patch.object(owner, 'operator', return_value={}), self.assertRaises(ValueError):
                owner.verify({'native_revision': '2' * 40, 'native_files': {}, 'registry_revision': '1' * 40},
                             scratch / 'native', scratch, support)

if __name__ == '__main__':
    unittest.main(verbosity=2)

