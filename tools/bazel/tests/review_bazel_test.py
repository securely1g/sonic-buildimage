"""Exercise review command forwarding and setup-bazel's persistent caches."""

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / 'tools/bazel/ci/review_bazel.sh'
spec = importlib.util.spec_from_file_location('review_bazel_rc', ROOT / 'tools/bazel/ci/review_bazel_rc.py')
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)
REVISION = '1234567890abcdef1234567890abcdef12345678'


class ReviewBazelTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sonic-review-test-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / 'source checkout'
        (self.workspace / 'tools/bazel').mkdir(parents=True)
        self.common = (ROOT / 'tools/bazel/common.bazelrc').read_text()
        (self.workspace / 'tools/bazel/common.bazelrc').write_text(self.common)
        self.home = self.directory / 'user configuration'
        self.home.mkdir()
        self.record = self.directory / 'called.json'
        self.fake = self.directory / 'bazel executable'
        self.fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
rc=next((x.split('=',1)[1] for x in args if x.startswith('--bazelrc=')),None)
pathlib.Path(os.environ['REVIEW_TEST_RECORD']).write_text(json.dumps({
    'arguments':args,'configuration':pathlib.Path(rc).read_text() if rc else '', 'rc_path':rc}))
sys.stdout.write(os.environ.get('REVIEW_TEST_STDOUT',''))
sys.stderr.write(os.environ.get('REVIEW_TEST_STDERR',''))
sys.exit(int(os.environ.get('REVIEW_TEST_EXIT','0')))
''')
        self.fake.chmod(0o755)

    def invoke(self, arguments, **changes):
        environment = dict(os.environ, SONIC_BAZEL_REGISTRY_REVISION=REVISION,
                           BAZEL_REAL=str(self.fake), REVIEW_TEST_RECORD=str(self.record))
        environment.update(changes)
        return subprocess.run([str(WRAPPER), *arguments], cwd=self.workspace,
                              env=environment, capture_output=True, text=True)

    def test_actual_command_and_startup_argument_forwarding(self):
        for command in ('build', 'aquery', 'cquery', 'test', 'query', 'fetch', 'info', 'mod'):
            with self.subTest(command=command):
                startup = ['--batch', '--output_base', str(self.directory / 'cached output'),
                           '--host_jvm_args=-Xmx3g', '--max_idle_secs=90']
                arguments = ['--jobs=2', '--disk_cache=' + str(self.directory / 'action cache'), '//owner:runtime']
                result = self.invoke([*startup, command, *arguments])
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                actual = record['arguments']
                self.assertEqual(actual[:len(startup)], startup)
                self.assertEqual(actual[-len(arguments):], arguments)
                self.assertEqual([x for x in actual if x.startswith('--registry=')], [
                    '--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/' + REVISION,
                    '--registry=https://bcr.bazel.build'])
                self.assertEqual(actual[actual.index(command)-1], '--bazelrc=' + record['rc_path'])
                self.assertIn('--nohome_rc', actual)
                self.assertNotIn('try-import', record['configuration'])
                self.assertIn('--host_platform=', record['configuration'])
                self.assertFalse(Path(record['rc_path']).exists(), 'temporary rc leaked')

    def test_image_root_wrappers_select_the_coherent_engine_catalog(self):
        expected = '9f225754068d1253ca56d6d40a7c5b2bd9de35ac'
        commands = self.directory / 'commands'
        commands.mkdir()
        (commands / 'bazel').symlink_to(self.fake)
        (self.home / '.bazelrc').write_text(
            'common --repository_cache=/persistent/repository\n'
            'build --disk_cache=/persistent/disk\n')
        local = ('common --distdir=/verified/distdir\n'
                 'common --experimental_downloader_config=/verified/mirrors.cfg\n')
        (self.workspace / '.bazelrc.user').write_text(local)
        environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                           HOME=str(self.home), BASH_ENV='', BAZEL_REAL=str(self.fake),
                           REVIEW_TEST_RECORD=str(self.record), SONIC_BAZEL_REGISTRY_REVISION=REVISION)
        for family in ('libteam', 'lldp', 'frr', 'framework'):
            wrapper = ROOT / ('tools/bazel/ci/' + family + '_draft_bazel.sh')
            for command in ('aquery', 'build', 'cquery', 'fetch', 'info', 'mod', 'query', 'test'):
                with self.subTest(wrapper=family, command=command):
                    startup = ['--batch', '--output_base=' + str(self.directory / 'persistent output')]
                    arguments = ['--jobs=2', '--disk_cache=/existing/action-cache', '//owner:runtime']
                    result = subprocess.run([str(wrapper), *startup, command, *arguments],
                                            cwd=self.workspace, env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    actual = record['arguments']
                    self.assertEqual(actual[:len(startup)], startup)
                    self.assertEqual(actual[-len(arguments):], arguments)
                    self.assertEqual([x for x in actual if x.startswith('--registry=')], [
                        '--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/' + expected,
                        '--registry=https://bcr.bazel.build'])
                    if family == 'libteam':
                        for setting in ('--repository_cache=/persistent/repository', '--disk_cache=/persistent/disk',
                                        '--distdir=/verified/distdir', '--experimental_downloader_config=/verified/mirrors.cfg'):
                            self.assertIn(setting, record['configuration'])
                    else:
                        self.assertEqual(record['configuration'], self.common)
                        self.assertIn('try-import %workspace%/.bazelrc.user', record['configuration'])
                    self.assertEqual((self.workspace / '.bazelrc.user').read_text(), local)

    def test_standalone_image_workflow_phases_forward_the_review_wrapper(self):
        """Parse each real driver's CLI; replace only its native phase body."""
        expected = str(ROOT / 'tools/bazel/ci/libteam_draft_bazel.sh')
        for family in ('database', 'eventd', 'radv', 'snmp'):
            workflow = 'bazel-snmp.yml' if family == 'snmp' else 'bazel-' + family + '-image.yml'
            text = (ROOT / '.github/workflows' / workflow).read_text()
            driver = importlib.import_module('tools.bazel.ci.' + family + '_image')
            if family != 'snmp':
                selected = re.search(r'^      BAZEL: (.+)$', text, re.MULTILINE)[1]
                self.assertEqual(selected.replace('${{ github.workspace }}', str(ROOT)), expected)
            for phase in ('build', 'incremental'):
                with self.subTest(family=family, phase=phase):
                    lines = [line.strip() for line in text.splitlines()
                             if 'tools/bazel/ci/' + family + '_image.py ' + phase + ' ' in line]
                    self.assertEqual(len(lines), 1)
                    line = lines[0].replace('$BAZEL', expected).replace('$GITHUB_WORKSPACE', str(ROOT))
                    argv = shlex.split(line)
                    self.assertEqual(argv[:2], ['python3', '-B'])
                    old_directory = Path.cwd()
                    try:
                        os.chdir(self.workspace)
                        with mock.patch.object(sys, 'argv', argv[2:]), \
                             mock.patch.object(driver, 'build_images') as build, \
                             mock.patch.object(driver, 'incremental') as incremental:
                            driver.main()
                            selected = build if phase == 'build' else incremental
                            selected.assert_called_once()
                            self.assertEqual(selected.call_args.args[0].bazel, expected)
                            self.assertEqual(selected.call_args.args[0].phase, phase)
                            (incremental if phase == 'build' else build).assert_not_called()
                    finally:
                        os.chdir(old_directory)

    def test_home_and_local_cache_values_survive(self):
        (self.home / '.bazelrc').write_text('''build --disk_cache='/persistent/action cache'
common --repository_cache=/persistent/downloads
common --registry=https://example.invalid/catalog
build --override_module=owner=/not-accepted
''')
        (self.workspace / '.bazelrc.user').write_text('''common --distdir '/verified source downloads'
common --repository_cache=/existing/local-downloads
common --experimental_repository_cache_hardlinks
common --registry=https://example.invalid/second-catalog
try-import /not-imported
''')
        result = rc.configuration(self.workspace, self.home)
        parsed = [shlex.split(line, comments=True) for line in result.splitlines()]
        self.assertIn(['build', '--disk_cache=/persistent/action cache'], parsed)
        self.assertIn(['common', '--repository_cache=/persistent/downloads'], parsed)
        self.assertIn(['common', '--repository_cache=/existing/local-downloads'], parsed)
        self.assertIn(['common', '--distdir=/verified source downloads'], parsed)
        self.assertIn(['common', '--experimental_repository_cache_hardlinks'], parsed)
        self.assertNotIn('--registry', result)
        self.assertNotIn('--override_module', result)
        self.assertNotIn('try-import', result)
        self.assertEqual((self.workspace / 'tools/bazel/common.bazelrc').read_text(), self.common)

    def test_verified_downloader_configuration_survives(self):
        (self.home / '.bazelrc').write_text(
            'common --experimental_downloader_config=/home/source-mirrors.cfg\n')
        (self.workspace / '.bazelrc.user').write_text(
            "common --experimental_downloader_config '/verified mirrors/source.cfg'\n"
            'common --registry=https://example.invalid/not-a-mirror\n')
        result = self.invoke(['build', '//owner:runtime'], HOME=str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.record.read_text())
        parsed = [shlex.split(line, comments=True) for line in record['configuration'].splitlines()]
        values = [line for line in parsed if len(line) == 2 and
                  line[1].startswith('--experimental_downloader_config=')]
        self.assertEqual(values, [
            ['common', '--experimental_downloader_config=/home/source-mirrors.cfg'],
            ['common', '--experimental_downloader_config=/verified mirrors/source.cfg']])
        self.assertNotIn('example.invalid', record['configuration'])
        self.assertEqual(len([arg for arg in record['arguments'] if arg.startswith('--registry=')]), 2)

    def test_missing_or_nonimmutable_catalog_fails_before_bazel(self):
        for revision in ('', 'main', '../main', 'a'*39, 'a'*41, 'A'*40):
            with self.subTest(revision=revision):
                result = self.invoke(['build', '//owner:runtime'], SONIC_BAZEL_REGISTRY_REVISION=revision)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.record.exists())

    def test_extra_registries_and_rc_overrides_are_rejected(self):
        for arguments in (['--bazelrc=/tmp/other', 'build'], ['--home_rc', 'build'],
                          ['--ignore_all_rc_files', 'build'], ['build', '--registry=https://example.invalid'],
                          ['build', '--registry', 'https://example.invalid'], ['clean'], []):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(arguments).returncode, 2)
                self.assertFalse(self.record.exists())

    def test_bazel_failure_and_version_are_preserved(self):
        result = self.invoke(['build', '//owner:missing'], REVIEW_TEST_EXIT='37')
        self.assertEqual(result.returncode, 37)
        self.assertFalse(Path(json.loads(self.record.read_text())['rc_path']).exists())
        result = self.invoke(['--version'], SONIC_BAZEL_REGISTRY_REVISION='')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.record.read_text())['arguments'], ['version', '--gnu_format'])

    def test_configuration_drift_fails_closed(self):
        common = self.workspace / 'tools/bazel/common.bazelrc'
        for extra in ('common --registry=https://example.invalid\n', 'import /other/config\n'):
            common.write_text(self.common + extra)
            with self.assertRaises(ValueError):
                rc.configuration(self.workspace, self.home)
        common.write_text(self.common.replace('try-import %workspace%/.bazelrc.user', ''))
        with self.assertRaises(ValueError):
            rc.configuration(self.workspace, self.home)

    def test_version_removes_only_the_leading_bazelisk_banner(self):
        cases = (
            ('plain', 'bazel 8.5.1\n', 'bazel 8.5.1\n', 0),
            ('bazelisk', 'Bazelisk v1.29.0\nbazel 8.5.1\n', 'bazel 8.5.1\n', 0),
            ('later-banner', 'bazel 8.5.1\nBazelisk later output\n',
             'bazel 8.5.1\nBazelisk later output\n', 0),
            ('other-output', 'warning\nBazelisk v1.29.0\nbazel 8.5.1\n',
             'warning\nBazelisk v1.29.0\nbazel 8.5.1\n', 0),
            ('backend-failure', 'Bazelisk v1.29.0\nbackend output\n',
             'backend output\n', 37),
        )
        for name, output, expected, exit_code in cases:
            with self.subTest(name=name):
                result = self.invoke(
                    ['--version'], SONIC_BAZEL_REGISTRY_REVISION='',
                    REVIEW_TEST_STDOUT=output, REVIEW_TEST_STDERR='backend diagnostic\n',
                    REVIEW_TEST_EXIT=str(exit_code))
                self.assertEqual(result.returncode, exit_code)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, 'backend diagnostic\n')
                self.assertEqual(json.loads(self.record.read_text())['arguments'],
                                 ['version', '--gnu_format'])


if __name__ == '__main__':
    unittest.main()
