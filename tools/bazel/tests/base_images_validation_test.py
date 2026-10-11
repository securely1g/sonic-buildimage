"""Reject assembled images that differ from their source package receipts."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.base_images import verify_source_files


class SourceImageValidationTest(unittest.TestCase):
    def setUp(self):
        self.path = "usr/bin/source-program"
        self.item = {"path": self.path, "sha256": "a" * 64, "mode": 0o755, "uid": 0, "gid": 0}
        self.report = {"source_packages": [{"package": "source-component", "native_files": [self.item]}]}
        self.files = {self.path: {**self.item, "elf_machine": 62}}

    def test_installed_file_matches_source_identity(self):
        native = verify_source_files(self.files, [self.report], 62)
        self.assertEqual(native[self.path]["source_package"], "source-component")

    def test_later_layer_cannot_replace_source_elf_or_metadata(self):
        for field, value in (("sha256", "b" * 64), ("mode", 0o644), ("uid", 1000), ("gid", 1000)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "changed source"):
                verify_source_files({self.path: {**self.files[self.path], field: value}}, [self.report], 62)

    def test_native_image_cannot_contain_foreign_architecture(self):
        with self.assertRaisesRegex(ValueError, "architecture"):
            verify_source_files(self.files, [self.report], 183)

    def test_distinct_owners_cannot_silently_overwrite_each_other(self):
        with self.assertRaisesRegex(ValueError, "two source owners"):
            verify_source_files(self.files, [self.report, self.report], 62)


if __name__ == "__main__":
    unittest.main()
