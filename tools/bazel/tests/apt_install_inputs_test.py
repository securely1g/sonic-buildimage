"""Check that real installation keeps SONiC's reviewed package selection."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from tools.bazel.oci.apt_install_inputs import selected_archives


class SelectedArchivesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / "selected.archive"
        self.archive.write_bytes(b"archive identity used by this unit test")
        self.item = {
            "key": "/trixie/example:amd64=1",
            "source_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        }
        self.packages = {self.item["key"]: str(self.archive)}

    def test_only_selected_packages_are_installed(self):
        # An already-satisfied base dependency is declared but not selected.
        # It must not enter the transaction or overwrite the base package.
        self.packages["/trixie/libssl3t64:amd64=older"] = "not-selected.archive"
        self.assertEqual(
            selected_archives({"selected": [self.item]}, self.packages),
            [str(self.archive)],
        )

    def test_changed_archive_is_rejected(self):
        self.archive.write_bytes(b"different archive")
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            selected_archives({"selected": [self.item]}, self.packages)

    def test_undeclared_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no declared Debian archive"):
            selected_archives({"selected": [self.item]}, {})

    def test_duplicate_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate selected package"):
            selected_archives({"selected": [self.item, self.item]}, self.packages)

    def test_missing_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "selected list"):
            selected_archives({}, self.packages)

    def test_empty_selection_is_allowed(self):
        self.assertEqual(selected_archives({"selected": []}, self.packages), [])

    def test_source_dependencies_join_the_selected_apt_transaction(self):
        source = Path(self.directory.name) / "source.deb"
        source.write_bytes(b"declared source package")
        self.packages["/trixie/libssl3t64:amd64=older"] = "not-selected.archive"
        self.assertEqual(selected_archives({"selected": [self.item]}, self.packages, [str(source)]),
                         [str(self.archive), str(source)])

    def test_duplicate_or_missing_additional_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "same Debian archive"):
            selected_archives({"selected": [self.item]}, self.packages, [str(self.archive)])
        with self.assertRaisesRegex(ValueError, "invalid additional"):
            selected_archives({"selected": []}, self.packages, ["missing.deb"])


if __name__ == "__main__":
    unittest.main()
