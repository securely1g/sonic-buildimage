"""Reject stale installation receipts and edited source files before OCI assembly."""
import io
import copy
import fnmatch
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from tools.bazel.oci import installed_source_metadata as subject
    from tools.bazel.oci.source_contract import generate
except ModuleNotFoundError as error:
    if error.name not in ('sonic_apt', 'sonic_oci'):
        raise
    subject = None


TAR = None


@unittest.skipIf(subject is None, 'Run the Bazel target with its declared sonic_apt dependency')
class InstalledSourceMetadataTest(unittest.TestCase):
    def test_stale_receipt_is_rejected_before_reading_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filesystem = root / 'rootfs.tar'
            filesystem.write_bytes(b'not an archive: stale receipt must fail first')
            receipt = root / 'receipt.json'
            receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'amd64',
                                          'rootfs_sha256': '0' * 64}))
            with patch.object(subject, 'apply_layer', side_effect=AssertionError('read stale input')):
                with self.assertRaisesRegex(ValueError, 'receipt does not match'):
                    subject.describe(filesystem, receipt, root / 'spec.json', 'amd64')

    def test_wrong_architecture_is_rejected_even_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filesystem = root / 'rootfs.tar'
            filesystem.write_bytes(b'wrong architecture')
            receipt = root / 'receipt.json'
            receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'arm64',
                                          'rootfs_sha256': subject.sha(filesystem)}))
            with self.assertRaisesRegex(ValueError, 'receipt does not match'):
                subject.describe(filesystem, receipt, root / 'spec.json', 'amd64')



@unittest.skipIf(subject is None, 'Run with the declared sonic_apt dependency')
class SourceOverlayTest(unittest.TestCase):
    """Real installed Debian metadata is retained during checked source TAR overlays."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.status = b'Package: common\nVersion: 1.0\nArchitecture: amd64\nStatus: install ok installed\nDescription: Common\n\n'
        self.files = {
            'var/lib/dpkg/status': (self.status, 0o644),
            'var/lib/dpkg/info/common.list': (b'/usr/lib/libcommon.so.0\n/usr/lib/libcommon.so.0.0.0\n/var/run/common/config\n', 0o644),
            'usr/lib/libcommon.so.0.0.0': (b'Make library', 0o644),
            'usr/lib/libcommon.so.0': ('libcommon.so.0.0.0', 0o777),
            'var/run': ('/run', 0o777),
            'run/common/config': (b'old config', 0o644),
        }
        self.original = {'usr/lib/libcommon.so.0.0.0': (b'Bazel library', 0o644),
                         'usr/lib/libcommon.so.0': ('libcommon.so.0.0.0', 0o777),
                         'var/run/common/config': (b'new config', 0o664)}
        self.normalized = {**self.original, 'run/common/config': (b'new config', 0o644)}
        del self.normalized['var/run/common/config']
        (self.root / 'spec.json').write_text(json.dumps({'schema': 1, 'packages': {'common': {
            'target': '//common:runtime', 'source_revision': 'reviewed-source',
            'required_paths': ['usr/lib/libcommon.so.0', 'var/run/common/config']}}}))

    def write_tar(self, name, entries):
        import tarfile
        path = self.root / name
        with tarfile.open(path, 'w') as stream:
            for name, (data, mode) in entries.items():
                entry = tarfile.TarInfo(name)
                entry.mode = mode
                if isinstance(data, str):
                    entry.type, entry.linkname = tarfile.SYMTYPE, data
                    stream.addfile(entry)
                else:
                    entry.size = len(data)
                    stream.addfile(entry, io.BytesIO(data))
        return path

    def inspect(self, **options):
        rootfs = self.write_tar('rootfs.tar', self.files)
        original = self.write_tar('source.tar', self.original)
        normalized = self.write_tar('normalized.tar', self.normalized)
        receipt = self.root / 'receipt.json'
        receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'amd64',
                                      'rootfs_sha256': subject.sha(rootfs), 'input_packages': []}))
        return subject.describe(rootfs, receipt, self.root / 'spec.json', 'amd64',
                                payloads={'common': original}, runtime_layer=normalized, **options)

    def test_same_owner_overlay_requires_explicit_compatibility(self):
        with self.assertRaisesRegex(ValueError, 'Debian-owned'):
            self.inspect()
        proof = self.inspect(allow_same_owner_overlays=True)
        overlay = proof['verified_overlays']
        self.assertEqual(set(overlay['paths']), set(self.normalized))
        self.assertEqual(overlay['runtime_layer_sha256'], subject.sha(self.root / 'normalized.tar'))
        self.assertEqual(overlay['payload_sha256'], {'common': subject.sha(self.root / 'source.tar')})
        self.assertEqual(overlay['paths']['usr/lib/libcommon.so.0']['file']['linkname'], 'libcommon.so.0.0.0')
        self.assertEqual(overlay['paths']['run/common/config']['file']['mode'], 0o644)
        self.assertEqual(proof['dependency_check']['packages']['common']['Version'], '1.0')
        self.assertEqual(proof['info_files']['var/lib/dpkg/status']['sha256'],
                         __import__('hashlib').sha256(self.status).hexdigest())

    def test_generated_owner_revision_reaches_installed_receipt(self):
        """Changing selected owner metadata updates source provenance, not Debian's installed version."""
        paths = self.root / 'paths.json'
        paths.write_text(json.dumps({'schema': 1, 'packages': {'common': {
            'required_paths': ['usr/lib/libcommon.so.0', 'var/run/common/config']}}}))
        module = self.root / 'MODULE.bazel'
        owner = self.root / 'owner.json'
        owner.write_text(json.dumps({'schema': 1, 'module': 'common', 'packages': {'common': {
            'runtime_target': '//:runtime', 'control_fields': {'Package': 'common', 'Version': '2.0'}}}}))
        revisions = ('a' * 40, 'b' * 40)
        for revision in revisions:
            module.write_text('module(name = "common", version = "0.0.4-' + revision + '")\n')
            generate(paths, {'@@common+//:runtime_packages.json': owner}, {'common': module},
                     {'common': '@@common+//:runtime'}, {'common': '@common//:runtime'},
                     self.root / 'spec.json', architecture='amd64', mode='provenance')
            report = self.inspect(allow_same_owner_overlays=True)
            self.assertEqual(report['source_packages'][0]['source_revision'], revision)
            self.assertEqual(report['source_packages'][0]['source_version'], '0.0.4-' + revision)
            self.assertEqual(report['dependency_check']['packages']['common']['Version'], '1.0')

    def test_excluded_baseline_file_must_be_absent_even_if_broken_symlink(self):
        spec = self.root / 'spec.json'
        document = json.loads(spec.read_text())
        path = 'usr/lib/x86_64-linux-gnu/libnl/cli/cls/basic.so'
        document['packages']['common']['excluded_baseline_paths'] = [path]
        spec.write_text(json.dumps(document))
        report = self.inspect(allow_same_owner_overlays=True)
        self.assertEqual(report['source_packages'][0]['excluded_baseline_paths'], [path])
        for value in (b'unused Debian plugin', 'missing.so'):
            with self.subTest(symlink=isinstance(value, str)):
                self.files[path] = (value, 0o644)
                with self.assertRaisesRegex(ValueError, 'excluded baseline file'):
                    self.inspect(allow_same_owner_overlays=True)

    def test_required_source_plugin_cannot_disappear_from_payload(self):
        spec = self.root / 'spec.json'
        document = json.loads(spec.read_text())
        path = 'usr/lib/x86_64-linux-gnu/libnl-3/cli/cls/basic.so'
        document['packages']['common']['required_paths'].append(path)
        spec.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, 'required runtime-layer path'):
            self.inspect(allow_same_owner_overlays=True)
        self.original[path] = self.normalized[path] = (b'source plugin', 0o644)
        report = self.inspect(allow_same_owner_overlays=True)
        self.assertIn(path, {entry['path'] for entry in report['source_packages'][0]['runtime_files']})

    def test_libnl_filter_excludes_only_reviewed_baseline_plugin_files(self):
        directory = Path(__file__).resolve().parents[3] / 'dockers/docker-config-engine-trixie/bazel'
        spec = json.loads((directory / 'source_paths.json').read_text())['packages']['libnl-cli-3-200']
        filters = [line.split()[1] for line in (directory / 'dpkg_02_source_libnl').read_text().splitlines()
                   if line.startswith('path-exclude ')]
        self.assertEqual(len(filters), 10)
        self.assertEqual(len(spec['excluded_baseline_paths']), 10)
        self.assertEqual(len(spec['required_paths']), 11)  # CLI library plus ten source plugins.
        for triplet in ('x86_64-linux-gnu', 'aarch64-linux-gnu'):
            for path in spec['excluded_baseline_paths']:
                path = '/' + path.format(multiarch=triplet)
                self.assertEqual(sum(fnmatch.fnmatchcase(path, pattern) for pattern in filters), 1)
                self.assertFalse(any(fnmatch.fnmatchcase(path + '.unrelated', pattern) for pattern in filters))
            for path in spec['required_paths']:
                path = '/' + path.format(multiarch=triplet)
                self.assertFalse(any(fnmatch.fnmatchcase(path, pattern) for pattern in filters))

    def test_other_owner_and_multiple_owners_are_rejected(self):
        common = self.files['var/lib/dpkg/info/common.list']
        for keep_common in (True, False):
            with self.subTest(multiple_owners=keep_common):
                self.files['var/lib/dpkg/info/other:amd64.list'] = common
                if not keep_common:
                    del self.files['var/lib/dpkg/info/common.list']
                with self.assertRaisesRegex(ValueError, 'Debian-owned'):
                    self.inspect(allow_same_owner_overlays=True)

    def test_development_symlink_does_not_claim_its_runtime_referent(self):
        # The development package owns libcommon.so. The runtime package owns
        # its SONAME link and real library, matching Debian's split packages.
        status = self.status + (b'Package: common-dev\nVersion: 1.0\nArchitecture: amd64\n'
                               b'Status: install ok installed\nDepends: common (= 1.0)\n'
                               b'Description: Development files\n\n')
        self.files['var/lib/dpkg/status'] = (status, 0o644)
        self.files['var/lib/dpkg/info/common-dev:amd64.list'] = (b'/lib/libcommon.so\n', 0o644)
        self.files['lib'] = ('usr/lib', 0o777)
        self.files['usr/lib/libcommon.so'] = ('libcommon.so.0', 0o777)
        development = {'usr/lib/libcommon.so': ('libcommon.so.0', 0o777)}
        specification = json.loads((self.root / 'spec.json').read_text())
        specification['packages']['common-dev'] = {'target': '//common:development',
            'source_revision': 'reviewed-source', 'required_paths': ['usr/lib/libcommon.so']}
        (self.root / 'spec.json').write_text(json.dumps(specification))
        rootfs = self.write_tar('rootfs.tar', self.files)
        runtime = self.write_tar('source.tar', self.original)
        devel = self.write_tar('devel.tar', development)
        layer = self.write_tar('combined.tar', {**self.normalized, **development})
        receipt = self.root / 'receipt.json'
        receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'amd64',
                                      'rootfs_sha256': subject.sha(rootfs)}))
        report = subject.describe(rootfs, receipt, self.root / 'spec.json', 'amd64',
            payloads={'common': runtime, 'common-dev': devel}, runtime_layer=layer,
            allow_same_owner_overlays=True)
        overlays = report['verified_overlays']['paths']
        self.assertEqual(overlays['usr/lib/libcommon.so']['owners'], ['common-dev'])
        self.assertEqual(overlays['usr/lib/libcommon.so.0']['owners'], ['common'])
        self.assertEqual(overlays['usr/lib/libcommon.so.0.0.0']['owners'], ['common'])

    def test_package_manager_state_is_never_an_overlay(self):
        for path in ('var/lib/dpkg/status', 'var/lib/dpkg/info/common.list', 'var/lib/apt/extended_states'):
            with self.subTest(path=path):
                self.normalized[path] = self.original[path] = (b'replacement', 0o644)
                with self.assertRaisesRegex(ValueError, 'package-manager state'):
                    self.inspect(allow_same_owner_overlays=True)
                del self.normalized[path], self.original[path]

    def test_changed_bytes_or_symlink_cannot_borrow_an_owners_permission(self):
        for path, value in [('usr/lib/libcommon.so.0.0.0', (b'unrelated library', 0o644)),
                            ('usr/lib/libcommon.so.0', ('unrelated.so', 0o777))]:
            with self.subTest(path=path):
                before = self.normalized[path]
                self.normalized[path] = value
                with self.assertRaisesRegex(ValueError, 'differs from its source payload'):
                    self.inspect(allow_same_owner_overlays=True)
                self.normalized[path] = before

    def test_no_exception_leaves_strict_profile_metadata_unchanged(self):
        del self.files['var/lib/dpkg/info/common.list']
        strict = self.inspect()
        self.assertNotIn('verified_overlays', strict)
        self.assertEqual(strict, self.inspect(allow_same_owner_overlays=True))

    def test_baseline_version_is_not_relabelled_as_the_source_version(self):
        path = self.root / 'spec.json'
        specification = json.loads(path.read_text())
        specification['packages']['common']['source_version'] = '2.0-sonic'
        path.write_text(json.dumps(specification))
        report = self.inspect(allow_same_owner_overlays=True)
        source = report['source_packages'][0]
        self.assertEqual(source['source_version'], '2.0-sonic')
        self.assertEqual(source['debian_baseline'], {'common': {'version': '1.0', 'architecture': 'amd64'}})
        self.assertNotIn('version', source)
        self.assertEqual(report['dependency_check']['packages']['common']['Version'], '1.0')
        self.assertEqual(report['info_files']['var/lib/dpkg/status']['sha256'],
                         __import__('hashlib').sha256(self.status).hexdigest())

    def test_source_symlinks_and_configuration_are_included_in_provenance(self):
        report = self.inspect(allow_same_owner_overlays=True)
        source = report['source_packages'][0]
        self.assertEqual(source['runtime_links'], [{'path': 'usr/lib/libcommon.so.0', 'kind': 'symlink',
                         'linkname': 'libcommon.so.0.0.0', 'mode': 0o777, 'uid': 0, 'gid': 0}])
        self.assertIn('run/common/config', {item['path'] for item in source['runtime_files']})

    def test_unowned_payload_cannot_change_bytes_or_links(self):
        del self.files['var/lib/dpkg/info/common.list']
        for name, replacement, message in (
            ('usr/lib/libcommon.so.0.0.0', (b'changed', 0o644), 'changed a runtime-layer file'),
            ('usr/lib/libcommon.so.0', ('other.so', 0o777), 'changed a runtime-layer symlink'),
        ):
            original = self.normalized[name]
            self.normalized[name] = replacement
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, message):
                self.inspect()
            self.normalized[name] = original

    def test_preferences_protect_source_package_names(self):
        specification = {'schema': 1, 'packages': {
            'libssl3t64': {},
            'ordinary': {},
        }}
        value = subject.apt_preferences(specification)
        self.assertIn('Package: libssl3t64 ordinary\nPin: version *\nPin-Priority: -1\n', value)

    def test_invalid_name_cannot_inject_an_apt_preferences_stanza(self):
        path = self.root / 'spec.json'
        specification = json.loads(path.read_text())
        specification['packages']['common\nPackage: *'] = specification['packages'].pop('common')
        path.write_text(json.dumps(specification))
        with self.assertRaisesRegex(ValueError, 'invalid source package names'):
            self.inspect(allow_same_owner_overlays=True)

    def test_debug_stage_rechecks_inherited_source_and_overlay_proof(self):
        report = self.inspect(allow_same_owner_overlays=True)
        inherited = self.root / 'inherited.json'
        inherited.write_text(json.dumps(report))
        self.files.update(self.normalized)
        self.files['usr/share/sonic/bazel/runtime.json'] = (inherited.read_bytes(), 0o644)
        rootfs = self.write_tar('debug-rootfs.tar', self.files)
        receipt = self.root / 'debug-installation.json'
        receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'amd64',
                                      'rootfs_sha256': subject.sha(rootfs), 'input_packages': []}))
        result = subject.describe(rootfs, receipt, self.root / 'spec.json', 'amd64',
            inherited_metadata=inherited, inherited_metadata_path='usr/share/sonic/bazel/runtime.json',
            payloads={'common': self.root / 'source.tar'})
        self.assertEqual(result['verified_overlays'], report['verified_overlays'])
        self.assertEqual(result['inherited_metadata_sha256'], subject.sha(inherited))

    def test_source_only_selection_retains_provenance_without_make_manifest(self):
        report = self.inspect(allow_same_owner_overlays=True)
        selection = {'schema': 1, 'variant': 'runtime', 'make_manifest_sha256': None,
                     'source_receipt_sha256': 'b' * 64, 'dependency_check': report['dependency_check']}
        path = self.root / 'selection.json'
        path.write_text(json.dumps(selection))
        result = self.inspect(allow_same_owner_overlays=True, selection=path)
        self.assertIsNone(result['make_manifest_sha256'])
        self.assertEqual(result['source_receipt_sha256'], 'b' * 64)
        selection['source_receipt_sha256'] = None
        path.write_text(json.dumps(selection))
        with self.assertRaisesRegex(ValueError, 'source_receipt_sha256'):
            self.inspect(allow_same_owner_overlays=True, selection=path)

    def test_receipt_selection_is_bound_to_real_dependency_controls(self):
        report = self.inspect(allow_same_owner_overlays=True)
        selection = {'schema': 1, 'variant': 'runtime', 'make_manifest_sha256': 'a' * 64,
                     'source_receipt_sha256': 'b' * 64, 'dependency_check': report['dependency_check']}
        path = self.root / 'selection.json'
        path.write_text(json.dumps(selection))
        result = self.inspect(allow_same_owner_overlays=True, selection=path)
        self.assertEqual(result['selection_sha256'], subject.sha(path))
        selection['dependency_check']['packages']['common']['Version'] = 'invented-source-version'
        path.write_text(json.dumps(selection))
        with self.assertRaisesRegex(ValueError, 'actual installed dependency controls'):
            self.inspect(allow_same_owner_overlays=True, selection=path)


if __name__ == '__main__':
    unittest.main(argv=[__import__('sys').argv[0]])
