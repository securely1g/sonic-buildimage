"""Pure runtime-policy tests; mock every probe, daemon and privileged operation."""
import hashlib
import io
import stat
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from tools.bazel.ci import vs_runtime_check as runtime
from tools.bazel.tests.vs_worker_inspection_test import observation


def environment():
    return {"GITHUB_REPOSITORY": "securely1g/sonic-buildimage", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_ACTOR": "securely1g", "GITHUB_TRIGGERING_ACTOR": "securely1g", "RUNNER_NAME": "ks-fixture",
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40}


def reviewed():
    row = observation()
    row["vs_observations"]["marker"]["value"] = {"manager_revision": runtime.MANAGER, "base_image": runtime.BASE_IMAGE}
    return row


class RuntimePolicyTest(unittest.TestCase):
    def test_only_actual_manual_actors_and_assigned_worker_are_admitted(self):
        row = runtime.identity("ks-fixture", environment())
        self.assertEqual(row["controller"], "a" * 40)
        for key, value in (("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_EVENT_NAME", "pull_request"),
                           ("GITHUB_ACTOR", "other"), ("GITHUB_TRIGGERING_ACTOR", "other"),
                           ("RUNNER_NAME", "ks-other"), ("GITHUB_RUN_ID", "0"),
                           ("GITHUB_RUN_ATTEMPT", "1/2"), ("GITHUB_SHA", "main")):
            env = environment()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                runtime.identity("ks-fixture", env)

    def test_state_names_are_scoped_and_fit_unix_socket_limits(self):
        row = runtime.identity("ks-fixture", environment())
        original = runtime.state_paths(row, "/actions/_work/_temp")
        for key, value in (("run", "124"), ("attempt", "2"), ("worker", "ks-other")):
            different = runtime.state_paths(dict(row, **{key: value}), "/actions/_work/_temp")
            self.assertNotEqual(original["state"], different["state"])
            self.assertNotEqual(original["exec"], different["exec"])
        self.assertLess(len(str(original["exec"] / "docker.sock")), 108)
        self.assertNotIn(".source-image-cache", str(original["state"]))

    def test_state_cannot_escape_the_worker_namespace(self):
        row = runtime.identity("ks-fixture", environment())
        for path in ("/tmp", "relative", "/actions/_work/../../tmp", "/actions/_work-other/tmp"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                runtime.state_paths(row, path)

    def test_symlink_ancestors_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(ValueError):
                runtime.no_links(root / "link" / "new-state")

    def test_manager_image_and_canonical_prerequisites_all_remain_required(self):
        runtime.reviewed_observation(reviewed())
        for key, value in (("manager_revision", "b" * 40), ("base_image", "sha256:" + "b" * 64)):
            row = reviewed()
            row["vs_observations"]["marker"]["value"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                runtime.reviewed_observation(row)
        row = reviewed()
        row["resources"]["facts"]["cpu_limit"] = 4
        with self.assertRaises(ValueError):
            runtime.reviewed_observation(row)

    def test_helper_bytes_and_symlinks_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helper.py"
            path.write_bytes(b"reviewed source\n")
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(runtime.checked_helper(path, expected), path)
            with self.assertRaises(ValueError):
                runtime.checked_helper(path, "0" * 64)
            link = path.with_name("link.py")
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                runtime.checked_helper(link, expected)

    def test_kernel_probe_requires_the_exact_version_and_existing_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "tools/bazel/kernel/.bazelversion"
            version.parent.mkdir(parents=True)
            helper = root / "tools/bazel/ci/check_kernel_sandbox.py"
            with mock.patch.object(runtime, "checked_helper", return_value=helper) as check:
                version.write_bytes(b"8.5.1\n")
                command = runtime.kernel_command(root)
                self.assertEqual(command, [runtime.sys.executable, "-B", str(helper)])
                check.assert_called_with(helper, runtime.KERNEL_HELPER_SHA256)
                version.write_bytes(b"9.0.0\n")
                with self.assertRaises(ValueError):
                    runtime.kernel_command(root)

    def test_daemon_arguments_keep_private_roots_and_original_driver(self):
        paths = runtime.state_paths(runtime.identity("ks-fixture", environment()), "/actions/_work/_temp")
        args = runtime.daemon_arguments("/usr/bin/dockerd", paths)
        self.assertEqual(args[args.index("--storage-driver") + 1], "overlay2")
        self.assertEqual(args[args.index("--host") + 1], "unix://" + str(paths["exec"] / "docker.sock"))
        self.assertEqual(args[args.index("--data-root") + 1], str(paths["state"] / "docker-data"))
        self.assertNotIn("/var/run/docker.sock", args)
        self.assertNotIn("/actions/sonic-native-docker", args)
        self.assertTrue({"--bridge=none", "--iptables=false", "--ip6tables=false", "--ip-forward=false", "--ip-masq=false"} <= set(args))
        self.assertNotIn("--bip", args)
        self.assertNotIn("--default-address-pool", args)

    def test_wrong_daemon_identity_root_driver_or_version_cannot_pass(self):
        paths = {"state": Path("/actions/_work/_temp/private")}
        original = {"root": str(paths["state"] / "docker-data"), "driver": "overlay2", "version": "28.0.1"}
        self.assertEqual(runtime.validate_daemon(original, paths, 123, "123\n"), "28.0.1")
        for key, value in (("root", "/var/lib/docker"), ("driver", "vfs"), ("version", "private\npath")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                runtime.validate_daemon(dict(original, **{key: value}), paths, 123, "123")
        with self.assertRaises(ValueError):
            runtime.validate_daemon(original, paths, 123, "456")

    def test_cleanup_signals_only_the_owned_child_then_waits(self):
        child = mock.Mock()
        child.poll.side_effect = [None, 0]
        self.assertTrue(runtime.stop_owned_daemon(child))
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=30)
        child.kill.assert_not_called()

    def test_cleanup_handles_exited_and_stalled_owned_children(self):
        child = mock.Mock()
        child.poll.return_value = 0
        self.assertTrue(runtime.stop_owned_daemon(child))
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        child = mock.Mock()
        child.poll.side_effect = [None, -9]
        child.wait.side_effect = [subprocess.TimeoutExpired("own daemon", 30), -9]
        self.assertTrue(runtime.stop_owned_daemon(child))
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=30), mock.call(timeout=5)])

    def test_docker_readiness_uses_nonroot_client_and_only_private_environment(self):
        child = mock.Mock(returncode=0)
        child.communicate.return_value = (b'{"root":"/private","driver":"overlay2","version":"28.0"}', None)
        env = {"PATH": runtime.PATH, "DOCKER_HOST": "unix:///run/private/docker.sock", "DOCKER_CONFIG": "/private/client"}
        with mock.patch.object(runtime.grp, "getgrnam", return_value=mock.Mock(gr_gid=999)), mock.patch.object(runtime.subprocess, "Popen", return_value=child) as spawn, mock.patch.object(runtime, "wait_group_gone", return_value=True):
            self.assertEqual(runtime.docker_information("/usr/bin/docker", env)["driver"], "overlay2")
        self.assertEqual(spawn.call_args.kwargs["user"], 1000)
        self.assertEqual(spawn.call_args.kwargs["group"], 1000)
        self.assertEqual(spawn.call_args.kwargs["extra_groups"], [999])
        self.assertEqual(spawn.call_args.kwargs["env"], env)
        self.assertTrue(spawn.call_args.kwargs["close_fds"])
        self.assertEqual(spawn.call_args.args[0][1], "info")
        child.communicate.assert_called_once_with(timeout=3)

    def test_no_daemon_root_admission_without_the_exact_marker(self):
        with mock.patch.object(runtime.os, "geteuid", return_value=0), mock.patch.object(runtime.Path, "is_file", return_value=True), mock.patch.object(runtime.inspection, "marker", return_value={}), mock.patch.object(runtime.subprocess, "Popen") as spawn:
            with self.assertRaises(ValueError):
                runtime.private_daemon({"state": Path("/unreached"), "exec": Path("/unreached")})
            spawn.assert_not_called()

    def test_failed_preflight_is_recorded_without_any_runtime_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "receipt.json"
            with mock.patch.dict(runtime.os.environ, dict(environment(), RUNNER_TEMP="/actions/_work/_temp"), clear=True), mock.patch.object(runtime.os, "getuid", return_value=1000), mock.patch.object(runtime.os, "geteuid", return_value=1000), mock.patch.object(runtime.os, "getgid", return_value=1000), mock.patch.object(runtime.os, "getegid", return_value=1000), mock.patch.object(runtime, "state_paths", return_value={"state": root / "state", "exec": root / "exec"}), mock.patch.object(runtime.inspection, "collect", return_value={}), mock.patch.object(runtime, "bounded") as execute:
                value = runtime.run_probe("ks-fixture", output)
            self.assertEqual(value["status"], "failed")
            self.assertFalse(value["full_VS_or_kernel_package_acceptance"])
            self.assertNotIn("private_docker", value)
            self.assertFalse((root / "state").exists())
            execute.assert_not_called()
            self.assertEqual(json.loads(output.read_text()), value)

    def test_existing_receipt_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            output.write_text("original")
            with mock.patch.dict(runtime.os.environ, environment(), clear=True), mock.patch.object(runtime.os, "getuid", return_value=1000), mock.patch.object(runtime.os, "geteuid", return_value=1000), mock.patch.object(runtime.os, "getgid", return_value=1000), mock.patch.object(runtime.os, "getegid", return_value=1000), self.assertRaises(ValueError):
                runtime.run_probe("ks-fixture", output)
            self.assertEqual(output.read_text(), "original")


    def test_root_timeout_closes_lifeline_and_reaps_without_unprivileged_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(stdin=io.BytesIO(), returncode=0)
            child.wait.side_effect = [subprocess.TimeoutExpired("root supervisor", 230), 0]
            with mock.patch.object(runtime.subprocess, "Popen", return_value=child) as spawn, mock.patch.object(runtime.os, "killpg") as kill, self.assertRaises(subprocess.TimeoutExpired):
                runtime.bounded(["sudo", "reviewed"], Path(directory), "root", 230, root_supervisor=True)
            self.assertTrue(child.stdin.closed)
            self.assertEqual(child.wait.call_args_list, [mock.call(timeout=230), mock.call(timeout=65)])
            self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.PIPE)
            self.assertTrue(spawn.call_args.kwargs["close_fds"])
            kill.assert_not_called()

    def test_root_interruption_closes_lifeline_and_reaps(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(stdin=io.BytesIO(), returncode=0)
            child.wait.side_effect = [KeyboardInterrupt(), 0]
            with mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime.os, "killpg") as kill, self.assertRaises(KeyboardInterrupt):
                runtime.bounded(["sudo", "reviewed"], Path(directory), "root", 230, root_supervisor=True)
            self.assertTrue(child.stdin.closed)
            self.assertEqual(child.wait.call_count, 2)
            kill.assert_not_called()

    def test_unreaped_root_is_explicit_not_inferred_from_pipe_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(stdin=io.BytesIO(), returncode=None)
            child.wait.side_effect = subprocess.TimeoutExpired("root supervisor", 65)
            with mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime.os, "killpg") as kill, self.assertRaises(runtime.SupervisorUnclosed) as caught:
                runtime.bounded(["sudo", "reviewed"], Path(directory), "root", 230, root_supervisor=True)
            self.assertFalse(runtime.safe_failure(caught.exception)["root_supervisor_reaped"])
            self.assertTrue(child.stdin.closed)
            kill.assert_not_called()

    def test_interrupted_same_uid_helper_is_stopped_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(pid=246, returncode=None)
            child.wait.side_effect = [KeyboardInterrupt(), 0]
            with mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime.os, "killpg") as kill, mock.patch.object(runtime, "group_alive", return_value=True), mock.patch.object(runtime, "wait_group_gone", return_value=True), self.assertRaises(KeyboardInterrupt):
                runtime.bounded(["reviewed-helper"], Path(directory), "helper", 10)
            kill.assert_called_once_with(246, runtime.signal.SIGTERM)
            self.assertEqual(child.wait.call_args_list, [mock.call(timeout=10), mock.call(timeout=5)])

    def test_lifeline_eof_and_root_deadline_are_independent(self):
        with mock.patch.object(runtime.time, "monotonic", return_value=9), mock.patch.object(runtime.os, "read", side_effect=BlockingIOError):
            runtime.root_lifeline_tick(10)
        with mock.patch.object(runtime.time, "monotonic", return_value=9), mock.patch.object(runtime.os, "read", return_value=b""), self.assertRaisesRegex(runtime.ProbeFailure, "lifeline closed"):
            runtime.root_lifeline_tick(10)
        with mock.patch.object(runtime.time, "monotonic", return_value=10), mock.patch.object(runtime.os, "read") as read, self.assertRaisesRegex(runtime.ProbeFailure, "deadline expired"):
            runtime.root_lifeline_tick(10)
        read.assert_not_called()

    def test_interrupted_docker_client_is_reaped_before_daemon_cleanup(self):
        child = mock.Mock(pid=247, returncode=None)
        child.communicate.side_effect = runtime.ProbeFailure("Caller lifeline closed")
        with mock.patch.object(runtime.grp, "getgrnam", return_value=mock.Mock(gr_gid=999)), mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime.os, "killpg") as kill, mock.patch.object(runtime, "group_alive", return_value=True), mock.patch.object(runtime, "wait_group_gone", return_value=True), self.assertRaises(runtime.ProbeFailure):
            runtime.docker_information("docker", {})
        kill.assert_called_once_with(247, runtime.signal.SIGTERM)
        child.wait.assert_called_once_with(timeout=5)

    def test_diagnostics_do_not_publish_urls_paths_or_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "https://private.invalid/?token=secret-value /private/host/path"
            (root / "dockerd.log").write_text("x" * 70000 + "failed to mount overlay: operation not permitted " + secret)
            records = runtime.diagnostics(root)
            self.assertEqual(records[0]["tail_bytes"], 65536)
            self.assertEqual(records[0]["known_errors"], ["operation_not_permitted", "overlay_failure"])
            serialized = json.dumps(records)
            self.assertNotIn("private.invalid", serialized)
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("/private/host", serialized)
            error = subprocess.TimeoutExpired(["download", secret], 3, stderr=secret)
            self.assertEqual(runtime.safe_failure(error), {"error_type": "TimeoutExpired", "timeout": 3})

    def test_cleanup_keeps_original_failure_and_disarms_before_reaping(self):
        state, execution = mock.MagicMock(), mock.MagicMock()
        state.stat.return_value = mock.Mock(st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_gid=1000)
        leaves = {name: mock.MagicMock() for name in ("daemon.json", "docker-data", "client", "dockerd.log")}
        for leaf in leaves.values():
            leaf.exists.return_value = leaf.is_symlink.return_value = False
        state.__truediv__.side_effect = leaves.__getitem__
        execution.exists.return_value = execution.is_symlink.return_value = False
        child = mock.Mock(returncode=None)
        child.poll.return_value = None
        events = []
        def stop(owned):
            self.assertIs(owned, child)
            events.append("stop")
            raise subprocess.TimeoutExpired("owned daemon", 30)
        with mock.patch.object(runtime.os, "geteuid", return_value=0), mock.patch.object(runtime.Path, "is_file", return_value=True), mock.patch.object(runtime.inspection, "marker", return_value={"base_image": runtime.BASE_IMAGE, "manager_revision": runtime.MANAGER}), mock.patch.object(runtime, "no_links"), mock.patch.object(runtime, "checked_helper", return_value=Path("reviewed")), mock.patch.object(runtime.shutil, "which", return_value="reviewed"), mock.patch.object(runtime.os, "fstat", return_value=mock.Mock(st_mode=stat.S_IFIFO)), mock.patch.object(runtime.os, "set_blocking"), mock.patch.object(runtime.os, "chown"), mock.patch.object(runtime.signal, "signal"), mock.patch.object(runtime.signal, "setitimer", side_effect=lambda timer, value, *rest: events.append(("timer", value))), mock.patch.object(runtime.time, "monotonic", return_value=10), mock.patch.object(runtime, "root_lifeline_tick"), mock.patch.object(runtime, "bounded"), mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime, "docker_information", side_effect=runtime.ProbeFailure("Caller lifeline closed")), mock.patch.object(runtime, "stop_owned_daemon", side_effect=stop), mock.patch.object(runtime, "diagnostics", return_value=[]):
            result = runtime.private_daemon({"state": state, "exec": execution})
        self.assertEqual(result["failure"]["reason"], "Caller lifeline closed")
        self.assertEqual(result["cleanup_failure"]["error_type"], "TimeoutExpired")
        self.assertFalse(result["daemon_closed"])
        self.assertEqual(result["status"], "failed")
        self.assertLess(events.index(("timer", 0)), events.index("stop"))


    def test_exited_leader_does_not_hide_surviving_owned_group(self):
        child = mock.Mock(pid=248, returncode=0)
        child.poll.return_value = 0
        with mock.patch.object(runtime, "group_alive", return_value=True), mock.patch.object(runtime, "wait_group_gone", side_effect=[False, True]) as gone, mock.patch.object(runtime.os, "killpg") as kill:
            runtime.stop_owned_session(child)
        self.assertEqual(kill.call_args_list, [mock.call(248, runtime.signal.SIGTERM), mock.call(248, runtime.signal.SIGKILL)])
        self.assertEqual(child.wait.call_args_list, [mock.call(timeout=5), mock.call(timeout=5)])
        self.assertEqual(gone.call_args_list, [mock.call(248, 5), mock.call(248, 5)])

    def test_unclosed_helper_group_remains_a_failure_after_leader_exit(self):
        child = mock.Mock(pid=249, returncode=0)
        child.poll.return_value = 0
        with mock.patch.object(runtime, "group_alive", return_value=True), mock.patch.object(runtime, "wait_group_gone", return_value=False), mock.patch.object(runtime.os, "killpg"), self.assertRaisesRegex(runtime.ProbeFailure, "process group did not close"):
            runtime.stop_owned_session(child)


    def test_successful_helper_leader_with_descendants_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(pid=250, returncode=0)
            child.wait.return_value = child.poll.return_value = 0
            with mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime, "wait_group_gone", side_effect=[False, True]), mock.patch.object(runtime, "group_alive", return_value=True), mock.patch.object(runtime.os, "killpg") as kill, self.assertRaisesRegex(runtime.ProbeFailure, "surviving descendants"):
                runtime.bounded(["reviewed-helper"], Path(directory), "helper", 10)
            kill.assert_called_once_with(250, runtime.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
