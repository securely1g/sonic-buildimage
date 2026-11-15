"""Focused tests for retained provider/runfiles/BEP boundaries; no Bazel execution."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


OWNER = module(os.environ['VS_OWNER_TEST_DIAGNOSTIC_SOURCE'], '_targeted_diagnostic')
PUBLIC = module(os.environ['OWNER_DIAGNOSTIC_PUBLIC_SOURCE'], '_targeted_public')


class OwnerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.execution = self.root / 'execroot'
        self.execution.mkdir()
        self.events = self.root / 'tests.events.json'

    def runtime(self):
        result = []
        for number, target in enumerate(OWNER.TARGETS[:3]):
            executable = 'out/test' + str(number)
            manifest = executable + '.manifest'
            for name in (executable, manifest):
                path = self.execution / name
                path.parent.mkdir(exist_ok=True)
                path.write_text('original')
            result.append({'label': '@@' + target, 'executable': executable, 'manifest': manifest})
        return result

    def parse_runtime(self, rows):
        return OWNER.runtime_rows(('\n'.join(json.dumps(row) for row in rows) + '\n').encode(),
                                  PUBLIC, self.execution, self.root)

    def test_runtime_actual_defaultinfo_shape(self):
        rows = self.runtime()
        result = self.parse_runtime(rows)
        self.assertEqual(set(result), set(OWNER.TARGETS[:3]))
        self.assertTrue(all(value['manifest'].is_file() for value in result.values()))

    def test_runtime_duplicate_after_main_normalization(self):
        rows = self.runtime()
        rows.append(dict(rows[0], label=OWNER.TARGETS[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.parse_runtime(rows)

    def test_runtime_missing_target(self):
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            self.parse_runtime(self.runtime()[:-1])

    def test_runtime_external_label_rejected(self):
        rows = self.runtime()
        rows[0]['label'] = '@@external//:owner'
        with self.assertRaisesRegex(ValueError, 'Different'):
            self.parse_runtime(rows)

    def test_runtime_null_provider_output_rejected(self):
        rows = self.runtime()
        rows[0]['manifest'] = None
        with self.assertRaisesRegex(ValueError, 'Invalid configured output'):
            self.parse_runtime(rows)

    def test_runtime_parent_escape_rejected(self):
        rows = self.runtime()
        rows[0]['executable'] = '../outside'
        with self.assertRaisesRegex(ValueError, 'relative'):
            self.parse_runtime(rows)

    def runfiles(self):
        python = self.root / 'python/bin/python3'
        installer = self.root / 'package/site-packages/installer/__init__.py'
        for path in (python, installer):
            path.parent.mkdir(parents=True)
            path.write_text('retained input')
        return [
            'rules_python++python+python_3_13_x86_64-unknown-linux-gnu/bin/python3 ' + str(python),
            'rules_python++pip+config_engine_pip_313_installer/site-packages/installer/__init__.py ' + str(installer),
        ]

    def parse_manifest(self, lines):
        return OWNER.selected_runfiles(('\n'.join(lines) + '\n').encode(), self.root)

    def test_manifest_selected_declared_inputs(self):
        rows = self.runfiles()
        result = self.parse_manifest(rows + [' unrelated\\sname /unrelated\\spath'])
        self.assertEqual(set(result), {'python', 'installer'})
        self.assertTrue(all(row[2].is_relative_to(self.root) for row in result.values()))

    def test_manifest_duplicate_runtime_rejected(self):
        rows = self.runfiles()
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.parse_manifest(rows + rows[:1])

    def test_manifest_escaped_selected_input_rejected(self):
        rows = self.runfiles()
        rows[1] = ' ' + rows[1]
        with self.assertRaisesRegex(ValueError, 'Escaped'):
            self.parse_manifest(rows)

    def test_manifest_path_outside_job_rejected(self):
        rows = self.runfiles()
        rows[0] = rows[0].partition(' ')[0] + ' /etc/passwd'
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.parse_manifest(rows)

    def bep(self, *, passed=False, omit_zero=False):
        labels = {target if target.startswith('//') else '@@scapy+//:upstream_test': target
                  for target in OWNER.TARGETS}
        records = []
        for number, (canonical, target) in enumerate(labels.items()):
            outputs = []
            names = ['test.log', 'test.xml']
            if target == '@scapy//:upstream_test':
                names.append('test.outputs/upstream-xunit.xml')
            for index, name in enumerate(names):
                path = self.root / ('original-' + str(number) + '-' + str(index))
                path.write_bytes(b'<testsuite><error><![CDATA[actual failure]]></error></testsuite>\n'
                                 b'<testsuite name="second"/>\n')
                outputs.append({'name': name, 'uri': path.as_uri()})
            records.append({'id': {'testResult': {'label': canonical}},
                            'testResult': {'status': 'PASSED' if passed else 'FAILED',
                                           'testActionOutput': outputs}})
        exit_code = {'name': 'SUCCESS'} if omit_zero else {'code': 0 if passed else 3}
        records.extend([{'finished': {'exitCode': exit_code}}, {'lastMessage': True}])
        return labels, records

    def parse_bep(self, labels, rows):
        self.events.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        return OWNER.test_evidence(self.events, labels, self.root, PUBLIC)

    def test_failed_test_results_and_raw_concatenated_xunit_preserved(self):
        labels, rows = self.bep()
        outcomes, outputs, code = self.parse_bep(labels, rows)
        self.assertEqual(set(outcomes.values()), {'FAILED'})
        self.assertEqual(code, 3)
        xunit = outputs['@scapy//:upstream_test', 'test.outputs/upstream-xunit.xml']
        self.assertEqual(xunit.read_bytes().count(b'<testsuite'), 2)
        self.assertIn(b'actual failure', xunit.read_bytes())

    def test_success_default_zero_exit_omission(self):
        labels, rows = self.bep(passed=True, omit_zero=True)
        outcomes, _, code = self.parse_bep(labels, rows)
        self.assertEqual(code, 0)
        self.assertEqual(set(outcomes.values()), {'PASSED'})

    def test_bep_duplicate_test_rejected(self):
        labels, rows = self.bep()
        rows.insert(0, rows[0])
        with self.assertRaisesRegex(ValueError, 'Repeated'):
            self.parse_bep(labels, rows)

    def test_bep_incomplete_stream_rejected(self):
        labels, rows = self.bep()
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            self.parse_bep(labels, rows[:-1])

    def test_bep_remote_uri_rejected(self):
        labels, rows = self.bep()
        rows[0]['testResult']['testActionOutput'][0]['uri'] = 'https://example.com/test.log'
        with self.assertRaisesRegex(ValueError, 'local URI'):
            self.parse_bep(labels, rows)

    def test_bep_unknown_status_rejected(self):
        labels, rows = self.bep()
        rows[0]['testResult']['status'] = 'UNKNOWN'
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.parse_bep(labels, rows)

    def test_missing_registered_original_marks_incomplete(self):
        output = self.root / 'captured'
        output.mkdir()
        result = {'status': 'running', 'files': {}}
        def unexpected(name, path):
            self.fail('missing file must not be copied')
        complete = OWNER.capture_registered([('missing.raw', self.root / 'absent')],
                                            output, unexpected, result)
        self.assertFalse(complete)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['files']['missing.raw']['retained'])

    def test_copy_failure_preserves_other_original_and_marks_incomplete(self):
        output = self.root / 'captured'
        output.mkdir()
        original = self.root / 'present'
        original.write_bytes(b'exact original')
        result = {'status': 'running', 'files': {}}
        def copy(name, path):
            if name == 'failed.raw':
                raise OSError('simulated write failure')
            (output / name).write_bytes(path.read_bytes())
            result['files'][name] = OWNER.identity(output / name)
        complete = OWNER.capture_registered([('failed.raw', original), ('good.raw', original)],
                                            output, copy, result)
        self.assertFalse(complete)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['files']['failed.raw']['retained'])
        self.assertEqual((output / 'good.raw').read_bytes(), b'exact original')


if __name__ == '__main__':
    unittest.main()

