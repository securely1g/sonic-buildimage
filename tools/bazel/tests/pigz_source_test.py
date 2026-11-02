"""The transport mirror must never change Bazel's selected source identity."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.gzip import prepare_pigz as source


class PigzSourceTest(unittest.TestCase):
    def test_rejects_changed_bytes_without_replacing_existing_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "MODULE.bazel"
            module.write_text('bazel_dep(name="pigz", version="2.8.bcr.2")')
            archive = root / "pigz-2.8.tar.gz"
            archive.write_bytes(b"existing source")
            with self.assertRaisesRegex(ValueError, "unchanged BCR SHA256"):
                source.prepare(module, root, fetch=lambda _: b"wrong response")
            self.assertEqual(archive.read_bytes(), b"existing source")

    def test_reuses_only_verified_bytes_and_publishes_atomically(self):
        data = b"source fixture"
        with tempfile.TemporaryDirectory() as temporary, patch.object(
                source, "SHA256", hashlib.sha256(data).hexdigest()):
            root = Path(temporary)
            module = root / "MODULE.bazel"
            module.write_text('bazel_dep(name="pigz", version="2.8.bcr.2")')
            first = source.prepare(module, root, fetch=lambda _: data)
            self.assertFalse(first["reused_verified_archive"])
            self.assertEqual((root / "pigz-2.8.tar.gz").stat().st_mode & 0o777, 0o644)
            def unexpected_fetch(_):
                self.fail("verified archive should be reused")
            self.assertTrue(source.prepare(module, root, fetch=unexpected_fetch)["reused_verified_archive"])

    def test_changed_selection_fails_before_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "MODULE.bazel"
            module.write_text('bazel_dep(name="pigz", version="2.9")')
            with self.assertRaisesRegex(ValueError, "selection changed"):
                source.prepare(module, root, fetch=lambda _: self.fail("unexpected fetch"))


if __name__ == "__main__":
    unittest.main()
