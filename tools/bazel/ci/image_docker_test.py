#!/usr/bin/env python3
"""Check the non-sudo controller boundary and owned-container cleanup."""

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import image_docker as controller


class DockerControllerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        (self.workspace / "MODULE.bazel").write_text("module(name = 'fixture')\n")
        self.state = self.root / "state"
        self.state.mkdir()
        self.args = argparse.Namespace(workspace=self.workspace, state=self.state,
            artifacts=self.workspace / "artifacts/image", ca_bundle=None, git_config=None,
            runner_uid=1234, runner_gid=1235)
        self.worker = "sha256:" + "c" * 64
        self.container = "d" * 64
        self.invocation = "a" * 32
        self.details = {"Id": self.container, "Name": "/sonic-vs-controller-" + self.invocation,
                        "Config": {"Labels": {controller.LABEL: self.invocation}},
                        "State": {"Running": False, "ExitCode": 0}}

    def test_controller_uses_same_host_paths_and_explicit_transport_and_trust(self):
        self.args.git_config = self.workspace / "source.gitconfig"
        trust = self.state / "worker/build-ca-bundle.pem"
        command = controller.create_command(self.args, self.root, self.worker, self.invocation, trust)
        self.assertIn("type=bind,src=" + str(self.root) + ",dst=" + str(self.root), command)
        self.assertIn("type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock", command)
        self.assertIn("GIT_CONFIG_SYSTEM=" + str(self.args.git_config), command)
        self.assertEqual(command[command.index("--ca-bundle") + 1], str(trust))
        self.assertIn(self.worker, command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("sudo", command)
        self.assertIn("--init", command)
        self.assertEqual(command[command.index("--stop-timeout") + 1], "180")
        with self.assertRaisesRegex(ValueError, "immutable"):
            controller.create_command(self.args, self.root, "worker:latest", self.invocation, None)

    def test_relocation_preserves_caches_across_distinct_runner_attempts(self):
        stable = self.root / "persistent-cache/vs-installer"
        cache = stable / "state/package-cache/entry"
        cache.parent.mkdir(parents=True)
        cache.write_text("warm cache")
        stable.chmod(0o700)
        keys = []
        for number in (1, 2):
            staged = self.root / ("runner-attempt-" + str(number)) / "_work/checkout"
            (staged / ".git").mkdir(parents=True)
            (staged / "MODULE.bazel").write_text("module(name='fixture')")
            (staged / "tools/bazel").mkdir(parents=True)
            (staged / "tools/bazel/common.bazelrc").write_text("build --jobs=4\n")
            destination = stable / ("checkout-" + str(number))
            keys.append(controller.relocate_checkout(staged, destination))
            self.assertFalse(staged.exists())
            self.assertTrue((destination / ".git").is_dir())
            self.assertEqual(cache.read_text(), "warm cache")
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(stat.S_IMODE(stable.stat().st_mode), 0o755)
        self.assertTrue((stable / "checkout-1/.git").is_dir())
        with self.assertRaisesRegex(ValueError, "retained checkout"):
            controller.relocate_checkout(stable / "checkout-2", stable / "checkout-1")
        self.assertTrue((stable / "checkout-2/.git").is_dir())

    def test_restore_only_visits_shared_caches_and_not_retained_native_state(self):
        for name in ("package-cache", "repository-cache", "native-older/docker-data"):
            (self.state / name).mkdir(parents=True)
            (self.state / name / "entry").write_text("keep me")
        with mock.patch.object(controller.os, "chown") as chown, \
                mock.patch.object(controller.image, "chown_tree") as tree:
            controller.restore_caches(self.state, (1234, 1235))
        chown.assert_called_once_with(self.state, 1234, 1235, follow_symlinks=False)
        self.assertEqual(tree.call_args_list, [
            mock.call(self.state / "package-cache", 1234, 1235),
            mock.call(self.state / "repository-cache", 1234, 1235)])
        self.assertEqual((self.state / "native-older/docker-data/entry").read_text(), "keep me")

    def test_cache_symlink_cannot_redirect_ownership_restoration(self):
        (self.state / "package-cache").symlink_to(self.workspace, target_is_directory=True)
        with mock.patch.object(controller.os, "chown"), \
                mock.patch.object(controller.image, "chown_tree") as tree, \
                self.assertRaisesRegex(ValueError, "symlink"):
            controller.restore_caches(self.state, (1234, 1235))
        tree.assert_not_called()

    def test_external_cache_mounts_and_restoration_never_include_old_native_state(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.args.cache_root = Path(temp.name) / "private/state"
        for name in ("package-cache", "repository-cache", "native-old/docker-data"):
            path = self.args.cache_root / name
            path.mkdir(parents=True)
            (path / "entry").write_text("retained")
        self.args.cache_root.parent.chmod(0o700)
        command = controller.create_command(self.args, self.root, self.worker, self.invocation, None)
        mounts = [command[i + 1] for i, flag in enumerate(command) if flag == "--mount"]
        self.assertEqual(len(mounts), 4)
        for name in ("package-cache", "repository-cache"):
            path = str(self.args.cache_root / name)
            self.assertIn("type=bind,src=" + path + ",dst=" + path, mounts)
        self.assertNotIn("type=bind,src=" + str(self.args.cache_root) + ",dst=" + str(self.args.cache_root), mounts)
        with mock.patch.object(controller.os, "chown") as chown, \
                mock.patch.object(controller.image, "chown_tree") as tree:
            controller.restore_caches(self.state, (1234, 1235), self.args.cache_root)
        chown.assert_called_once_with(self.state, 1234, 1235, follow_symlinks=False)
        self.assertEqual(tree.call_args_list, [
            mock.call(self.args.cache_root / "package-cache", 1234, 1235),
            mock.call(self.args.cache_root / "repository-cache", 1234, 1235)])
        self.assertEqual((self.args.cache_root / "native-old/docker-data/entry").read_text(), "retained")

    def test_external_cache_root_cannot_overlap_build_area_or_follow_symlinks(self):
        for path in (Path("/"), self.root, self.workspace, self.state / "nested"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                controller.image.cache_paths(self.workspace, self.state, path)
        alias = self.root / "alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            controller.image.cache_paths(self.workspace, self.state, alias)

    def test_inside_restores_runner_cache_access_on_build_failure_or_cancellation(self):
        exists = Path.exists
        for failure in (RuntimeError("build failed"), InterruptedError("cancelled")):
            with self.subTest(failure=failure), \
                    mock.patch.object(controller.os, "geteuid", return_value=0), \
                    mock.patch.object(Path, "exists", lambda path: str(path) == "/.dockerenv" or exists(path)), \
                    mock.patch.object(controller.image, "build", side_effect=failure), \
                    mock.patch.object(controller, "restore_caches") as restore, \
                    self.assertRaises(type(failure)):
                controller.inside(self.args)
            restore.assert_called_once_with(self.state, (1234, 1235))

    def test_foreign_container_is_never_stopped_or_removed(self):
        self.details["Config"]["Labels"][controller.LABEL] = "another-invocation"
        result = subprocess.CompletedProcess(["docker"], 0, json.dumps([self.details]), "")
        with mock.patch.object(controller.subprocess, "run", return_value=result) as run, \
                self.assertRaisesRegex(ValueError, "not owned"):
            controller.stop_owned(self.container, self.invocation)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0], ["docker", "inspect", self.container])

    def test_cancellation_stops_exact_owned_controller_with_cleanup_grace(self):
        running = dict(self.details, State={"Running": True})
        with mock.patch.object(controller, "inspect_owned", side_effect=[running, self.details]), \
                mock.patch.object(controller.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0)) as run:
            controller.stop_owned(self.container, self.invocation)
        self.assertEqual(run.call_args_list, [
            mock.call(["docker", "stop", "--time", "180", self.container], check=True,
                      timeout=195, start_new_session=True),
            mock.call(["docker", "rm", self.container], capture_output=True, text=True,
                      timeout=30, start_new_session=True)])

    def test_missing_controller_is_allowed_but_daemon_errors_are_not_hidden(self):
        for message in ("Error: No such object: fixture", "error: no such container: fixture"):
            result = subprocess.CompletedProcess(["docker"], 1, "[]", message)
            with self.subTest(message=message), \
                    mock.patch.object(controller.subprocess, "run", return_value=result):
                self.assertIsNone(controller.inspect_owned("fixture", self.invocation, missing_ok=True))
        result = subprocess.CompletedProcess(["docker"], 1, "", "daemon unavailable")
        with mock.patch.object(controller.subprocess, "run", return_value=result), \
                self.assertRaises(subprocess.CalledProcessError):
            controller.inspect_owned("fixture", self.invocation, missing_ok=True)

    def run_fixture(self, exit_code=0, interrupted=False, create_interrupted=False,
                    removed=False, still_running=False):
        spec = self.state / "worker/execution-environment.json"
        spec.parent.mkdir()
        spec.write_text(json.dumps({"worker_image": self.worker}))
        def bootstrap(workspace, state, artifacts, *_args):
            self.assertTrue(artifacts.is_relative_to(state))
            self.assertFalse(artifacts.is_relative_to(workspace))
            (artifacts / "bootstrap.log").write_text("must stay outside fresh source until build finishes")
            return spec
        process = mock.Mock()
        process.wait.side_effect = [InterruptedError("cancelled") if interrupted else exit_code, exit_code]
        self.details["State"]["ExitCode"] = exit_code
        self.details["State"]["Running"] = still_running
        with mock.patch.dict(os.environ), \
                mock.patch.object(Path, "is_socket", return_value=True), \
                mock.patch.object(controller.uuid, "uuid4", return_value=mock.Mock(hex=self.invocation)), \
                mock.patch.object(controller.image, "build_worker", side_effect=bootstrap), \
                mock.patch.object(controller.subprocess, "check_output",
                    side_effect=InterruptedError("create cancelled") if create_interrupted else None,
                    return_value=self.container), \
                mock.patch.object(controller.subprocess, "Popen", return_value=process), \
                mock.patch.object(controller, "inspect_owned",
                                  side_effect=[self.details, None if removed else self.details]), \
                mock.patch.object(controller, "stop_owned") as stop:
            if interrupted or create_interrupted:
                with self.assertRaises(InterruptedError):
                    controller.run(self.args)
            elif still_running:
                with self.assertRaisesRegex(ValueError, "attach ended before"):
                    controller.run(self.args)
            else:
                self.assertEqual(controller.run(self.args), exit_code)
        stop.assert_called_once_with(
            "sonic-vs-controller-" + self.invocation if create_interrupted else self.container,
            self.invocation, missing_ok=True)
        return json.loads((self.args.artifacts.with_name("controller") / "controller-receipt.json").read_text())

    def test_failed_container_status_is_returned_and_receipt_is_retained(self):
        self.assertEqual(self.run_fixture(exit_code=23)["returncode"], 23)

    def test_allocation_provenance_is_published_with_controller_evidence_on_failure(self):
        record = '{"scratch_retention":"host temporary-directory retention"}\n'
        (self.state / "build-area.json").write_text(record)
        self.run_fixture(exit_code=23)
        self.assertEqual((self.workspace / "artifacts/controller/build-area.json").read_text(), record)

    def test_auto_removed_container_preserves_attached_failure_exit_code(self):
        self.assertEqual(self.run_fixture(exit_code=23, removed=True)["returncode"], 23)

    def test_attach_cannot_succeed_while_controller_is_still_running(self):
        self.assertEqual(self.run_fixture(still_running=True)["status"], "failed")

    def test_interrupt_during_attach_still_stops_owned_container_and_records_failure(self):
        receipt = self.run_fixture(interrupted=True)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error"], "cancelled")
        self.assertNotEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)

    def test_interrupt_before_create_returns_id_uses_unique_owned_name_for_cleanup(self):
        self.assertEqual(self.run_fixture(create_interrupted=True)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
