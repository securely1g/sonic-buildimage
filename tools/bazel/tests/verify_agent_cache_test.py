"""Keep cache verification strict about disk hits and source invalidation."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.ci.verify_agent_cache import ACTIONS, action_evidence, archive_output, remove_temporary_work, verify


class CacheEvidenceTest(unittest.TestCase):
    """Validate synthetic cache evidence and safe removal of temporary build state."""

    def evidence(self, hit, runner, digest):
        return {"actions": {name: {"cache_hit": hit, "runner": runner} for name in ACTIONS},
                "archive_sha256": digest}

    def test_valid_disk_hits_and_source_invalidation(self):
        """Accept cold misses, unchanged disk hits and changed-source misses in receipts."""
        verify(self.evidence(False, "processwrapper-sandbox", "same"),
               self.evidence(True, "disk cache hit", "same"),
               self.evidence(False, "processwrapper-sandbox", "changed"))

    def test_local_action_reuse_does_not_prove_cross_checkout_cache(self):
        """Reject a local action-cache hit as evidence of shared disk-cache reuse."""
        with self.assertRaisesRegex(ValueError, "shared disk cache"):
            verify(self.evidence(False, "sandbox", "same"),
                   self.evidence(True, "local cache hit", "same"),
                   self.evidence(False, "sandbox", "changed"))

    def test_cache_cannot_hide_source_change(self):
        """Reject a changed-source receipt that still reports a cache hit."""
        with self.assertRaisesRegex(ValueError, "old output"):
            verify(self.evidence(False, "sandbox", "same"),
                   self.evidence(True, "disk cache hit", "same"),
                   self.evidence(True, "disk cache hit", "changed"))

    def test_multiline_execution_log_and_file_uri(self):
        """Read multiline action records and decode spaces in an archive's file URI."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.json"
            path.write_text("\n".join(json.dumps({"mnemonic": name, "targetLabel": label,
                                                 "cacheHit": True, "runner": "disk cache hit"}, indent=2)
                                      for name, label in ACTIONS.items()))
            self.assertEqual(set(action_evidence(path)), set(ACTIONS))
            path.write_text(json.dumps({"namedSetOfFiles": {"files": [
                {"uri": "file:///tmp/with%20space/archive-fixture.gz"}]}}))
            self.assertEqual(archive_output(path), Path("/tmp/with space/archive-fixture.gz"))

    def test_cleanup_handles_readonly_outputs_without_following_links(self):
        """Remove read-only work directories while preserving external symlink targets."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, outside = root / "work", root / "outside"
            nested = work / "readonly"
            nested.mkdir(parents=True)
            (nested / "index.json").write_text("{}")
            nested.chmod(0o555)
            outside.mkdir()
            (outside / "preserved").write_text("keep")
            outside.chmod(0o555)
            (work / "external").symlink_to(outside, target_is_directory=True)
            remove_temporary_work(work)
            self.assertFalse(work.exists())
            self.assertEqual((outside / "preserved").read_text(), "keep")
            self.assertEqual(outside.stat().st_mode & 0o777, 0o555)
            outside.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
