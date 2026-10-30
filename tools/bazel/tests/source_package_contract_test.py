"""Bind source TAR declarations without constructing Debian package fixtures."""
import copy
import unittest
from tools.bazel.oci import source_package_contract as subject


class SourcePackageContractTest(unittest.TestCase):
    def setUp(self):
        self.target = '@example//:runtime'
        self.spec = {'schema': 1, 'architecture': 'amd64', 'packages': [{
            'package': 'libexample', 'kind': 'runtime-layer',
            'source': {'target': self.target, 'version': '1.2.3.sonic.1'},
            'required_paths': {'usr/lib/libexample.so.1': 'elf'},
            'path_modes': {'usr/share/example.conf': 0o644}}]}

    def describe(self, spec=None, targets=None):
        return subject.describe(spec or self.spec, targets or {'libexample': self.target})

    def test_runtime_layer_keeps_provenance_and_declared_install_metadata(self):
        before = copy.deepcopy(self.spec)
        runtime = self.describe()['packages'][0]
        self.assertEqual(self.spec, before)
        self.assertEqual(runtime['version'], runtime['source']['version'])
        self.assertEqual(runtime['architecture'], 'amd64')
        self.assertEqual(runtime['path_modes'], {'usr/share/example.conf': 0o644})
        self.assertNotIn('control_fields', runtime)
        self.assertNotIn('deb', runtime)

    def test_rejects_wrong_public_target(self):
        with self.assertRaisesRegex(ValueError, 'Runtime layer target differs'):
            self.describe(targets={'libexample': '@other//:runtime'})

    def test_rejects_copied_debian_identity(self):
        for field in ('version', 'architecture', 'control_fields', 'deb', 'deb_target'):
            with self.subTest(field=field):
                spec = copy.deepcopy(self.spec)
                spec['packages'][0][field] = {}
                with self.assertRaisesRegex(ValueError, 'copy or override'):
                    self.describe(spec)

    def test_rejects_deb_declarations(self):
        specification = copy.deepcopy(self.spec)
        del specification['packages'][0]['kind']
        with self.assertRaisesRegex(ValueError, 'must supply runtime TARs'):
            self.describe(specification)

    def test_rejects_missing_extra_or_duplicate_sources(self):
        for targets in ({}, {'libexample': self.target, 'extra': '@extra//:runtime'}):
            with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, 'inputs differ'):
                subject.describe(self.spec, targets)
        specification = copy.deepcopy(self.spec)
        specification['packages'].append(copy.deepcopy(specification['packages'][0]))
        with self.assertRaisesRegex(ValueError, 'inputs differ'):
            self.describe(specification)


if __name__ == '__main__':
    unittest.main(argv=[__import__('sys').argv[0]])
