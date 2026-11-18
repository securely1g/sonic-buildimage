"""Exercise opt-in cache preparation and the VS workflow's actual integration."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tools.bazel.ci import nfs_repository_cache as subject
from tools.bazel.tests.source_workflow_test import ROOT, jobs, shell, steps


class NfsRepositoryCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_off_needs_no_nfs_version_file_or_commands(self):
        with patch.object(subject.subprocess, "check_output") as run:
            exports, receipt = subject.prepare("off", self.root / "absent", "")
        self.assertEqual(exports, {})
        self.assertEqual(receipt, {"mode": "off"})
        run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_modes_reuse_stable_uid_paths_without_changing_existing_entries(self):
        with patch.object(subject.subprocess, "check_output", return_value="nfs\n") as stat:
            downloads, _ = subject.prepare("downloads", self.root, "8.5.1")
            self.assertNotIn("SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE", downloads)
            directory = Path(downloads["SONIC_BAZEL_REPOSITORY_CACHE_SOURCE"])
            sentinel = directory / "existing"
            sentinel.write_bytes(b"cached archive")
            before = sentinel.stat()
            both, receipt = subject.prepare("downloads-and-contents", self.root, "8.5.1")
            again, _ = subject.prepare("downloads-and-contents", self.root, "8.5.1")
        stat.assert_called_with(
            ["stat", "--file-system", "--format=%T", "--", str(self.root)], text=True)
        self.assertEqual(again, both)
        self.assertEqual(both["SONIC_BAZEL_REPOSITORY_CACHE_SOURCE"], str(directory))
        self.assertEqual(sentinel.read_bytes(), b"cached archive")
        self.assertEqual(sentinel.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(sentinel.stat().st_mode, before.st_mode)
        self.assertEqual(receipt["filesystem"], "nfs")
        contents = Path(both["SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE"])
        self.assertIn("bazel-8.5.1/vs-amd64", str(contents))
        self.assertEqual(list(contents.iterdir()), [])

    def test_missing_mount_and_unsupported_version_do_not_create_caches(self):
        for mode, filesystem, version in (
                ("downloads", "ext2/ext3", "8.5.1"),
                ("downloads-and-contents", "nfs", "9.0.0")):
            with self.subTest(mode=mode), patch.object(
                    subject.subprocess, "check_output", return_value=filesystem):
                with self.assertRaises(ValueError):
                    subject.prepare(mode, self.root, version)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_redirected_parent_is_rejected_without_writing_through_it(self):
        outside = self.root / "outside"
        outside.mkdir()
        store = self.root / "store"
        store.mkdir()
        (store / "downloads").symlink_to(outside, target_is_directory=True)
        with patch.object(subject.subprocess, "check_output", return_value="nfs"):
            with self.assertRaisesRegex(ValueError, "redirected"):
                subject.prepare("downloads", store, "8.5.1")
        self.assertEqual(list(outside.iterdir()), [])

    def test_inaccessible_cache_or_record_lock_failure_is_fatal(self):
        for target in ("TemporaryFile", "lockf"):
            owner = subject.tempfile if target == "TemporaryFile" else subject.fcntl
            with self.subTest(target=target), patch.object(
                    subject.subprocess, "check_output", return_value="nfs4"), \
                    patch.object(owner, target, side_effect=OSError("NFS unavailable")):
                with self.assertRaisesRegex(OSError, "NFS unavailable"):
                    subject.prepare("downloads", self.root, "8.5.1")

    def test_workflow_defaults_to_contents_and_explicit_off_needs_no_nfs(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        trigger = yaml.safe_load(workflow.split("\npermissions:", 1)[0].replace("\non:", "\ntrigger:"))
        dispatch = trigger["trigger"]["workflow_dispatch"]["inputs"]["repository_cache_mode"]
        self.assertEqual(dispatch["default"], "downloads-and-contents")
        self.assertEqual(dispatch["options"], list(subject.MODES))
        job = jobs()["vs_container"]
        self.assertLess(job.index("- name: Reclaim local build caches"),
                        job.index("- name: Configure NFS repository caches"))
        self.assertLess(job.index("- name: Configure NFS repository caches"),
                        job.index("- name: Prepare the host-driven SONiC slave"))
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.repository_cache_mode"
                      " || 'downloads-and-contents' }}", job)
        environment = dict(os.environ, SONIC_VS_REPOSITORY_CACHE_MODE="off",
                           GITHUB_ENV=str(self.root / "github-env"))
        helper = self.root / "tools/bazel/ci/nfs_repository_cache.py"
        helper.parent.mkdir(parents=True)
        helper.symlink_to(ROOT / "tools/bazel/ci/nfs_repository_cache.py")
        result = subprocess.run(
            ["bash", "-e", "-c", shell(steps(job)["Configure NFS repository caches"])],
            cwd=self.root, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "github-env").read_text(),
                         "SONIC_BAZEL_REPOSITORY_CACHE_SOURCE=\n"
                         "SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE=\n")
        # Execute the CLI separately with a private receipt destination.
        receipt = self.root / "receipt.json"
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools/bazel/ci/nfs_repository_cache.py"),
             "--mode", "off", "--github-env", str(self.root / "github-env"),
             "--receipt", str(receipt)], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(receipt.read_text())["mode"], "off")


if __name__ == "__main__":
    unittest.main()
