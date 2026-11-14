"""Fixed adapter boundaries; no Bazel, network, ELF execution or readelf child."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from tools.bazel.ci import observe_declared_bitarray_wheels as subject
from tools.bazel.ci import recover_vs_original_inputs as original
from tools.bazel.ci import qualify_bitarray_registry_module as qualifier


class DeclaredWheelBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.sections = [{'name': '.text', 'bytes': 8, 'category': 'allocated'},
                         {'name': '.debug_info', 'bytes': 8, 'category': 'debug'},
                         {'name': '.debug_line', 'bytes': 8, 'category': 'debug'}]
        self.abi = {'Class': 'ELF64', 'Data': "2's complement, little endian",
                    'Type': 'DYN (Shared object file)', 'Machine': 'Advanced Micro Devices X86-64',
                    'needed': ['libc.so.6'], 'interpreter': [], 'rpath': []}
        def exports(content, scratch):
            content.seek(0); raw = content.read()
            return ['PyInit__bitarray' if raw[32] == 1 else 'PyInit__util']
        self.elf = SimpleNamespace(
            run_upstream=mock.Mock(side_effect=lambda *args: io.BytesIO(b'fixed listing')),
            sections_with_flags=mock.Mock(side_effect=lambda *args: copy.deepcopy(self.sections)),
            elf_metadata=mock.Mock(side_effect=lambda *args: copy.deepcopy(self.abi)),
            pyinit_exports=mock.Mock(side_effect=exports),
            string_classifications=mock.Mock(return_value={'paths': [], 'compilers': []}))

    def binary(self, marker):
        raw = bytearray(64); raw[:6] = b'\x7fELF\x02\x01'
        raw[16:20] = b'\x03\x00\x3e\x00'; raw[32] = marker
        return bytes(raw)

    def wheel(self, names=None, mode=stat.S_IFREG | 0o644):
        path = self.root / 'wheel.whl'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            for index, name in enumerate(names if names is not None else subject.MEMBERS):
                item = zipfile.ZipInfo(name); item.external_attr = mode << 16
                archive.writestr(item, self.binary(1 if '_bitarray.' in name else 2))
        return path, self.identity(path)

    def identity(self, path):
        raw = path.read_bytes()
        return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def observe(self, path, identity, debug=False):
        return subject.observe_wheel(path, identity, self.root, original, self.elf, object(), debug=debug)

    def baseline(self):
        operator = {'GITHUB_RUN_ID': '17', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40}
        wheel = {'bytes': 100, 'sha256': 'b' * 64}
        plan = {'registry_revision': 'c' * 40, 'source_revision': 'd' * 40,
                'native_revision': 'e' * 40, 'source_archive_sha256': 'f' * 64}
        report = {'status': 'FIXED_BITARRAY_AMD64_QUALIFICATION_PASSED', 'module': 'bitarray',
                  'version': '2.8.1.sonic.1', 'architecture': 'amd64', 'operator': operator,
                  'registry_revision': plan['registry_revision'], 'setup_source_revision': plan['source_revision'],
                  'native_setup_owner': plan['native_revision'], 'source_archive_sha256': plan['source_archive_sha256'],
                  'full_original_suites': 2, 'declared_header_edit_restored': True,
                  'disk_cache': False, 'repository_cache': False, 'HOME_unchanged': True,
                  'outputs': {p: {'@bitarray//:whl': wheel} for p in ('pass1', 'pass2', 'copy-baseline', 'restored')}}
        path = self.root / 'qualification.json'; path.write_text(json.dumps(report))
        rows, apis = {}, {}
        for index, kind in enumerate(('reports', 'outputs')):
            rows[kind] = {'run_id': 17, 'attempt': 1, 'head': 'a' * 40, 'upload_step': 'Upload ' + kind}
            apis[kind] = {'artifact': {'id': index + 1}, 'run': {'conclusion': 'success'},
                          'job': {'conclusion': 'success', 'steps': [
                              {'name': subject.VERIFY_STEP, 'number': 5, 'status': 'completed', 'conclusion': 'success'},
                              {'name': rows[kind]['upload_step'], 'number': 6, 'status': 'completed', 'conclusion': 'success'}]}}
        plan['baseline'] = {'transfers': rows, 'operator': operator, 'wheel': wheel,
                            'report': self.identity(path)}
        return plan, apis, path, SimpleNamespace(check_producer=mock.Mock())

    def test_pending_baseline_stops_before_owner_or_cache(self):
        producer = SimpleNamespace(check_producer=mock.Mock())
        with self.assertRaisesRegex(ValueError, 'pending'):
            subject.baseline_proof({'baseline': None}, {}, self.root / 'missing', producer, original)
        producer.check_producer.assert_not_called()
        support = SimpleNamespace(prepare_cache=mock.Mock())
        with self.assertRaisesRegex(ValueError, 'absent'):
            subject.prepare_debug({'baseline': None}, {}, self.root, self.root, self.root,
                                  self.root, self.root, self.root, support, qualifier)
        support.prepare_cache.assert_not_called()

    def test_successful_original_report_delegates_both_transport_checks(self):
        plan, apis, path, producer = self.baseline()
        result = subject.baseline_proof(plan, apis, path, producer, original)
        self.assertEqual(result['wheel'], plan['baseline']['wheel'])
        self.assertEqual(producer.check_producer.call_count, 2)
        for name in ('reports', 'outputs'):
            producer.check_producer.assert_any_call(plan['baseline']['transfers'][name],
                                                   apis[name]['artifact'], apis[name]['run'], apis[name]['job'])

    def test_failed_baseline_and_verify_after_upload_are_rejected(self):
        for change in ('failed', 'after-upload'):
            plan, apis, path, producer = self.baseline()
            if change == 'failed':
                apis['reports']['run']['conclusion'] = 'failure'
            else:
                apis['outputs']['job']['steps'][0]['number'] = 7
            with self.subTest(change=change), self.assertRaises(ValueError):
                subject.baseline_proof(plan, apis, path, producer, original)

    def test_changed_report_wheel_and_original_source_are_rejected(self):
        for field in ('wheel', 'source'):
            plan, apis, path, producer = self.baseline()
            report = json.loads(path.read_bytes())
            if field == 'wheel':
                report['outputs']['restored']['@bitarray//:whl']['sha256'] = '0' * 64
            else:
                report['setup_source_revision'] = '0' * 40
            path.write_text(json.dumps(report)); plan['baseline']['report'] = self.identity(path)
            with self.subTest(field=field), self.assertRaises(ValueError):
                subject.baseline_proof(plan, apis, path, producer, original)

    def test_two_original_members_produce_metadata_without_binary_contents(self):
        path, identity = self.wheel()
        result = self.observe(path, identity, debug=True)
        self.assertEqual({r['name'] for r in result['extensions']}, set(subject.MEMBERS))
        self.assertFalse(result['binary_contents_retained'])
        self.assertFalse(result['source_image_admission'])
        self.assertEqual(self.elf.run_upstream.call_count, 2)
        json.dumps(result)
        for record in result['extensions']:
            marker = 1 if '_bitarray.' in record['name'] else 2
            self.assertEqual(record['sha256'], hashlib.sha256(self.binary(marker)).hexdigest())

    def test_wrong_whole_wheel_hash_stops_before_elf_reader(self):
        path, identity = self.wheel(); identity['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'wheel differs'):
            self.observe(path, identity)
        self.elf.run_upstream.assert_not_called()

    def test_missing_duplicate_and_nonregular_member_are_rejected(self):
        names = list(subject.MEMBERS)
        for selected, mode in ((names[:1], stat.S_IFREG | 0o644),
                               (names + names[:1], stat.S_IFREG | 0o644),
                               (names, stat.S_IFLNK | 0o777)):
            with self.subTest(names=selected, mode=mode):
                path, identity = self.wheel(selected, mode)
                with self.assertRaises(ValueError):
                    self.observe(path, identity)
        self.elf.run_upstream.assert_not_called()

    def test_actual_zip_crc_failure_is_preserved(self):
        path, _ = self.wheel()
        with zipfile.ZipFile(path) as archive:
            first = archive.infolist()[0]
        raw = bytearray(path.read_bytes())
        raw[first.header_offset + 30 + len(first.filename.encode()) + 40] ^= 1
        path.write_bytes(raw)
        with self.assertRaises(zipfile.BadZipFile):
            self.observe(path, self.identity(path))
        self.elf.run_upstream.assert_not_called()

    def test_selected_binary_bound_and_machine_are_enforced(self):
        path, identity = self.wheel()
        with mock.patch.object(subject, 'MAX_ELF', 32), self.assertRaises(ValueError):
            self.observe(path, identity)
        with mock.patch.object(self, 'binary', return_value=b'not an ELF'):
            path, identity = self.wheel()
        with self.assertRaisesRegex(ValueError, 'not AMD64'):
            self.observe(path, identity)
        self.elf.run_upstream.assert_not_called()

    def test_wrong_abi_or_pyinit_and_missing_debug_sections_are_rejected(self):
        name = next(iter(subject.MEMBERS))
        correct = {**self.abi, 'defined_global_pyinit_exports': [subject.MEMBERS[name]]}
        for key, bad in (('needed', ['libc.so.6', 'libstdc++.so.6']), ('rpath', ['/unexpected']),
                         ('defined_global_pyinit_exports', ['PyInit_wrong'])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                subject.abi_checks(name, {**correct, key: bad}, self.sections, False)
        subject.abi_checks(name, correct, self.sections[:1], False)
        for sections in (self.sections[:1], self.sections[:2]):
            with self.subTest(sections=sections), self.assertRaisesRegex(ValueError, 'Debug information'):
                subject.abi_checks(name, correct, sections, True)

    def debug_case(self, fail=False):
        launcher = self.root / 'debug-bazel'; launcher.write_bytes(b'prepared launcher')
        proof = {'report_sha256': 'a' * 64}
        plan = {'baseline': {'report': {'sha256': 'a' * 64}}, 'registry_files': {},
                'registry_revision': 'b' * 40, 'source_revision': 'c' * 40}
        setup = {'baseline_report_sha256': 'a' * 64, 'launcher': self.identity(launcher),
                 'home_sha256': hashlib.sha256(os.environ['HOME'].encode()).hexdigest(),
                 'distdirs': [str(self.root / str(i)) for i in range(5)]}
        (self.root / 'debug-setup.json').write_text(json.dumps(setup))
        original_logged = mock.Mock()
        runner = SimpleNamespace(run_logged=original_logged)
        def run(registry, module, version, arch, work, artifacts, **options):
            if fail:
                raise RuntimeError('original build failed')
            artifacts.mkdir(parents=True)
            prefix = [options['bazel'], '--ignore_all_rc_files', '--output_user_root=' + str(work / 'bazel'), '--batch']
            for kind, targets in (('build', qualifier.BUILD), ('test', qualifier.TEST)):
                runner.run_logged([*prefix, kind, *options['bazel_build_args'], *targets],
                                  work / 'consumer', artifacts / (kind + '.log'))
            (artifacts / 'outputs.json').write_text(json.dumps({'@bitarray//:whl': [
                {'path': 'wheel.whl', 'size': 10, 'sha256': 'd' * 64}]}))
        runner.run = mock.Mock(side_effect=run)
        return plan, proof, runner, original_logged

    def test_debug_delegates_same_targets_audit_and_only_explicit_dbg_flag(self):
        home = os.environ.get('HOME')
        plan, proof, runner, original_logged = self.debug_case()
        audit = SimpleNamespace(audit_targets=mock.Mock())
        path, identity = subject.run_debug(plan, proof, self.root, self.root, self.root,
                                          SimpleNamespace(source_check=mock.Mock()), qualifier, runner, audit)
        self.assertEqual(path, self.root / 'debug/private/wheel.whl')
        self.assertEqual(identity, {'bytes': 10, 'sha256': 'd' * 64})
        self.assertIs(runner.run_logged, original_logged)
        self.assertEqual(audit.audit_targets.call_count, 2)
        for call in original_logged.call_args_list:
            command = call.args[0]
            self.assertEqual(command.count('--compilation_mode=dbg'), 1)
            self.assertIn('--repository_cache=', command); self.assertIn('--disk_cache=', command)
        self.assertEqual(os.environ.get('HOME'), home)

    def test_original_failure_restores_runner_and_produces_no_success(self):
        plan, proof, runner, original_logged = self.debug_case(fail=True)
        with self.assertRaisesRegex(RuntimeError, 'original build failed'):
            subject.run_debug(plan, proof, self.root, self.root, self.root,
                              SimpleNamespace(source_check=mock.Mock()), qualifier, runner,
                              SimpleNamespace(audit_targets=mock.Mock()))
        self.assertIs(runner.run_logged, original_logged)
        self.assertFalse((self.root / 'debug/private/outputs.json').exists())


if __name__ == '__main__':
    unittest.main()
