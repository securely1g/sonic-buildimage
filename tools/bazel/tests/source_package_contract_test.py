"""Bind selected Debian payloads and controls to their real producer archive."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

try:
    from tools.bazel.oci import source_package_contract as subject
except ModuleNotFoundError as error:
    if error.name != 'sonic_apt':
        raise
    subject = None

TAR = None


@unittest.skipIf(subject is None, 'Run the Bazel target with declared dependencies')
class SourcePackageContractTest(unittest.TestCase):
    def setUp(self):
        if not TAR:
            self.skipTest('Run with the declared BSD tar tool')
        tool = Path(TAR)
        if not tool.exists() and str(tool).startswith('external/'):
            tool = Path('..') / str(tool).removeprefix('external/')
        self.tar = str(tool.absolute())
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = '@example//debian:package'
        self.spec = {'schema': 1, 'architecture': 'amd64', 'packages': [{
            'package': 'libexample', 'deb_target': self.target,
            'source': {'target': self.target}, 'required_paths': {'usr/share/example.lua': 'file'}}]}
        self.make_deb()

    def make_deb(self, *, package='libexample', architecture='amd64', data=b'#!/usr/bin/lua\n'):
        entries = {
            'control.tar': ('control', ('Package: ' + package + '\nVersion: 1.2.3\nArchitecture: ' + architecture +
                '\nDepends: libc6 (>= 2.41)\nMaintainer: Test <test@example.invalid>\nDescription: Test\n').encode(), 0o644),
            'data.tar': ('usr/share/example.lua', data, 0o755),
        }
        for archive, (name, contents, mode) in entries.items():
            with tarfile.open(self.root / archive, 'w') as stream:
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(contents), mode
                stream.addfile(member, io.BytesIO(contents))
        (self.root / 'debian-binary').write_bytes(b'2.0\n')
        subprocess.run([self.tar, '--format=ar', '-cf', str(self.root / 'package.deb'), '-C', str(self.root),
                        'debian-binary', 'control.tar', 'data.tar'], check=True)

    def describe(self, spec=None, targets=None):
        return subject.describe(spec or self.spec, {'libexample': self.root / 'package.deb'},
                                targets or {'libexample': self.target}, self.tar)

    def test_actual_control_and_payload_hashes_replace_no_metadata(self):
        before = copy.deepcopy(self.spec)
        result = self.describe()['packages'][0]
        self.assertEqual(self.spec, before)
        self.assertEqual(result['control_fields']['Depends'], 'libc6 (>= 2.41)')
        self.assertEqual(result['version'], '1.2.3')
        self.assertEqual(result['deb'], {'target': self.target, 'sha256': subject.sha(self.root / 'package.deb'),
                                        'data_sha256': subject.sha(self.root / 'data.tar')})
        old = result['deb']
        self.make_deb(data=b'changed payload\n')
        new = self.describe()['packages'][0]['deb']
        self.assertNotEqual(old['sha256'], new['sha256'])
        self.assertNotEqual(old['data_sha256'], new['data_sha256'])

    def test_mixed_runtime_layer_does_not_need_a_deb_or_dependency_controls(self):
        spec = copy.deepcopy(self.spec)
        target = '@ordinary//:runtime_pkg'
        spec['packages'].append({'package': 'ordinary', 'kind': 'runtime-layer',
            'source': {'target': target, 'version': '0.0.1-' + 'a' * 40},
            'required_paths': {'usr/bin/ordinary': 'elf'},
            'path_modes': {'run/ordinary/config.json': 0o644}})
        result = subject.describe(spec, {'libexample': self.root / 'package.deb'},
                                  {'libexample': self.target, 'ordinary': target}, self.tar)
        runtime = result['packages'][1]
        self.assertEqual(runtime['kind'], 'runtime-layer')
        self.assertEqual(runtime['version'], runtime['source']['version'])
        self.assertEqual(runtime['path_modes'], {'run/ordinary/config.json': 0o644})
        self.assertNotIn('control_fields', runtime)
        self.assertNotIn('deb', runtime)
        self.assertIn('deb', result['packages'][0])

    def test_rejects_wrong_package_or_architecture(self):
        for fields in ({'package': 'other'}, {'architecture': 'arm64'}):
            with self.subTest(fields=fields):
                self.make_deb(**fields)
                with self.assertRaisesRegex(ValueError, 'Actual DEB differs'):
                    self.describe()

    def test_rejects_wrong_public_target(self):
        with self.assertRaisesRegex(ValueError, 'Actual DEB target differs'):
            self.describe(targets={'libexample': '@other//:package'})

    def test_rejects_copied_controls_or_file_metadata_overrides(self):
        for field in ('version', 'architecture', 'control_fields', 'deb', 'path_modes', 'path_owners'):
            with self.subTest(field=field):
                spec = copy.deepcopy(self.spec)
                spec['packages'][0][field] = {}
                with self.assertRaisesRegex(ValueError, 'copy or override'):
                    self.describe(spec)


if __name__ == '__main__':
    TAR = sys.argv.pop(1)
    unittest.main()
