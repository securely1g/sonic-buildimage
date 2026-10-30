#!/usr/bin/env python3
"""Check cleanup of Docker's private data-root bind mount."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import import_store


class ImportCleanupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "private-store"
        (self.root / "data").mkdir(parents=True)
        (self.root / "data/payload").write_text("retained until safe to delete")

    def test_unmounts_only_owned_data_root_before_removing_it(self):
        def command(arguments, **kwargs):
            self.assertTrue((self.root / "data/payload").is_file())
            return subprocess.CompletedProcess(arguments, 0)

        with mock.patch.object(import_store.subprocess, "run", side_effect=command) as run:
            import_store.remove_store_scratch(self.root)
        self.assertEqual(run.call_args_list, [
            mock.call(["mountpoint", "--quiet", str(self.root / "data")]),
            mock.call(["umount", "--", str(self.root / "data")], check=True),
        ])
        self.assertFalse(self.root.exists())

    def test_unmounted_store_is_removed_without_umount(self):
        with mock.patch.object(import_store.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 32)) as run:
            import_store.remove_store_scratch(self.root)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.root.exists())

    def test_failed_unmount_preserves_store_and_fails(self):
        with mock.patch.object(import_store.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(32, "umount"),
        ]):
            with self.assertRaises(subprocess.CalledProcessError):
                import_store.remove_store_scratch(self.root)
        self.assertTrue((self.root / "data/payload").is_file())

    def test_mount_probe_failure_does_not_remove_store(self):
        with mock.patch.object(import_store.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaisesRegex(RuntimeError, "mount state"):
                import_store.remove_store_scratch(self.root)
        self.assertEqual(run.call_count, 1)
        self.assertTrue((self.root / "data/payload").is_file())

    def test_early_daemon_failure_without_data_directory(self):
        (self.root / "data/payload").unlink()
        (self.root / "data").rmdir()
        with mock.patch.object(import_store.subprocess, "run") as run:
            import_store.remove_store_scratch(self.root)
        run.assert_not_called()
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
