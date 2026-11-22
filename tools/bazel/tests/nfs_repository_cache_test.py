"""Exercise cache preparation and the VS workflow's actual integration."""

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
        self.assertLess(job.index("- name: Prepare the host-driven SONiC slave"),
                        job.index("- name: Configure NFS repository caches"))
        self.assertLess(job.index("- name: Configure NFS repository caches"),
                        job.index("- name: Build sonic-vs.bin through the host Make wrapper"))
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
        receipt = self.root / "receipt.json"
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools/bazel/ci/nfs_repository_cache.py"),
             "--mode", "off", "--github-env", str(self.root / "github-env"),
             "--receipt", str(receipt)], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(receipt.read_text())["mode"], "off")

    def test_enabled_workflow_prepares_in_slave_and_exports_only_after_success(self):
        commands = self.root / "commands"
        commands.mkdir()
        make = commands / "make"
        make.write_text("#!" + sys.executable + "\n" + """
import json, os, pathlib, shlex, subprocess, sys
args = sys.argv[1:]
pathlib.Path('make-args.json').write_text(json.dumps(args))
if os.environ.get('FAIL_SLAVE'):
    sys.exit(17)
command = next(arg.split('=', 1)[1] for arg in args if arg.startswith('SONIC_RUN_CMDS='))
subprocess.run(shlex.split(command), env=dict(os.environ, INSIDE_SLAVE='1'), check=True)
""")
        make.chmod(0o755)
        helper = self.root / "tools/bazel/ci/nfs_repository_cache.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("""
import argparse, json, os
from pathlib import Path
assert os.environ.get('INSIDE_SLAVE') == '1', 'Host must not prepare NFS repositories'
parser = argparse.ArgumentParser()
parser.add_argument('--mode', required=True)
parser.add_argument('--github-env', type=Path, required=True)
parser.add_argument('--receipt', type=Path, required=True)
args = parser.parse_args()
root = '/nfs/bazel/repository-caches'
contents = root + '/contents/bazel-8.5.1/vs-amd64/uid-1000' if args.mode == 'downloads-and-contents' else ''
args.github_env.parent.mkdir(parents=True, exist_ok=True)
args.github_env.write_text('SONIC_BAZEL_REPOSITORY_CACHE_SOURCE=' + root + '/downloads/uid-1000\\n'
    + 'SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE=' + contents + '\\n')
args.receipt.write_text(json.dumps({'mode': args.mode, 'inside_slave': True}))
""")
        script = shell(steps(jobs()["vs_container"])["Configure NFS repository caches"])
        for mode in ("downloads", "downloads-and-contents"):
            environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                               SONIC_VS_REPOSITORY_CACHE_MODE=mode,
                               SONIC_CI_CONTAINER_LABEL="fixture-label",
                               GITHUB_ENV=str(self.root / f"github-env-{mode}"))
            result = subprocess.run(["bash", "-e", "-c", script], cwd=self.root, env=environment,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = json.loads((self.root / "make-args.json").read_text())
            self.assertEqual(args[:4],
                             ["-f", "Makefile.work", "BLDENV=trixie", "sonic-slave-run"])
            self.assertIn(
                "SONIC_BUILDER_EXTRA_CMDLINE=--label fixture-label --mount type=bind,"
                "source=/nfs/bazel/repository-caches,target=/nfs/bazel/repository-caches", args)
            exported = dict(line.split("=", 1) for line in
                            Path(environment["GITHUB_ENV"]).read_text().splitlines())
            self.assertEqual(exported["SONIC_BAZEL_REPOSITORY_CACHE_SOURCE"],
                             "/nfs/bazel/repository-caches/downloads/uid-1000")
            self.assertEqual(bool(exported["SONIC_BAZEL_REPO_CONTENTS_CACHE_SOURCE"]),
                             mode == "downloads-and-contents")
        failed_env = self.root / "failed-env"
        result = subprocess.run(
            ["bash", "-e", "-c", script], cwd=self.root,
            env=dict(environment, FAIL_SLAVE="1", GITHUB_ENV=str(failed_env)),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 17)
        self.assertFalse(failed_env.exists(), "A failed slave must not export stale cache paths")


if __name__ == "__main__":
    unittest.main()
