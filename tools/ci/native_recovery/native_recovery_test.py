#!/usr/bin/env python3
"""Real Git/filesystem regressions; Docker lifecycle is tested without Make."""

from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import native_recovery as recovery

image_inputs = recovery.load_support("image_inputs")
source_workspace = recovery.source_workspace


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {
            "GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "native-diagnostic",
            "GITHUB_REPOSITORY": "securely1g/sonic-buildimage", "GITHUB_SHA": "d" * 40,
            "GITHUB_WORKFLOW_REF": "securely1g/sonic-buildimage/.github/workflows/runner-manager.yml@refs/heads/fixture",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "build"
        self.workspace = self.root / "checkout"
        self.workspace.mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.invocation = "1" * 16
        self.native = self.state / ("native-" + self.invocation)
        (self.native / "docker-data").mkdir(parents=True)
        (self.native / "docker-data/keep").write_text("old outer Docker state")
        (self.workspace / "fsroot.docker.trixie").mkdir()
        (self.workspace / "fsroot.docker.trixie/keep").write_text("old inner Docker state")
        (self.workspace / "target").mkdir()
        (self.workspace / "target/keep").write_text("completed prerequisite")
        self.dash = self.workspace / "src/sonic-dash-api"
        self.dash.mkdir(parents=True)
        self.init(self.dash)
        for name, text in {
            recovery.OLD_HELPER: "from python.runfiles import runfiles\n",
            "bazel/BUILD.bazel": 'exports_files(["python_test.py"])\n',
            "misc/BUILD.bazel": 'srcs = ["//bazel:python_test.py"]\nmain = "//bazel:python_test.py"\n',
        }.items():
            path = self.dash / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
        self.dash_old = self.commit(self.dash)
        self.init(self.workspace)
        (self.workspace / ".gitignore").write_text("target/\nartifacts/\nrules/config.user\n.platform\n.arch\n")
        (self.workspace / "rules").mkdir()
        (self.workspace / "rules/config.user").write_text(recovery.CONFIG)
        (self.workspace / ".platform").write_text("vs\n")
        (self.workspace / ".arch").write_text("amd64\n")
        self.source_commit = self.commit(self.workspace)
        self.owner = self.parent / "owner"
        self.run_git(self.parent, "clone", "--quiet", "--no-hardlinks", str(self.dash), str(self.owner))
        self.run_git(self.owner, "config", "user.name", "Guohan Lu")
        self.run_git(self.owner, "config", "user.email", "securely1g@gmail.com")
        (self.owner / recovery.OLD_HELPER).rename(self.owner / recovery.NEW_HELPER)
        for name in recovery.REFERENCES:
            path = self.owner / name
            path.write_bytes(path.read_bytes().replace(b"python_test.py", b"python_test_runner.py"))
        self.owner_commit = self.commit(self.owner)
        self.image = {"source_commit": self.source_commit, "status": "failed",
                      "components": {"src/sonic-dash-api": {"commit": self.dash_old, "gitlink": self.dash_old}}}
        self.manifest = {"build_root": str(self.root), "source_commit": self.source_commit,
                         "source_date_epoch": self.run_git(self.workspace, "show", "-s", "--format=%ct", "HEAD"),
                         "invocation": self.invocation, "run_id": 123, "job_id": 456,
                         "dash_commit": self.dash_old, "reviewed_overlay_commit": self.owner_commit,
                         "worker_image": "sha256:" + "a" * 64, "evidence": {}}
        spec = self.state / ("worker-" + self.invocation)
        spec.mkdir()
        (spec / "execution-environment.json").write_text(json.dumps({
            "worker_image": self.manifest["worker_image"], "platform": "linux/amd64", "distribution": "trixie"}))
        artifacts = self.workspace / "artifacts/image"
        artifacts.mkdir(parents=True)
        previous = {key: self.manifest[key] for key in ("source_commit", "invocation", "worker_image")}
        previous.update(status="failed", worker_removed=True)
        (artifacts / "native-receipt.json").write_text(json.dumps(previous))
        (artifacts / "image-receipt.json").write_text(json.dumps(self.image))
        prefix = ["runuser", "--preserve-environment", "--user", "sonicnative", "--", "make", "-f",
                  "Makefile.work", "SOURCE_DATE_EPOCH=" + self.manifest["source_date_epoch"],
                  "SONIC_DPKG_CACHE_METHOD=none"]
        self.original_command = prefix + ["bazel-vs-native-inputs"]
        (artifacts / "native-build.log").write_text("".join(
            json.dumps({"native_stage": stage, "argv": prefix + [stage]}) + "\n"
            for stage in ("init", "configure", "bazel-vs-native-inputs")))
        (artifacts / "native-source-audit.json").write_text(json.dumps(
            source_workspace.audit(self.workspace, self.image)))
        for path in artifacts.iterdir():
            self.manifest["evidence"]["image/" + path.name] = recovery.digest(path)

    def run_git(self, cwd, *arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.run(["git", "-C", str(cwd), *arguments], env=env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def init(self, path):
        self.run_git(path, "init", "--quiet")
        self.run_git(path, "config", "user.name", "Guohan Lu")
        self.run_git(path, "config", "user.email", "securely1g@gmail.com")

    def commit(self, path):
        self.run_git(path, "add", "--all")
        self.run_git(path, "commit", "--quiet", "-m", "Git fixture")
        return self.run_git(path, "rev-parse", "HEAD")

    def overlay(self):
        return recovery.owner_overlay(self.owner, self.owner_commit, self.manifest)

    def test_directory_lock_is_exclusive_and_creates_no_files(self):
        before = set(self.root.iterdir())
        with recovery.exclusive(self.root):
            with self.assertRaises(BlockingIOError):
                with recovery.exclusive(self.root):
                    self.fail("second owner acquired lock")
        with recovery.exclusive(self.root):
            pass
        self.assertEqual(set(self.root.iterdir()), before)

    def test_inspection_never_locks_or_starts_docker_and_keeps_partial_results(self):
        before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        def mount(argv, **kwargs):
            if argv[-1].endswith("docker-data"):
                raise PermissionError("fixture inaccessible directory")
            return mock.Mock(stdout=json.dumps({"filesystems": [{"fstype": "ext4"}]}))
        with mock.patch.object(recovery, "exclusive", side_effect=AssertionError("lock attempted")), \
                mock.patch.object(recovery, "docker", side_effect=AssertionError("Docker attempted")), \
                mock.patch.object(recovery.subprocess, "run", side_effect=mount):
            report = recovery.inspect(self.manifest)
        self.assertIn("available_bytes", report["paths"][0])
        records = {item["path"]: item for item in report["paths"]}
        self.assertIn("error", records[str(self.native / "docker-data")])
        self.assertEqual(records[str(self.parent)]["uid"], self.parent.stat().st_uid)
        self.assertEqual(len(report["paths"]), 11)
        self.assertTrue(all(item["matches"] for item in report["evidence"].values()))
        after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_evidence_validates_actual_graph_receipts_config_and_current_metadata(self):
        source, _ = recovery.evidence(self.manifest)
        self.assertEqual(source["source_commit"], self.source_commit)
        (self.workspace / ".platform").write_text("other\n")
        with self.assertRaisesRegex(ValueError, "platform changed"):
            recovery.evidence(self.manifest)

    def test_tampered_prior_receipt_fails_before_reuse(self):
        (self.workspace / "artifacts/image/native-receipt.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "prior evidence changed"):
            recovery.evidence(self.manifest)

    def test_changed_tracked_source_and_new_git_identity_are_rejected(self):
        (self.dash / recovery.OLD_HELPER).write_text("changed source\n")
        with self.assertRaisesRegex(ValueError, "source status/metadata changed"):
            recovery.evidence(self.manifest)
        self.commit(self.dash)
        with self.assertRaisesRegex(ValueError, "HEAD differs"):
            recovery.evidence(self.manifest)

    def test_owner_rename_applies_exact_bytes_without_moving_head_or_old_products(self):
        contents, overlay = self.overlay()
        old = recovery.digest(self.workspace / "target/keep")
        recovery.apply_overlay(self.workspace, contents, overlay)
        self.assertFalse((self.dash / recovery.OLD_HELPER).exists())
        self.assertEqual((self.dash / recovery.NEW_HELPER).read_bytes(), contents[recovery.NEW_HELPER])
        self.assertEqual(self.run_git(self.dash, "rev-parse", "HEAD"), self.dash_old)
        self.assertEqual(recovery.digest(self.workspace / "target/keep"), old)
        self.assertEqual((self.native / "docker-data/keep").read_text(), "old outer Docker state")
        self.assertEqual((self.workspace / "fsroot.docker.trixie/keep").read_text(), "old inner Docker state")

    def test_owner_rejects_extra_changes_modified_launcher_and_mode_change(self):
        for change in ("extra", "launcher", "mode"):
            with self.subTest(change=change):
                self.run_git(self.owner, "reset", "--hard", self.owner_commit)
                if change == "extra":
                    (self.owner / "bazel/extra").write_text("unreviewed")
                elif change == "launcher":
                    (self.owner / recovery.NEW_HELPER).write_text("changed launcher")
                else:
                    (self.owner / recovery.NEW_HELPER).chmod(0o755)
                self.run_git(self.owner, "add", "--all")
                self.run_git(self.owner, "commit", "--quiet", "--amend", "--no-edit")
                changed = self.run_git(self.owner, "rev-parse", "HEAD")
                with self.assertRaises(ValueError):
                    recovery.owner_overlay(self.owner, changed,
                                           dict(self.manifest, reviewed_overlay_commit=changed))

    def test_overlay_rejects_links_and_existing_destination(self):
        contents, overlay = self.overlay()
        old = self.dash / recovery.OLD_HELPER
        old.rename(self.dash / "linked-original")
        old.symlink_to("../linked-original")
        with self.assertRaisesRegex(ValueError, "linked"):
            recovery.apply_overlay(self.workspace, contents, overlay)
        old.unlink()
        (self.dash / "linked-original").rename(old)
        os.link(old, self.parent / "hardlink")
        with self.assertRaisesRegex(ValueError, "shared"):
            recovery.apply_overlay(self.workspace, contents, overlay)

    def test_exact_logged_command_preserves_epoch_and_skips_init_configure(self):
        inner = self.state / "native-diagnostic/inner-docker-data"
        command = recovery.recorded_command(self.manifest, inner)
        self.assertEqual(command[:-2], self.original_command[:-1])
        self.assertEqual(command[-2:], ["DOCKER_ROOT=" + str(inner), "bazel-vs-native-inputs"])
        self.assertNotIn("init", command)
        self.assertNotIn("configure", command)
        (self.workspace / "artifacts/image/native-build.log").write_text("replacement")
        with self.assertRaisesRegex(ValueError, "log changed"):
            recovery.recorded_command(self.manifest, inner)

    def lifecycle(self, failure=None, cleanup_failure=False, audit_failure=False):
        output = self.parent / "job-result.json"
        process = mock.Mock()
        process.wait.side_effect = [failure or 0, 0]
        proof = self.workspace / "target/bazel-native"
        proof.mkdir()
        (proof / "provenance.json").write_text(json.dumps({"source_commit": self.source_commit}))
        original = {name: recovery.digest(self.workspace / "artifacts" / name)
                    for name in self.manifest["evidence"]}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(recovery, "validate_host", return_value=301 * 1024**3))
            stack.enter_context(mock.patch.object(recovery, "docker"))
            stack.enter_context(mock.patch.object(recovery, "owned", return_value={
                "Id": "b" * 64, "State": {"Running": False, "ExitCode": 0}}))
            cleanup = stack.enter_context(mock.patch.object(
                recovery, "stop_owned", side_effect=ValueError("foreign") if cleanup_failure else None))
            real_popen = subprocess.Popen
            stack.enter_context(mock.patch.object(recovery.subprocess, "Popen", side_effect=
                lambda argv, **kwargs: process if argv[0] == "docker" else real_popen(argv, **kwargs)))
            if audit_failure:
                stack.enter_context(mock.patch.object(source_workspace, "audit", side_effect=OSError("audit")))
            result = recovery.resume(self.manifest, self.owner, self.owner_commit, output)
        cleanup.assert_called_once()
        self.assertEqual(original, {name: recovery.digest(self.workspace / "artifacts" / name)
                                    for name in self.manifest["evidence"]})
        self.assertEqual((self.workspace / "target/keep").read_text(), "completed prerequisite")
        self.assertEqual((self.native / "docker-data/keep").read_text(), "old outer Docker state")
        self.assertEqual((self.workspace / "fsroot.docker.trixie/keep").read_text(), "old inner Docker state")
        return result, output, json.loads(output.read_text())

    def test_diagnostic_success_cannot_be_consumed_as_normal_native_proof(self):
        result, output, report = self.lifecycle()
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "diagnostic-passed")
        self.assertFalse(report["clean_checkout_proof"])
        with self.assertRaisesRegex(ValueError, "did not pass"):
            image_inputs.verify_native(output, self.workspace, self.image, report["invocation"])

    def test_cancellation_retains_evidence_and_removes_only_owned_worker(self):
        result, _, report = self.lifecycle(failure=InterruptedError("cancelled"))
        self.assertEqual(result, 1)
        self.assertTrue(report["worker_removed"])
        self.assertEqual(report["status"], "diagnostic-failed")
        self.assertIn("cancelled", report["error"])

    def test_cleanup_failure_cannot_publish_diagnostic_success(self):
        result, _, report = self.lifecycle(cleanup_failure=True)
        self.assertEqual(result, 1)
        self.assertIn("cleanup_error", report)

    def test_audit_failure_still_writes_final_diagnostic_receipt(self):
        result, _, report = self.lifecycle(audit_failure=True)
        self.assertEqual(result, 1)
        self.assertIn("audit_error", report)

    def test_host_rejects_changed_private_parent_without_docker_or_repairs(self):
        self.parent.chmod(0o755)
        with mock.patch.object(recovery, "docker") as docker:
            with self.assertRaisesRegex(ValueError, "private runner-owned"):
                recovery.validate_host(self.manifest)
        docker.assert_not_called()

    def test_support_loader_rejects_modified_helpers_and_ignores_import_shadows(self):
        directory = self.parent / "support"
        directory.mkdir()
        shutil.copyfile(recovery.SUPPORT / "build_area.py", directory / "build_area.py")
        with mock.patch.object(recovery, "SUPPORT", directory), \
                mock.patch.dict(sys.modules, {"build_area": mock.Mock(MIN_FREE=1)}):
            self.assertEqual(recovery.load_support("build_area").MIN_FREE, 300 * 1024**3)
            (directory / "build_area.py").write_text("raise AssertionError('must not execute')\n")
            with self.assertRaisesRegex(ValueError, "pinned PR2 helper differs"):
                recovery.load_support("build_area")

    def test_live_capacity_rejection_cannot_start_docker(self):
        actual_stat = Path.stat
        owners = {self.parent, self.root, self.workspace, self.state, self.native}
        def uid1000(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if path in owners:
                values = list(result)
                values[4] = 1000
                return os.stat_result(values)
            return result
        with mock.patch.object(Path, "stat", uid1000), \
                mock.patch.object(os, "getuid", return_value=1000), \
                mock.patch.object(recovery.build_area, "check_filesystem",
                                  side_effect=ValueError("current free space below 300 GiB")) as capacity, \
                mock.patch.object(recovery, "docker") as docker:
            with self.assertRaisesRegex(ValueError, "below 300 GiB"):
                recovery.validate_host(self.manifest)
        capacity.assert_called_once_with(self.root)
        docker.assert_not_called()
        self.assertFalse((self.state / "native-diagnostic").exists())

    def test_docker_environment_excludes_context_and_configuration_overrides(self):
        with mock.patch.dict(os.environ, {"DOCKER_CONTEXT": "other-host", "DOCKER_TLS_VERIFY": "1",
                                         "DOCKER_HOST": "tcp://other", "DOCKER_CONFIG": "/untrusted"}):
            environment = recovery.docker_environment()
        self.assertEqual({key: value for key, value in environment.items() if key.startswith("DOCKER_")},
                         {"DOCKER_HOST": "unix:///var/run/docker.sock"})

    def test_separate_cleanup_uses_only_this_job_reserved_invocation(self):
        _, output, report = self.lifecycle()
        destination = self.parent / "cleanup.json"
        with mock.patch.object(recovery, "stop_owned") as stop:
            self.assertEqual(recovery.cleanup_job(output, destination, self.manifest), 0)
        stop.assert_called_once_with("sonic-native-diagnostic-" + report["invocation"],
                                     report["invocation"], self.manifest)
        self.assertTrue(json.loads(destination.read_text())["worker_removed"])

    def test_separate_cleanup_rejects_other_job_attempt_and_plan(self):
        _, output, _ = self.lifecycle()
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), \
                mock.patch.object(recovery, "stop_owned") as stop:
            self.assertEqual(recovery.cleanup_job(output, self.parent / "wrong-job.json", self.manifest), 1)
        stop.assert_not_called()
        plan = self.state / "native-diagnostic/plan.json"
        data = json.loads(plan.read_text())
        data["invocation"] = "e" * 16
        plan.write_text(json.dumps(data))
        with mock.patch.object(recovery, "stop_owned") as stop:
            self.assertEqual(recovery.cleanup_job(output, self.parent / "wrong-plan.json", self.manifest), 1)
        stop.assert_not_called()

    def test_rejected_second_attempt_never_cleans_prior_worker(self):
        output = self.parent / "rejected.json"
        output.write_text(json.dumps({"status": "diagnostic-rejected", "job_identity": recovery.job_identity()}))
        with mock.patch.object(recovery, "stop_owned") as stop, \
                mock.patch.object(recovery, "exclusive") as lock:
            self.assertEqual(recovery.cleanup_job(output, self.parent / "skipped.json", self.manifest), 0)
        stop.assert_not_called()
        lock.assert_not_called()

    def test_interrupted_receipt_update_preserves_initial_reservation(self):
        output = self.parent / "reservation.json"
        recovery.job_evidence(output, {"invocation": "original"})
        with mock.patch.object(Path, "replace", side_effect=InterruptedError("killed")):
            with self.assertRaises(InterruptedError):
                recovery.job_evidence(output, {"invocation": "updated"})
        self.assertEqual(json.loads(output.read_text()), {"invocation": "original"})

    def test_reviewed_owner_identity_is_required_even_for_identical_delta(self):
        with self.assertRaisesRegex(ValueError, "reviewed overlay"):
            recovery.owner_overlay(self.owner, self.owner_commit,
                                   dict(self.manifest, reviewed_overlay_commit="f" * 40))

    def test_owned_worker_rejects_foreign_label_image_or_mount(self):
        info = {"Id": "b" * 64, "Config": {"Labels": {recovery.native_build.LABEL: self.invocation},
                                          "Image": self.manifest["worker_image"]},
                "HostConfig": {"Privileged": True, "CgroupnsMode": "private"},
                "Mounts": [{"Type": "bind", "Source": str(self.root), "Destination": str(self.root), "RW": True}]}
        def check(value):
            with mock.patch.object(recovery.subprocess, "run", return_value=mock.Mock(
                    stdout=json.dumps([value]), returncode=0)):
                return recovery.owned("name", self.invocation, self.manifest)
        self.assertEqual(check(info), info)
        for field in ("label", "image", "mount"):
            value = copy.deepcopy(info)
            if field == "label":
                value["Config"]["Labels"] = {}
            elif field == "image":
                value["Config"]["Image"] = "sha256:" + "c" * 64
            else:
                value["Mounts"][0]["Source"] = "/unrelated"
            with self.subTest(field=field), self.assertRaises(ValueError):
                check(value)


if __name__ == "__main__":
    unittest.main()
