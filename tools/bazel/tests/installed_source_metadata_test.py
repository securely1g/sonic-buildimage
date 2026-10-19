"""Reject stale installation receipts and edited source files before OCI assembly."""
import io
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from tools.bazel.oci import installed_source_metadata as subject
except ModuleNotFoundError as error:
    if error.name != 'sonic_apt':
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
                    subject.describe(filesystem, receipt, root / 'spec.json', {}, 'amd64', 'bsdtar')

    def test_wrong_architecture_is_rejected_even_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filesystem = root / 'rootfs.tar'
            filesystem.write_bytes(b'wrong architecture')
            receipt = root / 'receipt.json'
            receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'arm64',
                                          'rootfs_sha256': subject.sha(filesystem)}))
            with self.assertRaisesRegex(ValueError, 'receipt does not match'):
                subject.describe(filesystem, receipt, root / 'spec.json', {}, 'amd64', 'bsdtar')



# Each positive/negative case uses one original DEB made by the upstream
# archive tool and an independently assembled installed-filesystem fixture.
@unittest.skipIf(subject is None, 'Run the Bazel target with its declared sonic_apt dependency')
class InstalledFilesTest(unittest.TestCase):
    def setUp(self):
        import sys
        if not TAR:
            self.skipTest("Run the Bazel target with its declared BSD tar tool")
        tool = Path(TAR)
        if not tool.exists() and str(tool).startswith("external/"):
            tool = Path("..") / str(tool).removeprefix("external/")
        self.tar = str(tool.absolute())
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.elf = bytearray(64)
        self.elf[:6] = b'\x7fELF\x02\x01'
        self.elf[18:20] = (62).to_bytes(2, 'little')
        self.elf = bytes(self.elf)
        self.control = b'Package: example\nVersion: 1.0\nArchitecture: amd64\nMaintainer: Test <test@example.invalid>\nDescription: Test program\n'
        self.write_tar('control.tar', {'control': (self.control, 0o644)})
        self.write_tar('data.tar', {'usr/bin/example': (self.elf, 0o755)})
        (self.root / 'debian-binary').write_bytes(b'2.0\n')
        import subprocess
        subprocess.run([self.tar, '--format=ar', '-cf', str(self.root / 'example.deb'),
                        '-C', str(self.root), 'debian-binary', 'control.tar', 'data.tar'], check=True)
        self.spec = self.root / 'spec.json'
        self.spec.write_text(json.dumps({'schema': 1, 'packages': {'example': {
            'target': '//owner:package', 'source_revision': 'reviewed-source',
            'required_paths': ['usr/bin/example']}}}))

    def tearDown(self):
        self.temporary.cleanup()

    def write_tar(self, name, entries):
        import tarfile
        with tarfile.open(self.root / name, 'w') as stream:
            for path, (data, mode) in entries.items():
                info = tarfile.TarInfo(path)
                info.mode, info.size = mode, len(data)
                stream.addfile(info, io.BytesIO(data))

    def inspect(self, installed_elf, *, receipt_package=True, inherited=None, embedded=None, selection=None):
        status = self.control + b'Status: install ok installed\n\n'
        entries = {'usr/bin/example': (installed_elf, 0o2755), 'var/lib/dpkg/status': (status, 0o644)}
        if embedded is not None:
            entries['usr/share/sonic/bazel/runtime.json'] = (embedded, 0o644)
        self.write_tar('rootfs.tar', entries)
        receipt = self.root / 'receipt.json'
        receipt.write_text(json.dumps({'status': 'passed', 'architecture': 'amd64',
            'rootfs_sha256': subject.sha(self.root / 'rootfs.tar'),
            'input_packages': [{'sha256': subject.sha(self.root / 'example.deb')}] if receipt_package else []}))
        selection_path = None
        if selection is not None:
            selection_path = self.root / 'selection.json'
            selection_path.write_text(json.dumps(selection))
        return subject.describe(self.root / 'rootfs.tar', receipt, self.spec,
                                {'example': str(self.root / 'example.deb')}, 'amd64', self.tar,
                                inherited, 'usr/share/sonic/bazel/runtime.json' if inherited else None,
                                selection_path)

    def selection(self):
        check = self.inspect(self.elf)['dependency_check']
        # The selection describes the earlier base; the installed receipt must
        # keep its own current inventory hash rather than copy this one.
        check['base_package_inventory_sha256'] = '0' * 64
        # Match the actual selection receipt: architecture is in its controls.
        return {'schema': 1, 'variant': 'runtime',
                'make_manifest_sha256': 'a' * 64, 'source_receipt_sha256': 'b' * 64,
                'dependency_check': check}

    def test_runtime_selection_preserves_only_verified_provenance(self):
        selection = self.selection()
        result = self.inspect(self.elf, selection=selection)
        for field in ('variant', 'make_manifest_sha256', 'source_receipt_sha256'):
            self.assertEqual(result[field], selection[field])
        self.assertEqual(result['selection_sha256'], subject.sha(self.root / 'selection.json'))
        self.assertNotEqual(result['dependency_check']['base_package_inventory_sha256'], '0' * 64)

    def test_runtime_selection_rejects_different_dependency_controls(self):
        original = self.selection()
        for field, value in (('Version', '2.0'), ('Depends', 'example (>= 2.0)'),
                             ('Architecture', 'arm64'), ('Multi-Arch', 'foreign')):
            with self.subTest(field=field):
                selection = copy.deepcopy(original)
                selection['dependency_check']['packages']['example'][field] = value
                with self.assertRaisesRegex(ValueError, 'actual installed dependency controls'):
                    self.inspect(self.elf, selection=selection)
        for packages in ({}, {**original['dependency_check']['packages'], 'unexpected': {}}):
            selection = copy.deepcopy(original)
            selection['dependency_check']['packages'] = packages
            with self.assertRaisesRegex(ValueError, 'actual installed dependency controls'):
                self.inspect(self.elf, selection=selection)

    def test_runtime_selection_rejects_invalid_identity_and_provenance(self):
        original = self.selection()
        for field, value in (('schema', 2), ('architecture', 'arm64'), ('variant', 'debug'),
                             ('make_manifest_sha256', None), ('source_receipt_sha256', 'bad')):
            with self.subTest(field=field):
                selection = copy.deepcopy(original)
                selection[field] = value
                with self.assertRaisesRegex(ValueError, 'runtime package selection'):
                    self.inspect(self.elf, selection=selection)
        selection = copy.deepcopy(original)
        selection['dependency_check']['status'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'actual installed dependency controls'):
            self.inspect(self.elf, selection=selection)

    def test_records_actual_configured_permissions_and_dpkg_status(self):
        result = self.inspect(self.elf)
        self.assertEqual(result['source_packages'][0]['native_files'][0]['mode'], 0o2755)
        self.assertIn('var/lib/dpkg/status', result['info_files'])

    def test_rejects_modified_source_elf(self):
        with self.assertRaisesRegex(ValueError, 'changed a source ELF'):
            self.inspect(self.elf + b'changed')

    def test_rejects_package_absent_from_installation_inputs(self):
        with self.assertRaisesRegex(ValueError, 'not an installation input'):
            self.inspect(self.elf, receipt_package=False)

    def test_debug_stage_rechecks_sources_from_embedded_runtime_proof(self):
        inherited = self.root / 'inherited.json'
        inherited.write_text(json.dumps(self.inspect(self.elf)))
        result = self.inspect(self.elf, receipt_package=False, inherited=inherited, embedded=inherited.read_bytes())
        self.assertEqual(result['inherited_metadata_sha256'], subject.sha(inherited))
        with self.assertRaisesRegex(ValueError, 'changed a source ELF'):
            self.inspect(self.elf + b'changed', receipt_package=False, inherited=inherited, embedded=inherited.read_bytes())

    def test_debug_stage_rejects_unrelated_runtime_proof(self):
        inherited = self.root / 'inherited.json'
        inherited.write_text(json.dumps(self.inspect(self.elf)))
        with self.assertRaisesRegex(ValueError, 'inherited source metadata differs'):
            self.inspect(self.elf, receipt_package=False, inherited=inherited, embedded=b'other proof')


    def test_runtime_layers_keep_source_provenance_without_debian_identity(self):
        self.inspect(self.elf)
        spec = json.loads(self.spec.read_text())
        spec['packages']['listener'] = {'target': '//listener:runtime', 'source_revision': 'reviewed',
                                        'required_paths': ['usr/bin/listener']}
        self.spec.write_text(json.dumps(spec))
        self.write_tar('listener.tar', {'usr/bin/listener': (self.elf, 0o755)})
        result = subject.describe(self.root / 'rootfs.tar', self.root / 'receipt.json', self.spec,
            {'example': str(self.root / 'example.deb')}, 'amd64', self.tar,
            payloads={'listener': self.root / 'listener.tar'}, runtime_layer=self.root / 'listener.tar')
        listener = next(item for item in result['source_packages'] if item['package'] == 'listener')
        self.assertEqual(listener['packaging'], 'runtime-layer')
        self.assertNotIn('deb_sha256', listener)
        self.assertNotIn('listener', result['dependency_check']['packages'])
        self.assertEqual(listener['runtime_files'][0]['path'], 'usr/bin/listener')

    def test_runtime_layers_cannot_overwrite_debian_files_or_state(self):
        self.inspect(self.elf)
        status = self.control + b'Status: install ok installed\n\n'
        self.write_tar('rootfs.tar', {'var/lib/dpkg/status': (status, 0o644),
            'var/lib/dpkg/info/example.list': (b'/usr/bin/example\n', 0o644),
            'usr/bin/example': (self.elf, 0o755)})
        receipt = json.loads((self.root / 'receipt.json').read_text())
        receipt['rootfs_sha256'] = subject.sha(self.root / 'rootfs.tar')
        (self.root / 'receipt.json').write_text(json.dumps(receipt))
        for name, message in [('usr/bin/example', 'Debian-owned'), ('var/lib/dpkg/status', 'package-manager state')]:
            self.write_tar('layer.tar', {name: (b'override', 0o644)})
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, message):
                subject.describe(self.root / 'rootfs.tar', self.root / 'receipt.json', self.spec,
                    {'example': str(self.root / 'example.deb')}, 'amd64', self.tar,
                    runtime_layer=self.root / 'layer.tar')

    def test_runtime_layer_whiteouts_are_rejected(self):
        self.inspect(self.elf)
        self.write_tar('layer.tar', {'var/lib/dpkg/.wh.status': (b'', 0o644)})
        with self.assertRaisesRegex(ValueError, 'must not contain OCI whiteouts'):
            subject.describe(self.root / 'rootfs.tar', self.root / 'receipt.json', self.spec,
                {'example': str(self.root / 'example.deb')}, 'amd64', self.tar,
                runtime_layer=self.root / 'layer.tar')

    def test_runtime_layer_source_bytes_must_match_the_composed_layer(self):
        self.inspect(self.elf)
        spec = json.loads(self.spec.read_text())
        spec['packages']['listener'] = {'target': '//listener:runtime', 'source_revision': 'reviewed',
                                        'required_paths': ['usr/bin/listener']}
        self.spec.write_text(json.dumps(spec))
        self.write_tar('listener.tar', {'usr/bin/listener': (self.elf, 0o755)})
        self.write_tar('changed.tar', {'usr/bin/listener': (self.elf + b'changed', 0o755)})
        with self.assertRaisesRegex(ValueError, 'changed a runtime-layer file'):
            subject.describe(self.root / 'rootfs.tar', self.root / 'receipt.json', self.spec,
                {'example': str(self.root / 'example.deb')}, 'amd64', self.tar,
                payloads={'listener': self.root / 'listener.tar'}, runtime_layer=self.root / 'changed.tar')

@unittest.skipIf(subject is None, 'Run with the declared sonic_apt dependency')
class MakeBaseOverlayTest(unittest.TestCase):
    """An existing Make-installed package keeps its controls during a TAR overlay."""
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
        return subject.describe(rootfs, receipt, self.root / 'spec.json', {}, 'amd64', 'unused',
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

    def test_other_owner_and_multiple_owners_are_rejected(self):
        common = self.files['var/lib/dpkg/info/common.list']
        for keep_common in (True, False):
            with self.subTest(multiple_owners=keep_common):
                self.files['var/lib/dpkg/info/other:amd64.list'] = common
                if not keep_common:
                    del self.files['var/lib/dpkg/info/common.list']
                with self.assertRaisesRegex(ValueError, 'Debian-owned'):
                    self.inspect(allow_same_owner_overlays=True)

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


if __name__ == '__main__':
    TAR = __import__('sys').argv[1]
    unittest.main(argv=[__import__('sys').argv[0]])
