#!/usr/bin/env python3
"""Focused scope, audit ordering and original-archive binding checks."""
import copy
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
MODULE = ROOT / 'tools/bazel/ci/build_vs_swss_installed_policy.py'
spec = importlib.util.spec_from_file_location('installed_policy_caller', MODULE)
caller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caller)
PLAN = ROOT / 'tools/bazel/ci/vs_swss_installed_policy_inputs.json'


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = caller.load_plan(PLAN)
        self.enterContext(mock.patch.dict(os.environ, {
            'BAZEL_REAL': str(self.root / 'native-bazel'),
            'SONIC_BAZEL_REGISTRY_REVISION': self.plan['catalog_revision'],
            'USE_BAZEL_VERSION': '8.5.1', 'BAZELISK_HOME': str(self.root / 'bazelisk'),
            'SOURCE_INPUTS_RUN': str(self.plan['source_inputs']['run_id']),
            'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'temporary'),
        }))
        self.policy = {'architecture': 'amd64', 'symbol_pairs': [], 'images': [
            {'name': name, 'archive': name + '.gz', 'tag': name + ':latest',
             'archive_sha256': item['sha256']} for name, item in self.plan['expected_archives'].items()]}

    def write_policy(self, value):
        path = self.root / 'policy.json'
        path.write_text(json.dumps(value))
        return path

class PolicyBindingTests(_Fixture):
    def test_matching_original_archive_bindings_pass(self):
        identity = caller.validate_policy(self.write_policy(self.policy), self.plan)
        self.assertEqual(identity['bytes'], (self.root / 'policy.json').stat().st_size)
        self.assertEqual(len(identity['sha256']), 64)

    def test_different_built_archive_is_not_silently_substituted(self):
        self.policy['images'][1]['archive_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'frozen original archive'):
            caller.validate_policy(self.write_policy(self.policy), self.plan)

    def test_missing_duplicate_or_wrong_architecture_is_rejected(self):
        variants = [copy.deepcopy(self.policy) for _ in range(3)]
        variants[0]['images'].pop()
        variants[1]['images'][1] = variants[1]['images'][0]
        variants[2]['architecture'] = 'arm64'
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                caller.validate_policy(self.write_policy(value), self.plan)

    def test_retagged_or_oversized_policy_is_rejected(self):
        self.policy['images'][0]['tag'] = 'replacement:latest'
        with self.assertRaisesRegex(ValueError, 'tag differs'):
            caller.validate_policy(self.write_policy(self.policy), self.plan)
        path = self.root / 'large.json'
        # Exercise the same pre-read size guard within the bounded fixture's file limit.
        with mock.patch.object(caller, 'MAX_POLICY', 128):
            path.write_bytes(b'x' * 129)
            with self.assertRaisesRegex(ValueError, 'outside bounds'):
                caller.validate_policy(path, self.plan)

    def test_different_target_or_archive_set_is_rejected_before_setup(self):
        for key, value in [('target', '//unreviewed:target'), ('expected_archives', {})]:
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            path = self.root / 'plan.json'
            path.write_text(json.dumps(plan))
            with self.subTest(key=key), self.assertRaises(ValueError):
                caller.load_plan(path)


class AuditBeforeBuildTests(_Fixture):
    def owners(self, events, *, audit_error=False, mismatch=False):
        def execute(*args, **kwargs):
            events.append('manifests')
        def audit(*args, **kwargs):
            events.append('audit')
            self.assertEqual(args[2], [caller.TARGET])
            if audit_error:
                raise ValueError('DEB action')
            return {'targets': [caller.TARGET], 'deb_outputs': [], 'packaging_wrappers': []}
        def collect(source, directory, receipt, targets, **kwargs):
            events.append('build')
            self.assertEqual(targets, {caller.POLICY_NAME: caller.TARGET})
            value = copy.deepcopy(self.policy)
            if mismatch:
                value['images'][0]['archive_sha256'] = '0' * 64
            path = directory / caller.POLICY_NAME
            path.write_text(json.dumps(value))
            return {caller.POLICY_NAME: path}
        def export(source, destination):
            events.append('export')
            shutil.copyfile(source, destination)
        return (SimpleNamespace(OPTIONS=['--platforms=reviewed'], identity=mock.Mock(return_value=self.plan['source_revision'])),
                SimpleNamespace(audit_targets=audit), SimpleNamespace(collect_archives=collect),
                SimpleNamespace(execute=execute), export)

    def test_ambient_bazel_cannot_bypass_the_prepared_remote_launcher(self):
        events = []
        with mock.patch.dict(os.environ, {'BAZEL_REAL': 'bazel'}), self.assertRaisesRegex(ValueError, 'prepared remote-only'):
            caller.build_policy(self.plan, self.root / 'source', self.root, self.root / 'published', self.owners(events))
        self.assertEqual(events, [])

    def test_audit_failure_stops_before_any_build_or_export(self):
        events = []
        with self.assertRaisesRegex(ValueError, 'DEB action'):
            caller.build_policy(self.plan, self.root / 'source', self.root, self.root / 'published',
                                self.owners(events, audit_error=True))
        self.assertEqual(events, ['manifests', 'audit'])
        self.assertFalse((self.root / 'published').exists())

    def test_built_policy_mismatch_stops_before_publication(self):
        events = []
        with self.assertRaises(ValueError):
            caller.build_policy(self.plan, self.root / 'source', self.root, self.root / 'published',
                                self.owners(events, mismatch=True))
        self.assertEqual(events, ['manifests', 'audit', 'build'])
        self.assertFalse((self.root / 'published').exists())

    def test_owner_audit_precedes_build_and_matching_policy_is_retained(self):
        events = []
        owners = self.owners(events)
        result = caller.build_policy(self.plan, self.root / 'source', self.root, self.root / 'published', owners)
        self.assertEqual(events, ['manifests', 'audit', 'build', 'export'])
        self.assertEqual(owners[0].identity.call_count, 2)
        self.assertEqual(result['status'], 'POLICY_MATCHES_FROZEN_ORIGINAL_ARCHIVES')
        self.assertFalse(result['native_acceptance'])
        self.assertFalse(result['VS_acceptance'])
        self.assertEqual(result['original_archives'], self.plan['expected_archives'])


if __name__ == '__main__':
    unittest.main()
