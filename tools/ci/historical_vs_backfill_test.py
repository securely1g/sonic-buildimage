"""Reject mismatched historical source inputs before a runner builds them."""
import copy
import unittest
from pathlib import Path
import subprocess
import tempfile
from historical_vs_backfill import INPUTS, REPOSITORY, check_inputs, identity


class HistoricalInputsTest(unittest.TestCase):
    def setUp(self):
        self.source = next(iter(INPUTS))
        run_id, artifact_id, digest = INPUTS[self.source]
        self.run = {"id": run_id, "head_sha": self.source,
                    "repository": {"full_name": REPOSITORY},
                    "event": "push", "head_branch": "master"}
        self.artifact = {"id": artifact_id, "name": "bazel-repository-inputs",
                         "digest": digest, "expired": False, "expires_at": "2026-11-11T04:49:44Z",
                         "workflow_run": {"id": run_id, "head_sha": self.source}}

    def test_accepts_recorded_source_and_artifact(self):
        self.assertEqual(check_inputs(self.source, self.run, self.artifact)["source_sha"], self.source)

    def test_rejects_other_source_run(self):
        self.run["head_sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "pinned source"):
            check_inputs(self.source, self.run, self.artifact)

    def test_rejects_expired_wrong_digest_and_other_run_artifacts(self):
        for changes in ({"expired": True}, {"digest": "sha256:" + "0" * 64},
                        {"workflow_run": {"id": 1, "head_sha": self.source}}):
            with self.subTest(changes=changes):
                artifact = copy.deepcopy(self.artifact)
                artifact.update(changes)
                with self.assertRaises(ValueError):
                    check_inputs(self.source, self.run, artifact)

    def test_rejects_non_master_and_other_repository(self):
        for changes in ({"head_branch": "topic"}, {"event": "pull_request"},
                        {"repository": {"full_name": "someone/sonic-buildimage"}}):
            with self.subTest(changes=changes):
                run = copy.deepcopy(self.run)
                run.update(changes)
                with self.assertRaises(ValueError):
                    check_inputs(self.source, run, self.artifact)


class HistoricalCheckoutTest(unittest.TestCase):
    def test_preserves_source_and_pinned_submodules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child, source = root / "child", root / "source"

            def git(path, *args):
                return subprocess.check_output([
                    "git", "-c", "protocol.file.allow=always", "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "-C", str(path), *args],
                    text=True, stderr=subprocess.DEVNULL).strip()

            for path in (child, source):
                path.mkdir()
                git(path, "init", "-q")
                (path / "recipe").write_text("original source\n")
                git(path, "add", "recipe")
                git(path, "commit", "-qm", "original")
            git(source, "submodule", "add", "-q", str(child), "dependency")
            git(source, "commit", "-qam", "pin dependency")
            revision = git(source, "rev-parse", "HEAD")
            self.assertTrue(identity(revision, source, False)["submodule_commits_unchanged"])
            # Patches applied by Make inside a pinned submodule are recorded as
            # build transformations, but must never move the recorded commit.
            (source / "dependency/recipe").write_text("Make-applied patch\n")
            self.assertTrue(identity(revision, source, True)["tracked_superproject_unchanged"])
            git(source / "dependency", "commit", "-qam", "different source")
            with self.assertRaises(subprocess.CalledProcessError):
                identity(revision, source, True)
            git(source / "dependency", "reset", "--hard", "HEAD~1")
            (source / "recipe").write_text("different recipe\n")
            with self.assertRaises(subprocess.CalledProcessError):
                identity(revision, source, True)


if __name__ == "__main__":
    unittest.main()
