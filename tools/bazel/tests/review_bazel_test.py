"""Exercise review command forwarding and setup-bazel's persistent caches."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import textwrap
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
sys.exit(int(os.environ.get('REVIEW_TEST_EXIT','0')))
''')
        self.fake.chmod(0o755)

    def invoke(self, arguments, **changes):
        environment = dict(os.environ, SONIC_BAZEL_REGISTRY_REVISION=REVISION,
                           BAZEL_REAL=str(self.fake), REVIEW_TEST_RECORD=str(self.record))
        environment.update(changes)
        return subprocess.run([str(WRAPPER), *arguments], cwd=self.workspace,
                              env=environment, capture_output=True, text=True)

    def test_debian_bootstrap_verifies_bytes_and_keeps_existing_cache_settings(self):
        for workflow, job, revision, expected in (
            ('bazel-oci.yml', 'checks', '4f1b2b169066b4d12d94a5edee5b6e36e36b1370',
             '8a61da28325afa7c488843f98a59ba5c7eccb71a2a0300657a58510105692b18'),
            ('bazel-oci.yml', 'archive', '97cc502556b7ec3435d2d67b105315bcc18bb47e',
             'ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720'),
            ('bazel-libteam.yml', 'native', '97cc502556b7ec3435d2d67b105315bcc18bb47e',
             'ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720'),
            ('bazel-supervisor-rust.yml', 'native', '97cc502556b7ec3435d2d67b105315bcc18bb47e',
             'ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720'),
            ('bazel-yang-models.yml', 'package', '97cc502556b7ec3435d2d67b105315bcc18bb47e',
             'ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720'),
            ('bazel-base-images.yml', 'native', '97cc502556b7ec3435d2d67b105315bcc18bb47e',
             'ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720'),
            ('bazel-eventd.yml', 'native', '97cc502556b7ec3435d2d67b105315bcc18bb47e',
             'ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720'),
        ):
            fixture = ROOT / 'tools/bazel/tests/fixtures' / ('source-mirrors-' + revision[:8] + '.cfg')
            data = fixture.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
            url = 'https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/' + revision + '/ci/source_mirrors.cfg'
            document = (ROOT / '.github/workflows' / workflow).read_text()
            section = re.search(r'^  ' + job + r':\n(.*?)(?=^  \S|\Z)', document, re.M | re.S).group(1)
            steps = re.split(r'^      - ', section, flags=re.M)[1:]
            setup = next(i for i, s in enumerate(steps) if 'setup-bazel@' in s)
            bootstrap = next(s for s in steps if s.startswith('name: Configure the verified source mirror\n'))
            self.assertEqual(steps.index(bootstrap), setup + 1)
            code = textwrap.dedent(bootstrap.split("python3 - <<'PYCFG'\n", 1)[1].rsplit('          PYCFG', 1)[0])
            for corrupt in (False, True):
                with self.subTest(workflow=workflow, corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    before = 'common --repository_cache=/retained/repository\nbuild --disk_cache=/retained/disk\ncommon --distdir=/retained/dist\n'
                    (root / '.bazelrc.user').write_text(before)
                    with contextlib.chdir(root), mock.patch.dict(os.environ, {'RUNNER_TEMP': str(root)}), \
                            mock.patch('urllib.request.urlopen', return_value=io.BytesIO(data + (b'corrupt' if corrupt else b''))) as fetch:
                        if corrupt:
                            with self.assertRaisesRegex(SystemExit, '^Source mirror configuration checksum mismatch$'):
                                exec(compile(code, workflow, 'exec'), {})
                        else:
                            exec(compile(code, workflow, 'exec'), {})
                    fetch.assert_called_once_with(url, timeout=60)
                    config = root / 'sonic-source-mirrors.cfg'
                    self.assertEqual(config.exists(), not corrupt)
                    self.assertEqual((root / '.bazelrc.user').read_text(), before + (
                        '' if corrupt else 'common --experimental_downloader_config=' + str(config) + '\n'))
                    if not corrupt:
                        self.assertEqual(config.read_bytes(), data)
                        (root / 'tools/bazel').mkdir(parents=True)
                        (root / 'tools/bazel/common.bazelrc').write_text(self.common)
                        effective = rc.configuration(root, self.home)
                        for value in ('--repository_cache=/retained/repository', '--disk_cache=/retained/disk',
                                      '--distdir=/retained/dist', '--experimental_downloader_config=' + str(config)):
                            self.assertIn(value, effective)

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
        self.assertEqual(json.loads(self.record.read_text())['arguments'], ['--version'])

    def test_configuration_drift_fails_closed(self):
        common = self.workspace / 'tools/bazel/common.bazelrc'
        for extra in ('common --registry=https://example.invalid\n', 'import /other/config\n'):
            common.write_text(self.common + extra)
            with self.assertRaises(ValueError):
                rc.configuration(self.workspace, self.home)
        common.write_text(self.common.replace('try-import %workspace%/.bazelrc.user', ''))
        with self.assertRaises(ValueError):
            rc.configuration(self.workspace, self.home)


if __name__ == '__main__':
    unittest.main()
