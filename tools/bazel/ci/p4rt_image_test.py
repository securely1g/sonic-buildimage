"""Keep private test-runtime materialization faithful to the declared TARs."""

from pathlib import Path
import tempfile
import os
import subprocess
from unittest.mock import patch
from types import SimpleNamespace
import unittest

from tools.bazel.ci import p4rt_image as image


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
