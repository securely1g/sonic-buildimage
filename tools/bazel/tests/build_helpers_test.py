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


if __name__ == "__main__":
    unittest.main()
