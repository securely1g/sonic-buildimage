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
from types import SimpleNamespace
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
        (self.state / ("bazel-source-" + self.invocation)).mkdir()
        self.native = self.state / ("native-" + self.invocation)
        (self.native / "docker-data").mkdir(parents=True)
        (self.native / "native-cache").mkdir()
        (self.native / "docker-data/keep").write_text("old outer Docker state")
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
        (self.workspace / "fsroot.docker.trixie").mkdir()
        (self.workspace / "fsroot.docker.trixie/keep").write_text("old inner Docker state")
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
                         "inspected_ownership": {
                             name: [os.getuid(), os.getgid()] for name in ("runner", "native", "docker")},
                         "dash_commit": self.dash_old, "reviewed_overlay_commit": self.owner_commit,
                         "cache_inspection": copy.deepcopy(recovery.MANIFEST["cache_inspection"]),
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
                  "SONIC_DPKG_CACHE_SOURCE=" + str(self.native / "native-cache"),
                  "SONIC_DPKG_CACHE_METHOD=none", "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none"]
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
        self.assertEqual(len(report["paths"]), 14)
        self.assertEqual(len(report["native_cache_inventory"]), 3)
        self.assertTrue(all(item["matches"] for item in report["evidence"].values()))
        after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_cache_inventory_reads_metadata_without_following_links_or_double_counting_files(self):
        cache = self.native / "native-cache"
        (cache / "bazel").mkdir()
        (cache / "bazel/object").write_bytes(b"cache")
        os.link(cache / "bazel/object", cache / "bazel/hardlink")
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "not-cache").write_bytes(b"not included")
        (cache / "linked").symlink_to(outside, target_is_directory=True)
        before = (cache / "bazel/object").stat()
        with mock.patch.object(Path, "open", side_effect=AssertionError("file contents read")), \
                mock.patch.object(os, "chmod", side_effect=AssertionError("permissions changed")), \
                mock.patch.object(os, "chown", side_effect=AssertionError("owners changed")):
            result = recovery.cache_inventory(cache)
        self.assertTrue(result["complete"])
        self.assertTrue(result["immediate_child_names_complete"])
        self.assertEqual(result["entries_observed"], 4)
        self.assertEqual(result["regular_file_bytes"], 5)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["directories"], 1)
        self.assertEqual(result["symlinks"], 1)
        self.assertEqual([item["name"] for item in result["immediate_children"]], ["bazel", "linked"])
        self.assertEqual((cache / "bazel/object").stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(recovery.cache_inventory(cache / "linked")["complete"])

    def test_cache_inventory_reports_limits_missing_and_inaccessible_paths_independently(self):
        cache = self.native / "native-cache"
        for number in range(5):
            (cache / str(number)).touch()
        limited = recovery.cache_inventory(cache, entry_limit=2, child_limit=1)
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["entries_observed"], 2)
        self.assertEqual(limited["immediate_children_observed"], 2)
        self.assertEqual(len(limited["immediate_children"]), 1)
        self.assertIn("entry limit", limited["limitations"])
        self.assertIn("time limit", recovery.cache_inventory(cache, seconds=0)["limitations"])
        self.assertEqual(recovery.cache_inventory(cache / "absent")["errors"][0]["error"],
                         "FileNotFoundError")
        with mock.patch.object(recovery.os, "scandir", side_effect=PermissionError):
            inaccessible = recovery.cache_inventory(cache)
        self.assertEqual(inaccessible["errors"][0]["error"], "PermissionError")
        self.assertTrue(recovery.cache_inventory(cache)["complete"])

    def test_cache_inventory_bounds_depth_and_does_not_cross_filesystems(self):
        cache = self.native / "native-cache"
        (cache / "bazel").mkdir()
        (cache / "bazel/object").write_bytes(b"cache")
        limited = recovery.cache_inventory(cache, depth_limit=0)
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["files"], 0)
        self.assertIn("depth limit", limited["limitations"])
        real_fstat = os.fstat
        def another_device(descriptor):
            fields = list(real_fstat(descriptor))
            fields[2] += 1
            return os.stat_result(fields)
        with mock.patch.object(recovery.os, "fstat", side_effect=another_device):
            different = recovery.cache_inventory(cache)
        self.assertIn("other filesystem", different["limitations"])
        self.assertEqual(different["files"], 0)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses Unix directory permissions")
    def test_cache_inventory_keeps_sibling_metadata_after_permission_failure(self):
        cache = self.native / "native-cache"
        denied = cache / "bazel"
        denied.mkdir(mode=0o700)
        (denied / "unreadable").write_bytes(b"excluded")
        (cache / "vcache").mkdir()
        (cache / "vcache/visible").write_bytes(b"known")
        denied.chmod(0)
        try:
            result = recovery.cache_inventory(cache)
        finally:
            denied.chmod(0o700)
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"], [{"path": "bazel", "error": "PermissionError"}])
        self.assertEqual(result["regular_file_bytes"], 5)
        self.assertEqual(result["immediate_children_observed"], 2)

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

    def test_old_docker_exclusion_rejects_index_head_captured_entries_and_gitlinks(self):
        path = recovery.OLD_INNER + "/keep"
        self.run_git(self.workspace, "add", path)
        with self.assertRaisesRegex(ValueError, "staged"):
            recovery.audit_support(self.manifest)
        self.run_git(self.workspace, "commit", "--quiet", "-m", "tracked cache entry")
        with self.assertRaisesRegex(ValueError, "tracked entries"):
            recovery.audit_support(self.manifest)
        changed = dict(self.manifest, source_commit=self.run_git(self.workspace, "rev-parse", "HEAD"))
        self.run_git(self.workspace, "reset", "--quiet", self.source_commit)
        with self.assertRaisesRegex(ValueError, "tracked entries"):
            recovery.audit_support(changed)
        self.run_git(self.workspace, "update-index", "--add", "--cacheinfo",
                     "160000," + self.dash_old + "," + recovery.OLD_INNER)
        with self.assertRaisesRegex(ValueError, "staged"):
            recovery.audit_support(self.manifest)

    def test_old_docker_exclusion_is_exact_and_preserves_other_source_checks(self):
        cache = self.workspace / recovery.OLD_INNER / "keep"
        cache.write_text("changed cache metadata, intentionally not validated")
        recovery.evidence(self.manifest)
        self.assertEqual(recovery.audit_exclusion(self.manifest)["prior_untracked_metadata_count"], 1)
        sibling = self.workspace / (recovery.OLD_INNER + "-sibling")
        sibling.write_text("new source metadata")
        with self.assertRaisesRegex(ValueError, "source status/metadata changed"):
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
        expected = [item.replace(str(self.native / "native-cache"),
                                 str(inner.parent / "native-cache")) for item in self.original_command[:-1]]
        self.assertEqual(command[:-2], expected)
        self.assertEqual(command[-2:], ["DOCKER_ROOT=" + str(inner), "bazel-vs-native-inputs"])
        self.assertNotIn("init", command)
        self.assertNotIn("configure", command)
        (self.workspace / "artifacts/image/native-build.log").write_text("replacement")
        with self.assertRaisesRegex(ValueError, "log changed"):
            recovery.recorded_command(self.manifest, inner)

    def test_cache_option_replacement_rejects_missing_duplicate_malformed_and_changed_options(self):
        log = self.workspace / "artifacts/image/native-build.log"
        original = log.read_text()
        for key in ("SONIC_DPKG_CACHE_SOURCE", "SONIC_DPKG_CACHE_METHOD", "SONIC_DPKG_CACHE_METHOD_OVERRIDE"):
            for mutation in ("missing", "duplicate", "malformed", "changed"):
                with self.subTest(key=key, mutation=mutation):
                    records = [json.loads(line) for line in original.splitlines()]
                    argv = records[-1]["argv"]
                    item = next(item for item in argv if item.startswith(key + "="))
                    argv.remove(item)
                    if mutation == "duplicate":
                        argv[-1:-1] = [item, item]
                    elif mutation == "malformed":
                        argv.insert(-1, item.replace("=", "+=", 1))
                    elif mutation == "changed":
                        argv.insert(-1, key + "=unreviewed")
                    log.write_text("".join(json.dumps(record) + "\n" for record in records))
                    self.manifest["evidence"]["image/native-build.log"] = recovery.digest(log)
                    with self.assertRaisesRegex(ValueError, "cache option changed"):
                        recovery.recorded_command(self.manifest, self.state / "native-diagnostic/inner-docker-data")
        log.write_text(original)
        self.manifest["evidence"]["image/native-build.log"] = recovery.digest(log)
        records = [json.loads(line) for line in original.splitlines()]
        records[-1]["argv"].insert(-1, "SONIC_BAZEL_CACHE_SOURCE=/unreviewed")
        log.write_text("".join(json.dumps(record) + "\n" for record in records))
        self.manifest["evidence"]["image/native-build.log"] = recovery.digest(log)
        with self.assertRaisesRegex(ValueError, "derived cache options"):
            recovery.recorded_command(self.manifest, self.state / "native-diagnostic/inner-docker-data")

    def test_live_derived_caches_allow_missing_or_empty_but_reject_data_links_and_incomplete_scan(self):
        cache = self.native / "native-cache"
        (cache / "disabled-dpkg-marker").write_bytes(b"preserved")
        self.assertTrue(all(item["missing"] for item in recovery.derived_cache_inventories(self.manifest)))
        (cache / "bazel").mkdir()
        (cache / "vcache").mkdir()
        self.assertTrue(all(item["complete"] for item in recovery.derived_cache_inventories(self.manifest)))
        for name in ("bazel", "vcache"):
            for kind in ("file", "symlink", "fifo"):
                with self.subTest(name=name, kind=kind):
                    path = cache / name / "data"
                    if kind == "file":
                        path.write_bytes(b"cache hit")
                    elif kind == "symlink":
                        path.symlink_to(self.parent)
                    else:
                        os.mkfifo(path)
                    try:
                        with self.assertRaisesRegex(ValueError, "data or an incomplete inventory"):
                            recovery.derived_cache_inventories(self.manifest)
                    finally:
                        path.unlink()
        with mock.patch.object(recovery, "cache_inventory", return_value={"complete": False}):
            with self.assertRaisesRegex(ValueError, "incomplete inventory"):
                recovery.derived_cache_inventories(self.manifest)
        self.assertEqual((cache / "disabled-dpkg-marker").read_bytes(), b"preserved")

    def test_populated_derived_cache_rejects_before_reservation_worker_or_overlay(self):
        cache = self.native / "native-cache/bazel"
        cache.mkdir()
        (cache / "result").write_bytes(b"later cache result")
        with mock.patch.object(recovery, "validate_host", return_value=301 * 1024**3), \
                mock.patch.object(recovery, "docker") as docker, \
                mock.patch.object(recovery, "apply_overlay") as overlay:
            with self.assertRaisesRegex(ValueError, "old derived cache has data"):
                recovery.resume(self.manifest, self.owner, self.owner_commit, self.parent / "job-result.json")
        docker.assert_not_called()
        overlay.assert_not_called()
        self.assertFalse((self.state / "native-diagnostic").exists())
        self.assertEqual((cache / "result").read_bytes(), b"later cache result")

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
                support = recovery.audit_support(self.manifest)
                stack.enter_context(mock.patch.object(recovery, "audit_support",
                                                     side_effect=[support, OSError("audit")]))
            result = recovery.resume(self.manifest, self.owner, self.owner_commit, output)
        cleanup.assert_called_once()
        self.assertEqual(original, {name: recovery.digest(self.workspace / "artifacts" / name)
                                    for name in self.manifest["evidence"]})
        self.assertEqual((self.workspace / "target/keep").read_text(), "completed prerequisite")
        self.assertEqual((self.native / "docker-data/keep").read_text(), "old outer Docker state")
        self.assertEqual((self.workspace / "fsroot.docker.trixie/keep").read_text(), "old inner Docker state")
        report_cache = json.loads(output.read_text())["native_cache"]
        self.assertEqual(report_cache, recovery.cache_selection(self.manifest))
        for name in ("diagnostic", "derived_bazel_cache", "derived_version_cache"):
            cache = Path(report_cache[name])
            self.assertEqual(cache.stat().st_uid, os.getuid())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        return result, output, json.loads(output.read_text())

    def test_diagnostic_success_cannot_be_consumed_as_normal_native_proof(self):
        result, output, report = self.lifecycle()
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "diagnostic-passed")
        self.assertFalse(report["clean_checkout_proof"])
        self.assertEqual(report["execution_identity"]["uid"], os.getuid())
        self.assertFalse(report["execution_identity"]["retained_ownership_changed"])
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
        with mock.patch.object(recovery.build_area, "check_filesystem",
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

    def test_inspected_mixed_owners_are_required_before_docker(self):
        self.manifest["inspected_ownership"] = {
            "runner": [1001, 1003], "native": [1000, 1000], "docker": [0, 0]}
        actual_stat = Path.stat
        def inspected(path, *args, **kwargs):
            details = list(actual_stat(path, *args, **kwargs))
            owners = [1001, 1003]
            if path in (self.native, self.native / "native-cache",
                        self.state / ("bazel-source-" + self.invocation)):
                owners = [1000, 1000]
            elif path == self.native / "docker-data":
                owners = [0, 0]
            details[4:6] = owners
            return os.stat_result(details)
        image = {"Id": self.manifest["worker_image"], "Architecture": "amd64", "Os": "linux"}
        with mock.patch.object(Path, "stat", inspected), \
                mock.patch.object(os, "getuid", return_value=1001), \
                mock.patch.object(os, "getgid", return_value=1003), \
                mock.patch.object(recovery.build_area, "check_filesystem", return_value=301 * 1024**3), \
                mock.patch.object(recovery, "docker", side_effect=[
                    mock.Mock(stdout=""), mock.Mock(stdout=json.dumps([image]))]) as docker:
            self.assertEqual(recovery.validate_host(self.manifest), 301 * 1024**3)
            self.assertEqual(docker.call_count, 2)
        with mock.patch.object(os, "getuid", return_value=1009), \
                mock.patch.object(recovery, "docker") as docker:
            with self.assertRaisesRegex(ValueError, "executing identity"):
                recovery.validate_host(self.manifest)
        docker.assert_not_called()
        self.manifest["inspected_ownership"]["runner"] = [os.getuid(), os.getgid()]
        with mock.patch.object(recovery, "docker") as docker:
            with self.assertRaisesRegex(ValueError, "retained owner"):
                recovery.validate_host(self.manifest)
        docker.assert_not_called()

    def test_worker_account_uses_inspected_identity_without_chown(self):
        self.manifest["inspected_ownership"]["runner"] = [1001, 1003]
        identity = recovery.execution_identity(self.manifest)
        account = SimpleNamespace(pw_name=recovery.native_build.USER, pw_uid=1001, pw_gid=1003)
        with mock.patch.object(recovery.grp, "getgrgid", side_effect=KeyError), \
                mock.patch.object(recovery.pwd, "getpwuid", side_effect=KeyError), \
                mock.patch.object(recovery.pwd, "getpwnam", return_value=account), \
                mock.patch.object(recovery.native_build, "command") as command, \
                mock.patch.object(os, "chown", side_effect=AssertionError("retained owner changed")):
            recovery.worker_account(identity)
        self.assertEqual(command.call_args_list, [
            mock.call(["groupadd", "--gid", "1003", recovery.native_build.USER]),
            mock.call(["useradd", "--uid", "1001", "--gid", "1003", "--create-home",
                       "--shell", "/bin/bash", recovery.native_build.USER])])
        account.pw_gid = 1000
        with mock.patch.object(recovery.grp, "getgrgid",
                               return_value=SimpleNamespace(gr_name=recovery.native_build.USER)), \
                mock.patch.object(recovery.pwd, "getpwuid", return_value=account), \
                mock.patch.object(recovery.native_build, "command") as command:
            with self.assertRaisesRegex(ValueError, "unexpected diagnostic worker account"):
                recovery.worker_account(identity)
        command.assert_not_called()

    def test_old_native_cache_requires_no_write_access_or_permission_repair(self):
        image = {"Id": self.manifest["worker_image"], "Architecture": "amd64", "Os": "linux"}
        with mock.patch.object(os, "access", return_value=False), \
                mock.patch.object(recovery.build_area, "check_filesystem", return_value=301 * 1024**3), \
                mock.patch.object(recovery, "docker", side_effect=[
                    mock.Mock(stdout=""), mock.Mock(stdout=json.dumps([image]))]), \
                mock.patch.object(os, "chmod", side_effect=AssertionError("cache permissions changed")), \
                mock.patch.object(os, "chown", side_effect=AssertionError("cache ownership changed")):
            self.assertEqual(recovery.validate_host(self.manifest), 301 * 1024**3)

    def test_worker_cache_check_uses_actual_account_and_propagates_denial(self):
        identity = recovery.execution_identity(self.manifest)
        cache = self.state / "native-diagnostic/native-cache"
        (cache / "bazel").mkdir(parents=True)
        (cache / "vcache").mkdir()
        with mock.patch.object(recovery.native_build, "command",
                               side_effect=subprocess.CalledProcessError(1, ["test"])) as command, \
                mock.patch.object(os, "access", return_value=True), \
                mock.patch.object(os, "chmod", side_effect=AssertionError("cache permissions changed")):
            with self.assertRaises(subprocess.CalledProcessError):
                recovery.worker_cache_access(self.manifest, identity)
        command.assert_called_once_with([
            "runuser", "--user", recovery.native_build.USER, "--", "sh", "-c",
            'for path do test -w "$path" && test -x "$path" || exit 1; done', "native-cache-permission-check",
            str(cache), str(cache / "bazel"), str(cache / "vcache")])

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
