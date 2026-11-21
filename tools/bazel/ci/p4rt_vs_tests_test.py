"""Verify that P4RT exercises all native tests and the installed VS inputs without packaging."""

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.bazel.ci import p4rt_vs_tests as tests


class VSOwnerTests(unittest.TestCase):
    native = '//p4rt_app/tests:source_test'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output, self.sdk, self.source = (self.root / name for name in ('out', 'sdk', 'pins'))
        for directory in (self.output, self.sdk, self.source):
            directory.mkdir()
        self.args = SimpleNamespace(owner_source=self.source, owner_revision='a' * 40, owner_bazel='bazel6',
                                    jobs=3, owner_output_base=self.root / 'base')
        self.targets = sorted([self.native, tests.INSTALLED])
        self.actions = {'targets': [{'id': 1, 'label': self.native}],
                        'pathFragments': [{'id': 1, 'label': 'external/sonic_swss_common/include/swss/test.h'},
                                          {'id': 2, 'label': 'external/sonic_swss_common/lib/libswsscommon.so.0'}],
                        'artifacts': [{'id': i, 'pathFragmentId': i} for i in (1, 2)],
                        'actions': [{'targetId': 1, 'mnemonic': 'CppCompile', 'outputIds': []}]}
        self.calls = []
        self.owner = Mock()
        self.built = {'test_targets': self.targets}
        self.owner.verify_build.return_value = self.built
        self.owner.runtime_environment.return_value = ({}, {'receipt_sha256': 'b' * 64})
        self.runtime = self.root / 'runtime.json'

    def bazel(self, args, **options):
        self.calls.append(args)
        if args[0] == 'aquery':
            json.dump(self.actions, options['stdout'])
        if args[0] == 'test':
            path = next(x.split('=', 1)[1] for x in args if x.startswith('--build_event_json_file='))
            Path(path).write_text(json.dumps({'id': {'testSummary': {'label': self.native}},
                                              'testSummary': {'overallStatus': 'PASSED'}}))
            log = self.root / 'p4rt_app/tests/source_test/test.xml'
            log.parent.mkdir(parents=True)
            log.write_text('<testsuite tests="1"><testcase name="source"/></testsuite>')
        values = {'version': 'bazel 6.4.0', 'query': '\n'.join(self.targets), 'info': str(self.root)}
        return subprocess.CompletedProcess(args, 0, stdout=values.get(args[0], ''))

    def invoke(self, command, **options):
        if command[0] == 'bazel6':
            self.assertEqual(options['env']['USE_BAZEL_VERSION'], '6.4.0')
            return self.bazel(command[2:], **options)
        self.assertEqual(options['cwd'], self.source)
        self.assertEqual(command[2], str(self.source / 'ci/runtime_run_under.py'))
        launcher = Path(command[command.index('--') + 1])
        self.assertIn(str(self.source / 'p4rt_app/runtime_package_test.py'), launcher.read_text())
        self.assertEqual(command[command.index('--runtime') + 1], self.output / 'p4rt_runtime.tar')
        self.assertEqual(command[command.index('--debug') + 1], self.output / 'p4rt_runtime_debug.tar')
        self.assertEqual(command[command.index('--runtime-contract') + 1], self.output / 'p4rt_runtime_contract.json')
        self.assertEqual(command[command.index('--debug-contract') + 1], self.output / 'p4rt_runtime_debug_contract.json')
        (Path(options['env']['TEST_UNDECLARED_OUTPUTS_DIR']) / 'installed-packages.json').write_text('{"status":"passed"}')
        return subprocess.CompletedProcess(command, 0)

    def run_tests(self):
        with patch.object(tests.p4rt_handoff, 'owner_api', return_value=self.owner), \
                patch.object(tests.p4rt_handoff, 'source_identity'), patch.object(tests.subprocess, 'run', self.invoke):
            tests.run(self.args, self.sdk, self.output, self.runtime)

    def test_native_runfiles_exclude_production_packages_and_installed_probe_consumes_vs(self):
        self.run_tests()
        selected, = [args for args in self.calls if args[0] == 'test']
        self.assertIn(self.native, selected)
        self.assertNotIn(tests.INSTALLED, selected)
        self.assertNotIn('build', [args[0] for args in self.calls])
        self.assertEqual(json.loads((self.output / 'test-results.json').read_text()), {self.native: 'PASSED'})
        self.owner.verify_installed.assert_called_once_with({'status': 'passed'}, self.built, self.sdk)
        self.owner.verify_runtime_unchanged.assert_called_once()

    def test_indirect_production_packages_are_rejected_before_any_native_test_runs(self):
        self.actions['targets'].append({'id': 2, 'label': '//p4rt_app:p4rt_runtime_debug'})
        with self.assertRaisesRegex(ValueError, 'would rebuild production'):
            self.run_tests()
        self.assertNotIn('test', [args[0] for args in self.calls])
        self.owner.verify_installed.assert_not_called()

    def test_omitting_any_original_target_cannot_produce_a_pass(self):
        with self.assertRaisesRegex(ValueError, 'differ from the VS'):
            tests.source_tests([self.native], self.targets)
