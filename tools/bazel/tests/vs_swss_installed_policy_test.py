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


class SourceMirrorRevisionTests(_Fixture):
    def mocked_setup(self, actual_mirror_revision):
        source, native = self.root / 'source', self.root / 'native'
        source.mkdir()
        native.mkdir()
        mirrors = self.root / 'mirrors/ci/source_mirrors.cfg'
        mirrors.parent.mkdir(parents=True)
        mirrors.write_bytes(b'reviewed source mirrors')
        binary = self.root / 'bazel'
        binary.write_bytes(b'qualified launcher')
        binary.chmod(0o700)
        plan = copy.deepcopy(self.plan)
        plan['native_files'] = {}
        plan['bazel_launcher_sha256'] = caller.hashlib.sha256(binary.read_bytes()).hexdigest()
        worker = SimpleNamespace(
            prepare_source_downloads=mock.Mock(return_value=([], {'verified': True})),
            mirror_configuration=mock.Mock(return_value=b'reviewed downloader config'),
            prepare_remote_cache=mock.Mock(return_value={}),
            launcher_text=mock.Mock(return_value='#!/bin/sh\n'))
        preflight = SimpleNamespace(cpu_limit=mock.Mock(return_value=4.0),
                                    select_build_profile=mock.Mock(return_value={'jobs': 2}))
        checks = []
        def source_check(path, revision):
            checks.append((path, revision))
            if path == mirrors.parent.parent and revision != actual_mirror_revision:
                raise ValueError('Source revision differs')
        original_read = Path.read_text
        def read_text(path, *args, **kwargs):
            values = {'/sys/fs/cgroup/memory.max': str(24 * 1024**3),
                      '/sys/fs/cgroup/memory.current': '0',
                      '/sys/fs/cgroup/cpu.max': '400000 100000'}
            return values[str(path)] if str(path) in values else original_read(path, *args, **kwargs)
        original_stat = Path.stat
        def file_stat(path, *args, **kwargs):
            if path == binary:
                return SimpleNamespace(st_mode=0o100700, st_uid=0, st_size=18)
            return original_stat(path, *args, **kwargs)
        for patch in (
            mock.patch.object(caller, 'source_check', side_effect=source_check),
            mock.patch.object(caller, 'import_from', return_value=worker),
            mock.patch.object(caller.importlib, 'import_module', return_value=preflight),
            mock.patch.object(caller.platform, 'machine', return_value='x86_64'),
            mock.patch.object(caller.platform, 'freedesktop_os_release', return_value={'VERSION_CODENAME': 'trixie'}),
            mock.patch.object(caller.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024**4)),
            mock.patch.object(caller.shutil, 'which', return_value=str(binary)),
            mock.patch.object(Path, 'read_text', read_text),
            mock.patch.object(Path, 'stat', file_stat),
        ):
            self.enterContext(patch)
        return plan, source, native, mirrors, worker, checks

    def test_native_mirror_revision_is_independent_of_bazel_catalog(self):
        self.assertEqual(self.plan['source_mirror_revision'], '97cc502556b7ec3435d2d67b105315bcc18bb47e')
        self.assertEqual(self.plan['catalog_revision'], 'e6ef022c94dd2fdeeac364dfd591f9701cd37c29')
        plan, source, native, mirrors, worker, checks = self.mocked_setup(self.plan['source_mirror_revision'])
        caller.prepare_cache(plan, source, native, self.root, self.root / 'archives', mirrors)
        self.assertEqual(checks, [(source, plan['source_revision']), (native, plan['native_revision']),
                                  (mirrors.parent.parent, plan['source_mirror_revision'])])
        worker.mirror_configuration.assert_called_once_with(b'reviewed source mirrors')
        worker.prepare_remote_cache.assert_called_once()

    def test_catalog_checkout_is_rejected_before_download_setup_or_cache_access(self):
        plan, source, native, mirrors, worker, checks = self.mocked_setup(self.plan['catalog_revision'])
        with self.assertRaisesRegex(ValueError, 'Source revision differs'):
            caller.prepare_cache(plan, source, native, self.root, self.root / 'archives', mirrors)
        self.assertEqual(checks[-1], (mirrors.parent.parent, plan['source_mirror_revision']))
        worker.prepare_source_downloads.assert_not_called()
        worker.mirror_configuration.assert_not_called()
        worker.prepare_remote_cache.assert_not_called()


class ArchiveMismatchDiagnosticsTests(_Fixture):
    def test_all_mismatches_report_only_declared_names_and_exact_hashes(self):
        policy = copy.deepcopy(self.policy)
        for row in policy['images'][:2]:
            row['archive_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Policy does not match') as raised:
            caller.validate_policy(self.write_policy(policy), self.plan)
        prefix, payload = str(raised.exception).split(': ', 1)
        observed = json.loads(payload)
        expected = [{'name': row['name'],
                     'expected_sha256': self.plan['expected_archives'][row['name']]['sha256'],
                     'actual_sha256': '0' * 64} for row in policy['images'][:2]]
        self.assertEqual(observed, expected)
        self.assertLess(len(str(raised.exception)), 1024)

    def test_malformed_hash_text_is_rejected_without_reflection(self):
        for value in ['arbitrary-private-value\nnot-a-hash', 'F' * 64, None, {'secret': 'value'}]:
            policy = copy.deepcopy(self.policy)
            policy['images'][0]['archive_sha256'] = value
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                caller.validate_policy(self.write_policy(policy), self.plan)
            self.assertEqual(str(raised.exception), 'Invalid policy archive hash')


if __name__ == '__main__':
    unittest.main()
