"""Reject mismatched historical source inputs before a runner builds them."""
import copy
import hashlib
import json
import unittest
from pathlib import Path
import subprocess
import sys
import tempfile
from historical_vs_backfill import INPUTS, P4_INPUTS, REPOSITORY, check_inputs, identity, kernel_command

ROOT = Path(__file__).resolve().parents[2]


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
        receipt = check_inputs(self.source, self.run, self.artifact)
        self.assertEqual(receipt["source_sha"], self.source)
        self.assertEqual(receipt["prefer_debian_repo"], "n")
        self.assertEqual(receipt["native_docker_apt_retries"], "")
        self.assertNotIn("p4_artifact_id", receipt)

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


class C20InputsTest(unittest.TestCase):
    def setUp(self):
        self.source = "c20b1d43e26c6eb2928191f041b78a60251ef9a0"
        run_id, artifact_id, digest = INPUTS[self.source]
        self.run = {"id": run_id, "head_sha": self.source,
                    "repository": {"full_name": REPOSITORY},
                    "event": "push", "head_branch": "master"}
        self.artifact = {"id": artifact_id, "name": "bazel-repository-inputs",
                         "digest": digest, "expired": False, "expires_at": "2026-11-16T09:53:34Z",
                         "workflow_run": {"id": run_id, "head_sha": self.source}}
        p4_id, p4_name, p4_digest = P4_INPUTS[self.source]
        self.p4 = {"id": p4_id, "name": p4_name, "digest": p4_digest,
                   "expired": False, "expires_at": "2026-11-16T09:53:50Z",
                   "workflow_run": {"id": run_id, "head_sha": self.source}}

    def test_c20_preserves_original_p4_and_debian_source_profile(self):
        receipt = check_inputs(self.source, self.run, self.artifact, self.p4)
        self.assertEqual(receipt["p4_artifact_id"], 13130893268)
        self.assertEqual(receipt["prefer_debian_repo"], "y")
        self.assertEqual(receipt["native_docker_apt_retries"], "0")

    def test_c20_requires_its_p4_artifact(self):
        with self.assertRaisesRegex(ValueError, "Pinned P4 source artifact is required"):
            check_inputs(self.source, self.run, self.artifact)

    def test_rejects_changed_expired_or_misattributed_p4_artifact(self):
        for changes in ({"expired": True}, {"id": 1}, {"name": "different"},
                        {"digest": "sha256:" + "0" * 64},
                        {"workflow_run": {"id": 1, "head_sha": self.source}},
                        {"workflow_run": {"id": self.run["id"], "head_sha": "0" * 40}}):
            with self.subTest(changes=changes):
                p4 = copy.deepcopy(self.p4)
                p4.update(changes)
                with self.assertRaises(ValueError):
                    check_inputs(self.source, self.run, self.artifact, p4)


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


class HistoricalKernelCommandTest(unittest.TestCase):
    def test_command_matches_the_original_make_recipe_with_and_without_cache(self):
        # Execute the real kernel Make recipe with a recording Python launcher;
        # compare its complete argument list against the separate prebuild CLI.
        # Quotes and spaces catch accidental shell/Make normalization changes.
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            binary = workspace / "bin"
            binary.mkdir()
            launcher = binary / "python3"
            launcher.write_text(
                "#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                "Path('arguments.json').write_text(json.dumps(sys.argv[1:]))\n")
            launcher.chmod(0o755)
            store = workspace / "worker public cacerts"
            store.write_bytes(b"fixture public trust store")
            for cache in ("", "/bazel_cache"):
                with self.subTest(cache=cache):
                    (workspace / "slave.mk").write_text(
                        "SRC_PATH := src\nDEBS_PATH := target/debs/trixie\n"
                        "BUILD_WITH_BAZEL_WHEN_AVAILABLE := y\n"
                        "CONFIGURED_ARCH := amd64\nCONFIGURED_PLATFORM := vs\nBLDENV := trixie\n"
                        "CROSS_BUILD_ENVIRON := n\nMULTIARCH_QEMU_ENVIRON := n\n"
                        "SECURE_UPGRADE_MODE := \"no_sign\"\n"
                        "ADDITIONAL_BUILD_PROFILES := test's profile\n"
                        "SECURE_UPGRADE_KERNEL_CAFILE := path with spaces/public.pem\n"
                        f"SONIC_BAZEL_CACHE_SOURCE := {cache}\n"
                        f"export PATH := {binary}:$(PATH)\n"
                        f"include {ROOT / 'rules/linux-kernel.mk'}\n")
                    recorded = workspace / "arguments.json"
                    recorded.unlink(missing_ok=True)
                    command, receipt = kernel_command(workspace, store)
                    self.assertFalse(recorded.exists(), "configuration query ran the kernel recipe")
                    subprocess.run(["make", "-s", "-f", "slave.mk", "bazel-kernel-ensure"],
                                   cwd=workspace, check=True, capture_output=True, text=True)
                    self.assertEqual(command[1:-2], json.loads(recorded.read_text()))
                    self.assertEqual(command[-2:], ["--java-trust-store", str(store)])
                    self.assertEqual(receipt["kernel_config"]["SECURE_UPGRADE_MODE"], "no_sign")
                    self.assertEqual(receipt["kernel_config"]["ADDITIONAL_BUILD_PROFILES"], "test's profile")
                    self.assertEqual(receipt["debs_directory"], "target/debs/trixie")
                    self.assertEqual(receipt["java_trust_store_sha256"], hashlib.sha256(store.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
