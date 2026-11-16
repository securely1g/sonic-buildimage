"""Check shared archive publication and optional cache settings."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel import build_helpers


class BuildHelpersTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "target/container.gz"
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"previous image")

    def test_failed_copy_preserves_previous_image(self):
        """Keep the last good image and remove staging files when archive copying fails."""
        source = self.root / "source"
        source.write_bytes(b"new image")
        with patch.object(build_helpers.shutil, "copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                build_helpers.export_archive(source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"previous image")
        self.assertEqual(list(self.destination.parent.glob("container.gz.*")), [])

    def test_unchanged_archive_keeps_timestamp(self):
        """Avoid invalidating downstream Make targets when the exported archive bytes are
        unchanged.
        """
        source = self.root / "source"
        source.write_bytes(self.destination.read_bytes())
        before = self.destination.stat().st_mtime_ns
        build_helpers.export_archive(source, self.destination)
        self.assertEqual(self.destination.stat().st_mtime_ns, before)

    def test_optional_cache_does_not_change_normal_bazel_defaults(self):
        """Leave Bazel cache and output settings untouched when no shared cache is requested."""
        self.assertEqual(build_helpers.cache_options(None), [])
        self.assertEqual(build_helpers.cache_options(""), [])

    def test_legacy_cache_still_creates_repository_and_disk_directories(self):
        root = self.root / "legacy-cache"
        self.assertEqual(build_helpers.cache_options(root), [
            f"--repository_cache={root / 'repository_cache'}",
            f"--disk_cache={root / 'disk_cache'}",
        ])
        self.assertTrue((root / "repository_cache").is_dir())
        self.assertTrue((root / "disk_cache").is_dir())

    def test_remote_only_working_storage_preserves_existing_disk_cache(self):
        root = self.root / "remote-working"
        self.assertEqual(build_helpers.cache_options(root, remote_only=True), [
            f"--repository_cache={root / 'repository_cache'}",
        ])
        self.assertTrue((root / "repository_cache").is_dir())
        self.assertFalse((root / "disk_cache").exists())
        disk = root / "disk_cache"
        disk.mkdir()
        sentinel = disk / "unrelated-existing-cache-entry"
        sentinel.write_bytes(b"preserve existing caches")
        before = sentinel.stat()
        build_helpers.cache_options(root, remote_only=True)
        after = sentinel.stat()
        self.assertEqual(sentinel.read_bytes(), b"preserve existing caches")
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))

    def test_required_remote_helper_must_be_executable(self):
        with patch.dict(build_helpers.os.environ, {"BAZEL_REMOTE_AUTH": "fixture-presence"}):
            with patch.object(build_helpers.credentials, "authorization", return_value="fixture"):
                with patch.object(build_helpers.os, "access", return_value=False):
                    with self.assertRaisesRegex(ValueError, "Bazel remote cache access failed"):
                        build_helpers.remote_cache_options(required=True)


if __name__ == "__main__":
    unittest.main()
