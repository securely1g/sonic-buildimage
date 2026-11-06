"""Keep cache verification strict about disk hits and source invalidation."""

import json
from argparse import Namespace
from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.bazel.ci.verify_agent_cache import ACTIONS, action_evidence, archive_output, remove_temporary_work, run_build, verify


class CacheEvidenceTest(unittest.TestCase):
    """Validate synthetic cache evidence and safe removal of temporary build state."""

    def evidence(self, hit, runner, digest):
        return {"actions": {name: {"cache_hit": hit, "runner": runner} for name in ACTIONS},
                "archive_sha256": digest}

    def test_isolated_build_disables_remote_cache_executor_and_downloader(self):
        for image in (None, "bazel-test-image"):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                args = Namespace(
                    artifacts=root, docker_image=image, bazel="bazel",
                    bazel_startup_arg=[], docker_arg=[], repository_cache=None,
                    bazel_arg=["--remote_cache=grpc://cache:9092",
                               "--experimental_remote_downloader=grpc://cache:9092"])
                with mock.patch("tools.bazel.ci.verify_agent_cache.subprocess.run",
                                return_value=mock.Mock(returncode=1)) as run:
                    with redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, "cold build failed"):
                        run_build(args, "cold", root, root / "cache", root / "output")
                command = run.call_args.args[0]
                for flag in ("--remote_cache=", "--remote_executor=",
                             "--experimental_remote_downloader="):
                    self.assertIn(flag, command)
                    self.assertFalse(any(value.startswith(flag) and value != flag
                                         for value in command[command.index(flag) + 1:]))
                self.assertIn("--disk_cache=" + ("/bazel_cache" if image else str(root / "cache"))
                              + "/disk_cache", command)

    def test_build_failure_prints_retained_bazel_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bazel = root / "bazel"
            bazel.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'fixture build stdout'\n"
                "printf '%s\\n' 'fixture build stderr' >&2\n"
                "exit 37\n")
            bazel.chmod(0o755)
            args = Namespace(
                artifacts=root, docker_image=None, bazel=str(bazel),
                bazel_startup_arg=[], bazel_arg=[], repository_cache=None)
            diagnostics = io.StringIO()
            with redirect_stderr(diagnostics):
                with self.assertRaisesRegex(RuntimeError, "cold build failed with exit code 37"):
                    run_build(args, "cold", root, root / "cache", root / "output")
            retained = (root / "cold/build.log").read_text()
            self.assertEqual(retained, "fixture build stdout\nfixture build stderr\n")
            self.assertIn(retained, diagnostics.getvalue())
            self.assertIn("cold build failed with exit code 37", diagnostics.getvalue())
            self.assertTrue((root / "cold/command.json").is_file())

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
