"""Keep private test-runtime materialization faithful to the declared TARs."""

from pathlib import Path
import io
import json
import struct
import tarfile
import tempfile
import os
import subprocess
from unittest.mock import patch
from types import SimpleNamespace
import unittest

from tools.bazel.ci import p4rt_image as image
from tools.bazel import p4rt_common_sdk as sdk


class SdkTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.elf = b'\x7fELF\x02\x01' + bytes(12) + struct.pack('<H', 62)
        self.common = self.archive('common.tar', {
            'lib/libswsscommon.so.0': self.elf,
            'include/swss/dbconnector.h': b'// database',
            'include/swss/cfg_schema.h': b'// schema',
        })
        self.members = {'usr/include/libnl3/netlink/netlink.h': b'// source-owned header'}
        for stem in sdk.LIBNL_STEMS:
            name = 'usr/lib/x86_64-linux-gnu/' + stem
            self.members[name + '.so.200.26.0'] = self.elf + stem.encode()
            self.members[name + '.so.200'] = stem + '.so.200.26.0'
        self.parent = self.archive('parent.tar', self.members)
        self.inputs = {'@libnl3//:libnl-nf-3-dev_pkg': self.archive('nf.tar', self.members),
                       '@libnl3//:libnl-genl-3-dev_pkg': self.archive('genl.tar', self.members)}
        self.output = self.root / 'sdk'

    def archive(self, name, members):
        path = self.root / name
        with tarfile.open(path, 'w') as archive:
            for name, contents in members.items():
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                if isinstance(contents, str):
                    member.type, member.linkname = tarfile.SYMTYPE, contents
                    archive.addfile(member)
                else:
                    member.size = len(contents)
                    archive.addfile(member, io.BytesIO(contents))
        return path

    def prepare(self):
        sdk.prepare_repository(self.common, self.output, 'amd64', {},
                               libnl_archives=self.inputs, parent_archive=self.parent)

    def test_declared_libraries_headers_links_and_parent_identity(self):
        self.prepare()
        self.assertIn('_DECLARED_LIBNL = True', (self.output / 'BUILD.bazel').read_text())
        for stem in sdk.LIBNL_STEMS:
            library = self.output / ('lib/' + stem + '.so.200')
            self.assertTrue(library.is_symlink())
            self.assertEqual(library.read_bytes(), self.elf + stem.encode())
        self.assertEqual((self.output / 'include/libnl3/netlink/netlink.h').read_bytes(),
                         self.members['usr/include/libnl3/netlink/netlink.h'])
        receipt = json.loads((self.output / 'sdk-build.json').read_text())['libnl']
        self.assertEqual(receipt['parent_source_tar_sha256'], image.check.sha(self.parent))
        self.assertEqual(receipt['producer_targets'], {key: image.check.sha(path) for key, path in self.inputs.items()})

    def test_legacy_sdk_keeps_original_dependency_contract(self):
        sdk.prepare_repository(self.common, self.output, 'amd64', {})
        self.assertIn('_DECLARED_LIBNL = False', (self.output / 'BUILD.bazel').read_text())
        self.assertNotIn('libnl', json.loads((self.output / 'sdk-build.json').read_text()))

    def test_flattened_relative_soname_prefix_is_normalized(self):
        flattened = {'./' + name: './' + value if isinstance(value, str) else value
                     for name, value in self.members.items()}
        self.archive('nf.tar', flattened)
        self.prepare()
        for stem in sdk.LIBNL_STEMS:
            self.assertEqual((self.output / ('lib/' + stem + '.so.200')).readlink().as_posix(),
                             stem + '.so.200.26.0')

    def test_conflicting_header_does_not_replace_existing_sdk(self):
        self.prepare()
        before = (self.output / 'sdk-build.json').read_bytes()
        changed = dict(self.members, **{'usr/include/libnl3/netlink/netlink.h': b'wrong header'})
        self.archive('genl.tar', changed)
        with self.assertRaisesRegex(ValueError, 'Conflicting libnl SDK member'):
            self.prepare()
        self.assertEqual((self.output / 'sdk-build.json').read_bytes(), before)

    def test_parent_with_different_library_is_rejected(self):
        changed = dict(self.members, **{'usr/lib/x86_64-linux-gnu/libnl-3.so.200.26.0': self.elf + b'wrong'})
        self.archive('parent.tar', changed)
        with self.assertRaisesRegex(ValueError, 'differs from the selected parent runtime'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_escaping_soname_and_incomplete_sdk_are_rejected(self):
        changed = dict(self.members, **{'usr/lib/x86_64-linux-gnu/libnl-3.so.200': '/tmp/ambient.so'})
        self.archive('nf.tar', changed)
        with self.assertRaisesRegex(ValueError, 'Unexpected libnl SONAME link'):
            self.prepare()
        self.inputs = {'@libnl3//:incomplete': self.archive('incomplete.tar', {})}
        with self.assertRaisesRegex(ValueError, 'Incomplete libnl SDK'):
            self.prepare()


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = self.root / 'usr/lib/libfixture.so.1.0'
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(b'\x7fELFdeclared fixture library')
        self.alias = self.library.with_name('libfixture.so.1')
        self.alias.symlink_to(self.library.name)
        self.selected = {
            'usr/lib/libfixture.so.1.0': {'kind': 'file', 'elf_machine': 62,
                                        'sha256': image.check.sha(self.library)},
            'usr/lib/libfixture.so.1': {'kind': 'symlink', 'linkname': self.library.name},
        }

    def test_accepts_real_payload_and_declared_soname_alias(self):
        actual = image.runtime_libraries(self.root, self.selected)
        self.assertEqual(set(actual), {str(self.library), str(self.alias)})
        self.assertEqual(set(actual.values()), {image.check.sha(self.library)})

    def test_loader_report_hashes_actual_resolved_files(self):
        libraries = image.runtime_libraries(self.root, self.selected)
        output = f'\t{self.alias.name} => {self.alias} (0x1234)\n'
        self.assertEqual(image.loaded_libraries(output, libraries),
                         {self.alias.name: image.check.sha(self.library)})

    def test_loader_report_rejects_changed_or_undeclared_libraries(self):
        libraries = image.runtime_libraries(self.root, self.selected)
        for output in (f'\tlibundeclared.so.1 => {self.alias} (0x1234)\n',
                       f'\t{self.alias.name} => {self.alias} (0x1234)\n'):
            with self.subTest(output=output):
                if 'libundeclared' not in output:
                    self.library.write_bytes(b'\x7fELFwrong runtime')
                with self.assertRaisesRegex(ValueError, 'resolved an ambient library'):
                    image.loaded_libraries(output, libraries)

    def test_unrecorded_or_changed_library_is_rejected(self):
        extra = self.library.with_name('libambient.so')
        extra.write_bytes(b'\x7fELFambient library')
        with self.assertRaisesRegex(ValueError, 'differ from the declared'):
            image.runtime_libraries(self.root, self.selected)
        extra.unlink()
        self.library.write_bytes(b'\x7fELFwrong version')
        with self.assertRaisesRegex(ValueError, 'differ from the declared'):
            image.runtime_libraries(self.root, self.selected)

    def test_make_cache_options_preserve_native_platform_and_quoted_arguments(self):
        with patch.dict(os.environ, {'BAZEL_CONTAINER_CACHE_DIR': str(self.root / 'cache'),
                'BAZEL_CONTAINER_ARGS': '--jobs=1 --define="fixture=value with spaces" --platforms=other'}, clear=True):
            options = image.root_options()
        self.assertIn('--repository_cache=' + str(self.root / 'cache/repository_cache'), options)
        self.assertIn('--disk_cache=' + str(self.root / 'cache/disk_cache'), options)
        self.assertIn('--define=fixture=value with spaces', options)
        self.assertEqual([value for value in options if value.startswith('--jobs=')][-1], '--jobs=1')
        self.assertEqual([value for value in options if value.startswith('--platforms=')][-1], image.OPTIONS[-2])

    def test_make_legacy_fallback_and_empty_container_override(self):
        with patch.dict(os.environ, {'BAZEL_SWSS_ARGS': '--jobs=3'}, clear=True):
            self.assertIn('--jobs=3', image.root_options())
        with patch.dict(os.environ, {'BAZEL_SWSS_ARGS': '--jobs=3', 'BAZEL_CONTAINER_ARGS': ''}, clear=True):
            self.assertNotIn('--jobs=3', image.root_options())

    def test_clean_checkout_renders_manifests_before_the_first_owner_graph_audit(self):
        events = []
        args = SimpleNamespace(directory=self.root / 'artifacts', owner_source=self.root / 'owner',
                               owner_revision='a' * 40, bazel='root-bazel')
        def execute(command, *unused, **keywords):
            if '--version' in command:
                return 'bazel 8.5.1\n'
            self.assertEqual(command[:3], ['make', '-f', 'tools/bazel/prepare_manifests.mk'])
            self.assertIn('MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-p4rt.mk', command)
            events.append('manifests')
        def audit(*unused, **keywords):
            self.assertEqual(events, ['manifests'], 'A fresh graph cannot load before metadata exists')
            events.append('audit')
            raise RuntimeError('stop before any compilation')
        with patch.object(image, 'native'), patch.object(image, 'source_identity', return_value='b' * 40), \
                patch.object(image.p4rt_handoff, 'source_identity'), \
                patch.object(image.command_log, 'execute', side_effect=execute), \
                patch.object(image.bazel_commands, 'audit_targets', side_effect=audit):
            with self.assertRaisesRegex(RuntimeError, 'stop before any compilation'):
                image.prepare_owner(args)
        self.assertEqual(events, ['manifests', 'audit'])

    def test_runtime_link_cannot_escape_private_root(self):
        outside = self.root.parent / (self.root.name + '-outside.so')
        outside.write_bytes(b'\x7fELFoutside library')
        self.addCleanup(outside.unlink)
        self.alias.unlink()
        self.alias.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes runtime root'):
            image.runtime_libraries(self.root, self.selected)

    def test_existing_make_and_ci_evidence_can_coexist(self):
        with patch.object(image, 'ROOT', self.root), patch.object(image.subprocess, 'run') as run, \
                patch.object(image.subprocess, 'check_output', side_effect=[
                    b'artifacts/vs/timing.json\0artifacts/archive/revision.json\0', 'a' * 40 + '\n']):
            self.assertEqual(image.source_identity(self.root / 'artifacts/p4rt'), 'a' * 40)
        run.assert_called_once_with(['git', 'diff', '--exit-code', '--ignore-submodules=none', 'HEAD'],
                                    cwd=self.root, check=True)

    def test_untracked_source_remains_rejected(self):
        with patch.object(image, 'ROOT', self.root), patch.object(image.subprocess, 'run'), \
                patch.object(image.subprocess, 'check_output', return_value=b'artifacts/vs/timing.json\0src/untracked.cc\0'):
            with self.assertRaisesRegex(ValueError, 'undeclared inputs'):
                image.source_identity(self.root / 'artifacts/p4rt')

    def test_tracked_changes_fail_before_evidence_exemption(self):
        with patch.object(image, 'ROOT', self.root), \
                patch.object(image.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'git diff')), \
                patch.object(image.subprocess, 'check_output') as read:
            with self.assertRaises(subprocess.CalledProcessError):
                image.source_identity(self.root / 'artifacts/p4rt')
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
