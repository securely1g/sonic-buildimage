#!/usr/bin/env python3
"""Check the APT refresh action audit without invoking Bazel or creating DEBs."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

OWNER = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(OWNER / "bazel"))
import refresh_apt_lock as subject


class RefreshAptLockTest(unittest.TestCase):
    def audit(self, output, command):
        with tempfile.TemporaryDirectory(prefix="syncd-aquery-test-") as temporary:
            path = Path(temporary) / "actions.json"
            path.write_text(json.dumps({
                "pathFragments": [{"id": 1, "label": output}],
                "artifacts": [{"id": 1, "pathFragmentId": 1}],
                "actions": [{"mnemonic": "Genrule", "outputIds": [1], "arguments": ["/bin/sh", "-c", command]}],
            }))
            return subject.inspect_actions(path)

    def test_tar_only_action_is_clear(self):
        result = self.audit("content.tar.gz", "zstd -n < data.tar > content.tar.gz")
        self.assertEqual(result["deb_outputs"], [])
        self.assertEqual(result["packaging_wrappers"], [])

    def test_deb_output_and_make_wrapper_are_reported(self):
        result = self.audit("fixture.deb", "make package")
        self.assertEqual(result["deb_outputs"], ["fixture.deb"])
        self.assertEqual(len(result["packaging_wrappers"]), 1)

    def test_dpkg_deb_build_is_reported_but_extraction_is_clear(self):
        result = self.audit("content.tar", "dpkg-deb --build source fixture.deb")
        self.assertEqual(len(result["packaging_wrappers"]), 1)
        result = self.audit("content.tar", "dpkg-deb --fsys-tarfile existing.deb > content.tar")
        self.assertEqual(result["packaging_wrappers"], [])


if __name__ == "__main__":
    unittest.main()
