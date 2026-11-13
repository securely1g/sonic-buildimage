"""Small original-byte reader fixtures; no package construction or tool execution."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import observe_final_bitarray_elf as subject
from tools.bazel.ci import recover_vs_original_inputs as original
from tools.bazel.ci import observe_original_socat as elf_owner
from tools.bazel.ci import observe_vs_original_installed_policy as archive_library
from tools.bazel.ci import observe_vs_original_installed_policy_test as archive_support


LISTING = b'''  [ 1] .text PROGBITS 0000000000000040 000040 000020 00 AX 0 0 16
  [ 2] .rodata PROGBITS 0000000000000060 000060 000050 00 A 0 0 8
  [ 3] .comment PROGBITS 0000000000000000 0000b0 000030 01 MS 0 0 1
  [ 4] .debug_info PROGBITS 0000000000000000 0000e0 000010 00 0 0 1
'''
ABI = b'''  Class: ELF64
  Data: 2's complement, little endian
  Type: DYN
  Machine: Advanced Micro Devices X86-64
  OS/ABI: UNIX - System V
 0x1 (NEEDED) Shared library: [libc.so.6]
 0x1d (RUNPATH) Library runpath: [/tmp/private-build-path]
Version needs section '.gnu.version_r':
 Name: GLIBC_2.36 Flags: none
'''
SYMBOLS = b'  1: 0000000000000040 32 FUNC GLOBAL DEFAULT 1 PyInit__bitarray\n'


def elf_bytes(tag=b'a'):
    data = bytearray(256)
    data[:6] = b'\x7fELF\x02\x01'
    data[16:20] = b'\x03\x00\x3e\x00'
    data[64:96] = b'\x90' * 32
    path = b'/tmp/pip-build-' + tag + b'/bitarray/module.c'
    data[96:96+len(path)] = path
    compiler = b'GCC: (Debian 12.2.0) 12.2.0'
    data[176:176+len(compiler)] = compiler
    return bytes(data)


class FinalBitarrayElfTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def originals(self):
        body = elf_bytes()
        layer = bytearray(1024)
        layer[32:288] = body
        layer[320:576] = body
        path = self.root/'layer.tar'
        path.write_bytes(layer)
        row = {'layer_bytes': len(layer), 'expected_diff_id': 'sha256:'+hashlib.sha256(layer).hexdigest(),
               'binaries': [{'name': name, 'offset': offset, 'bytes': len(body),
                             'sha256': hashlib.sha256(body).hexdigest(), 'order': order}
                            for order, (name, offset) in enumerate((('one.so', 32), ('two.so', 320)))]}
        return SimpleNamespace(layers=[None]*11+[path]), row

    def upstream(self, _program, arguments, _source, _scratch, _maximum):
        return io.BytesIO(LISTING if '--section-headers' in arguments else
                          SYMBOLS if '--dyn-syms' in arguments else ABI)

    def record(self, body):
        sections = subject.sections_with_flags(LISTING, body, elf_owner)
        return {'name': 'one.so', 'sha256': hashlib.sha256(body).hexdigest(), 'sections': sections,
                'classifications': subject.string_classifications(body, sections)}

    def test_fixed_offsets_copy_only_hash_verified_originals(self):
        view, row = self.originals()
        before = view.layers[11].read_bytes()
        with mock.patch.object(subject, 'run_upstream', side_effect=self.upstream) as reader:
            result = subject.inspect_elf_ranges(view, row, self.root, original, elf_owner)
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(reader.call_count, 6)
        self.assertTrue(all(call.args[0] == '/usr/bin/readelf' for call in reader.call_args_list))
        self.assertEqual(result['records'][0]['abi']['needed'], ['libc.so.6'])
        self.assertEqual(result['records'][0]['abi']['defined_global_pyinit_exports'], ['PyInit__bitarray'])
        self.assertNotIn('/tmp/private-build-path', json.dumps(result['records']))
        self.assertEqual(view.layers[11].read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [view.layers[11]])

    def test_wrong_whole_layer_hash_stops_before_reader(self):
        view, row = self.originals()
        row['expected_diff_id'] = 'sha256:'+'0'*64
        with mock.patch.object(subject, 'run_upstream') as reader, self.assertRaisesRegex(ValueError, 'layer differs'):
            subject.inspect_elf_ranges(view, row, self.root, original, elf_owner)
        reader.assert_not_called()

    def test_wrong_original_elf_hash_stops_before_reader(self):
        view, row = self.originals()
        row['binaries'][0]['sha256'] = '0'*64
        with mock.patch.object(subject, 'run_upstream') as reader, self.assertRaisesRegex(ValueError, 'ELF hash differs'):
            subject.inspect_elf_ranges(view, row, self.root, original, elf_owner)
        reader.assert_not_called()

    def test_range_outside_exact_layer_is_rejected(self):
        view, row = self.originals()
        row['binaries'][0]['offset'] = row['layer_bytes']
        with mock.patch.object(subject, 'run_upstream') as reader, self.assertRaisesRegex(ValueError, 'range exceeds'):
            subject.inspect_elf_ranges(view, row, self.root, original, elf_owner)
        reader.assert_not_called()

    def test_layer_mutation_is_detected_after_readers(self):
        view, row = self.originals()
        def mutate(*args):
            with view.layers[11].open('r+b') as stream:
                stream.seek(1000)
                stream.write(b'x')
            return self.upstream(*args)
        with mock.patch.object(subject, 'run_upstream', side_effect=mutate), self.assertRaisesRegex(ValueError, 'identity or metadata'):
            subject.inspect_elf_ranges(view, row, self.root, original, elf_owner)

    def test_reader_failure_is_terminal(self):
        view, row = self.originals()
        with mock.patch.object(subject, 'run_upstream', side_effect=ValueError('upstream failure')) as reader:
            with self.assertRaisesRegex(ValueError, 'upstream failure'):
                subject.inspect_elf_ranges(view, row, self.root, original, elf_owner)
        self.assertEqual(reader.call_count, 1)

    def test_allocated_debug_and_other_categories_use_actual_flags(self):
        rows = subject.sections_with_flags(LISTING, elf_bytes(), elf_owner)
        self.assertEqual([r['category'] for r in rows], ['allocated', 'allocated', 'other', 'debug'])
        self.assertEqual([r['flags'] for r in rows], ['AX', 'A', 'MS', ''])

    def test_malformed_upstream_flags_are_rejected(self):
        bad = LISTING.replace(b'00 AX 0 0 16', b'00 A? 0 0 16')
        with self.assertRaisesRegex(ValueError, 'flags'):
            subject.sections_with_flags(bad, elf_bytes(), elf_owner)

    def test_compiler_and_paths_are_bounded_metadata(self):
        record = self.record(elf_bytes())
        observed = record['classifications']
        self.assertEqual(observed['compilers'][0]['kind'], 'gcc')
        self.assertEqual(observed['compilers'][0]['version_tokens'], ['12.2.0', '12.2.0'])
        self.assertIn('temporary_build_path', observed['paths'][0]['classes'])
        self.assertNotIn('/tmp/pip-build-a', json.dumps(observed))
        with self.assertRaisesRegex(ValueError, 'path classification exceeds'):
            subject.string_classifications(b'/tmp/a.c\x00'*65, [])

    def test_difference_projection_never_retains_binary_context(self):
        a, b = elf_bytes(b'a'), elf_bytes(b'b')
        result = subject.compare_pair(self.record(a), self.record(b), a, b, elf_owner)
        self.assertEqual(result['changed_section_counts'], {'allocated': 1, 'debug': 0, 'other': 0})
        self.assertTrue(result['reported_ranges'][0]['within_classified_path_string_both'])
        self.assertNotIn('left_hex', json.dumps(result))
        self.assertNotIn('left_ascii', json.dumps(result))
        self.assertFalse(result['byte_equivalence_claimed'])

    def test_only_defined_global_pyinit_functions_are_reported(self):
        data = SYMBOLS + b' 2: 00000000 0 FUNC GLOBAL DEFAULT UND PyInit_missing\n 3: 00000040 32 FUNC LOCAL DEFAULT 1 PyInit_local\n'
        with mock.patch.object(subject, 'run_upstream', return_value=io.BytesIO(data)):
            self.assertEqual(subject.pyinit_exports(io.BytesIO(), self.root), ['PyInit__bitarray'])

    def test_pyinit_export_count_is_bounded(self):
        data = b''.join(f' {i}: 00000040 32 FUNC GLOBAL DEFAULT 1 PyInit_x{i}\n'.encode() for i in range(33))
        with mock.patch.object(subject, 'run_upstream', return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(ValueError, 'export report exceeds'):
                subject.pyinit_exports(io.BytesIO(), self.root)

    def test_actual_home_is_preserved_and_credentials_are_filtered(self):
        state = mock.Mock(si_status=0, si_code=os.CLD_EXITED)
        process = mock.Mock(pid=7654321)
        environment = {'HOME': '/original/home', 'GH_TOKEN': 'not-for-reader', 'RUNNER_TRACKING_ID': 'owned'}
        with mock.patch.dict(os.environ, environment, clear=True), tempfile.TemporaryFile(dir=self.root) as source, \
                mock.patch.object(subject.subprocess, 'Popen', return_value=process) as launch, \
                mock.patch.object(subject.os, 'waitid', return_value=state), mock.patch.object(subject.os, 'killpg'):
            subject.run_upstream('/usr/bin/readelf', ['--section-headers'], source, self.root, subject.MIB).close()
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual(launch.call_args.kwargs['env'], {'HOME': '/original/home', 'PATH': '/usr/bin:/bin',
                                                        'LC_ALL': 'C', 'RUNNER_TRACKING_ID': 'owned'})

    def test_absent_home_stays_absent(self):
        state = mock.Mock(si_status=0, si_code=os.CLD_EXITED)
        process = mock.Mock(pid=7654321)
        with mock.patch.dict(os.environ, {}, clear=True), tempfile.TemporaryFile(dir=self.root) as source, \
                mock.patch.object(subject.subprocess, 'Popen', return_value=process) as launch, \
                mock.patch.object(subject.os, 'waitid', return_value=state), mock.patch.object(subject.os, 'killpg'):
            subject.run_upstream('/usr/bin/readelf', ['--section-headers'], source, self.root, subject.MIB).close()
        self.assertNotIn('HOME', launch.call_args.kwargs['env'])

    def test_changed_control_stops_before_any_owner_import(self):
        (self.root/next(iter(subject.MODULES))).write_bytes(b'changed')
        with mock.patch.object(subject, 'operator_identity', return_value={'fixture': True}), \
                mock.patch.object(subject.importlib, 'import_module') as imports:
            with self.assertRaises(RuntimeError):
                subject.run(self.root, self.root, self.root, self.root, self.root/'report')
        imports.assert_not_called()
        report = json.loads((self.root/'report/bitarray-elf-comparison.json').read_text())
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertFalse(report['source_image_installed_policy_admission'])

    def test_fixed_plan_has_only_four_authenticated_ranges(self):
        path = Path(subject.__file__).with_name('vs_final_bitarray_elf_inputs.json')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), subject.PLAN_SHA256)
        plan = json.loads(path.read_text())
        self.assertEqual([r['family'] for r in plan['images']], ['syncd', 'gearbox'])
        self.assertEqual(sum(len(r['binaries']) for r in plan['images']), 4)
        self.assertEqual(plan['prior_audit']['artifact_id'], 14075044534)
        self.assertFalse(plan['source_image_installed_policy_admission'])

    def test_timeout_closes_unreaped_group_before_wait(self):
        process = mock.Mock(pid=7654321); process.wait.return_value = -9
        order = []
        process.wait.side_effect = lambda **_: order.append("wait") or -9
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "killpg", side_effect=lambda *_: order.append("kill")), mock.patch.object(subject.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaises(subprocess.TimeoutExpired):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        self.assertEqual(order, ["kill", "wait"])

    def test_waitid_success_closes_descendants_before_reaping(self):
        process = mock.Mock(pid=7654321)
        order = []
        process.wait.side_effect = lambda **_: order.append("wait") or 0
        state = mock.Mock(si_status=0, si_code=os.CLD_EXITED)
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", side_effect=lambda *_: order.append("observe") or state) as observe, mock.patch.object(subject.os, "killpg", side_effect=lambda *_: order.append("kill")):
            output = subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
            output.close()
        self.assertEqual(order, ["observe", "kill", "wait"])
        self.assertEqual(observe.call_args.args, (os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))

    def test_nonzero_exit_also_closes_descendants_before_reaping(self):
        process = mock.Mock(pid=7654321); process.wait.return_value = 4
        order = []
        process.wait.side_effect = lambda **_: order.append("wait") or 4
        state = mock.Mock(si_status=4, si_code=os.CLD_EXITED)
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", return_value=state), mock.patch.object(subject.os, "killpg", side_effect=lambda *_: order.append("kill")):
            with self.assertRaises(ValueError):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        self.assertEqual(order, ["kill", "wait"])

    def test_external_reaper_prevents_unsafe_numeric_signal(self):
        process = mock.Mock(pid=7654321)
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", side_effect=ChildProcessError), mock.patch.object(subject.os, "killpg") as kill:
            with self.assertRaises(ChildProcessError):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        kill.assert_not_called(); process.wait.assert_not_called()

    def test_interruption_closes_the_still_owned_group(self):
        process = mock.Mock(pid=7654321); process.wait.return_value = -9
        with tempfile.TemporaryFile(dir=self.root) as source, mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject.os, "waitid", side_effect=KeyboardInterrupt), mock.patch.object(subject.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                subject.run_upstream("/usr/bin/dpkg-deb", ["--ctrl-tarfile"], source, self.root, subject.MIB)
        kill.assert_called_once_with(process.pid, subject.signal.SIGKILL)
        process.wait.assert_called_once()


    def test_actual_archive_owner_accepts_the_callback_inventory_tuple(self):
        fixture = archive_support.ArchiveInspectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        inventory = {'records': [{'name': 'fixed-ELF'}], 'bodies': {}}
        with mock.patch.object(subject, 'inspect_elf_ranges', return_value=inventory) as selected:
            result = archive_library.inspect_archive(fixture.row, fixture.root, fixture.scratch, None,
                fixture.proof, fixture.owners, audit_only=True,
                inventory_reader=lambda view: subject.elf_inventory(
                    view, fixture.row, fixture.scratch, original, elf_owner))
        selected.assert_called_once()
        self.assertIs(result['files'], inventory)
        self.assertTrue(result['audit_only'])
        self.assertFalse(result['Make_manifest_equivalence_claimed'])
        fixture.checks.image_inventory.assert_not_called()



if __name__ == "__main__":
    unittest.main()
