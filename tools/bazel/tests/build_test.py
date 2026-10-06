"""Exercise shared build/query/export with unrelated target declarations."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import build


class BuildEvidenceTest(unittest.TestCase):
    """Exercise collection through a subprocess that simulates Bazel commands."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'checkout'
        self.workspace.mkdir()
        self.directory = self.root / 'evidence'
        self.directory.mkdir()
        self.receipt = {'commands': []}
        self.targets = {'telemetry.tar': '@telemetry//dist:runtime',
                        'routing.tar': '@routing//:package'}
        self.output_base = self.root / 'output base'
        self.roots = {'@telemetry//dist:runtime': 'external/telemetry+',
                      '@routing//:package': 'external/routing+'}
        for root in self.roots.values():
            (self.output_base / root).mkdir(parents=True)
        self.mode = self.workspace / 'mode'
        self.mode.write_text('')
        self.bazel = self.root / 'bazel executable'
        self.bazel.write_text('#!/usr/bin/env python3\n' + f'''
import json, pathlib, sys
args = sys.argv[1:]
mode = pathlib.Path('mode').read_text()
labels = {self.targets!r}
roots = {self.roots!r}
if args[0] == 'info':
    if args[1:] != ['output_base']:
        raise SystemExit('unexpected configured info query')
    print({str(self.output_base)!r})
elif args[0] == 'build':
    if mode == 'build_failure':
        raise SystemExit('fixture build failed')
    output = pathlib.Path('bazel-bin/archive outputs')
    output.mkdir(parents=True, exist_ok=True)
    for name in labels:
        (output / name).write_bytes(b'' if mode == 'empty_file' else ('payload ' + name).encode())
elif args[0] == 'cquery':
    print('INFO: query diagnostic', file=sys.stderr)
    label = args[-1]
    if mode == 'query_failure':
        print('bogus-but-existing-output.tar')
        raise SystemExit('fixture query failed')
    if '--output=starlark' in args:
        print(roots.get(label, ''))
        if mode == 'multiple_roots': print('external/extra+')
    else:
        name = next(name for name, target in labels.items() if target == label)
        if mode != 'no_outputs':
            print('bazel-bin/archive outputs/' + ('absent.tar' if mode == 'missing_file' else name))
        if mode == 'multiple_outputs': print('bazel-bin/second.tar')
else:
    raise SystemExit('unexpected operation: ' + args[0])
''')
        self.bazel.chmod(0o755)
        self.options = ['--platforms=@platforms//:unrelated', '--jobs=2']

    def collect(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return build.collect_archives(self.workspace, self.directory, self.receipt, self.targets,
                                          bazel=str(self.bazel), options=self.options)

    def test_two_consumers_keep_labels_options_bytes_and_hash_receipts(self):
        """Export unrelated targets with correct evidence and preserve unchanged mtimes."""
        paths = self.collect()
        self.assertEqual(set(paths), set(self.targets))
        for name, target in self.targets.items():
            content = ('payload ' + name).encode()
            self.assertEqual(paths[name].read_bytes(), content)
            self.assertEqual(paths[name].stat().st_mode & 0o777, 0o644)
            self.assertEqual(self.receipt['artifacts'][name], {
                'target': target, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
            self.assertIn('INFO: query diagnostic', (self.directory / (name + '.query.log')).read_text())
        commands = [command['argv'] for command in self.receipt['commands']]
        self.assertEqual([command[1] for command in commands], ['build', 'cquery', 'cquery'])
        for command in commands:
            for option in self.options: self.assertIn(option, command)
        self.assertEqual(commands[0][-2:], list(self.targets.values()))
        before = {name: path.stat().st_mtime_ns for name, path in paths.items()}
        self.collect()
        self.assertEqual(before, {name: path.stat().st_mtime_ns for name, path in paths.items()})

    def test_query_failures_keep_prior_outputs_and_command_evidence(self):
        """Keep prior publications and diagnostics when queries fail or yield invalid files."""
        for mode in ('query_failure', 'no_outputs', 'multiple_outputs', 'missing_file', 'empty_file'):
            with self.subTest(mode=mode):
                self.mode.write_text(mode)
                self.receipt = {'commands': []}
                previous = self.directory / 'telemetry.tar'
                previous.write_bytes(b'previous publication')
                mtime = previous.stat().st_mtime_ns
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.collect()
                self.assertEqual((previous.read_bytes(), previous.stat().st_mtime_ns),
                                 (b'previous publication', mtime))
                self.assertEqual(self.receipt['artifacts'], {})
                self.assertEqual(len(self.receipt['commands']), 2)
                self.assertIn('INFO: query diagnostic', (self.directory / 'telemetry.tar.query.log').read_text())

    def test_build_failure_stops_queries_and_retains_failure_receipt(self):
        """Stop after a failed build and retain its exit status and diagnostic log."""
        self.mode.write_text('build_failure')
        with self.assertRaises(subprocess.CalledProcessError): self.collect()
        self.assertEqual(self.receipt['artifacts'], {})
        self.assertEqual(len(self.receipt['commands']), 1)
        self.assertEqual(self.receipt['commands'][0]['returncode'], 1)
        self.assertIn('fixture build failed', (self.directory / 'build.log').read_text())

    def test_failed_copy_keeps_prior_archive_and_no_completed_export_receipt(self):
        """Preserve the published archive and remove temporary output when copying fails."""
        previous = self.directory / 'telemetry.tar'
        previous.write_bytes(b'previous publication')
        with mock.patch('tools.bazel.build_helpers.shutil.copyfile', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'): self.collect()
        self.assertEqual(previous.read_bytes(), b'previous publication')
        self.assertEqual(self.receipt['artifacts'], {})
        self.assertEqual(sorted(path.name for path in self.directory.glob('telemetry.tar.*')),
                         ['telemetry.tar.query.log'])

    def test_source_lookup_handles_two_external_repositories_and_main_workspace(self):
        """Resolve external and local source roots using an unconfigured output-base query."""
        for target in (*self.roots, '//local:target'):
            with self.subTest(target=target), contextlib.redirect_stdout(io.StringIO()):
                result = build.source_directory(self.workspace, self.directory, self.receipt, target,
                                                bazel=str(self.bazel), options=self.options,
                                                name='lookup-' + str(len(self.receipt['commands'])))
                self.assertEqual(result, self.output_base / self.roots[target]
                                 if target in self.roots else self.workspace)
        info_commands = [command['argv'][1:] for command in self.receipt['commands']
                         if command['argv'][1] == 'info']
        self.assertEqual(info_commands, [['info', 'output_base']] * 3)

    def test_source_lookup_rejects_ambiguous_query_results(self):
        """Reject multiple repository roots instead of silently choosing one."""
        self.mode.write_text('multiple_roots')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'one source repository'):
            build.source_directory(self.workspace, self.directory, self.receipt, next(iter(self.roots)),
                                   bazel=str(self.bazel), options=self.options)


if __name__ == '__main__':
    unittest.main()
