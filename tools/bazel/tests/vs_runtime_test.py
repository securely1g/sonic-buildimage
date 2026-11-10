"""Pure runtime-policy tests; mock every probe, daemon and privileged operation."""
from contextlib import ExitStack
import hashlib
import re
import io
import stat
import struct
import shutil
from types import SimpleNamespace
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
        self.assertEqual(next(arg.removeprefix("--data-root=") for arg in args if arg.startswith("--data-root=")), str(paths["docker_data"]))
        self.assertNotIn("/var/run/docker.sock", args)
        self.assertNotIn("/actions/sonic-native-docker", args)
        self.assertTrue({"--bridge=none", "--iptables=false", "--ip6tables=false", "--ip-forward=false", "--ip-masq=false"} <= set(args))
        self.assertNotIn("--bip", args)
        self.assertNotIn("--default-address-pool", args)

    def test_wrong_daemon_identity_root_driver_or_version_cannot_pass(self):
        paths = {"state": Path("/actions/_work/_temp/private"),
                 "docker_data": Path("/actions/_work/_temp/private/source-image-docker-123-1")}
        original = {"root": str(paths["docker_data"]), "driver": "overlay2", "version": "28.0.1"}
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
            self.assertEqual(child.wait.call_args_list, [mock.call(timeout=230), mock.call(timeout=420)])
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
            child.wait.side_effect = subprocess.TimeoutExpired("root supervisor", 135)
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
            (root / "dockerd.log").write_text("x" * 70000 + "\nTraceback (most recent call last):\nPermissionError: [Errno 1] " + secret)
            records = runtime.diagnostics(root)
            self.assertEqual(records[0]["tail_bytes"], 65536)
            self.assertEqual(records[0]["known_errors"], ["operation_not_permitted"])
            self.assertEqual(records[0]["exception_facts"], [{"error_type": "PermissionError", "errno": 1}])
            serialized = json.dumps(records)
            self.assertNotIn("private.invalid", serialized)
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("/private/host", serialized)
            error = subprocess.TimeoutExpired(["download", secret], 3, stderr=secret)
            self.assertEqual(runtime.safe_failure(error), {"error_type": "TimeoutExpired", "timeout": 3})

    def test_cleanup_keeps_original_failure_and_disarms_before_reaping(self):
        state, execution = mock.MagicMock(), mock.MagicMock()
        state.stat.return_value = mock.Mock(st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_gid=1000)
        leaves = {name: mock.MagicMock() for name in ("daemon.json", "docker-data", "client", "dockerd.log", "native-stop-control")}
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
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(runtime.os, "geteuid", return_value=0))
            patches.enter_context(mock.patch.object(runtime.Path, "is_file", return_value=True))
            patches.enter_context(mock.patch.object(runtime.inspection, "marker", return_value={"base_image": runtime.BASE_IMAGE, "manager_revision": runtime.MANAGER}))
            patches.enter_context(mock.patch.object(runtime, "no_links"))
            patches.enter_context(mock.patch.object(runtime, "native_stop_source", return_value=Path("native-cleanup")))
            patches.enter_context(mock.patch.object(runtime, "native_capture", return_value={"status": "passed"}))
            patches.enter_context(mock.patch.object(runtime, "native_stop", side_effect=runtime.ProbeFailure("Native stop failed")))
            patches.enter_context(mock.patch.object(runtime, "native_mount_diagnostics", return_value=None))
            patches.enter_context(mock.patch.object(runtime, "checked_helper", return_value=Path("reviewed")))
            patches.enter_context(mock.patch.object(runtime.shutil, "which", return_value="reviewed"))
            patches.enter_context(mock.patch.object(runtime.os, "fstat", return_value=mock.Mock(st_mode=stat.S_IFIFO)))
            patches.enter_context(mock.patch.object(runtime.os, "set_blocking"))
            patches.enter_context(mock.patch.object(runtime.os, "chown"))
            patches.enter_context(mock.patch.object(runtime.signal, "signal"))
            patches.enter_context(mock.patch.object(runtime.signal, "setitimer", side_effect=lambda timer, value, *rest: events.append(("timer", value))))
            patches.enter_context(mock.patch.object(runtime.time, "monotonic", return_value=10))
            patches.enter_context(mock.patch.object(runtime, "root_lifeline_tick"))
            patches.enter_context(mock.patch.object(runtime, "bounded"))
            patches.enter_context(mock.patch.object(runtime.subprocess, "Popen", return_value=child))
            patches.enter_context(mock.patch.object(runtime, "docker_information", side_effect=runtime.ProbeFailure("Caller lifeline closed")))
            patches.enter_context(mock.patch.object(runtime, "stop_owned_daemon", side_effect=stop))
            patches.enter_context(mock.patch.object(runtime, "diagnostics", return_value=[]))
            result = runtime.private_daemon({"state": state, "exec": execution, "docker_data": leaves["docker-data"],
                                             "identity": {"worker": "ks-fixture", "run": "123", "attempt": "1"}})
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


class StoppedContainerTest(unittest.TestCase):
    def setUp(self):
        self.state = Path("/actions/_work/_temp/vs-runtime-" + "a" * 24)
        self.name = "sonic-vs-stopped-" + "a" * 24
        self.tag = "sonic-vs-stopped:" + "a" * 24
        self.image = "sha256:" + "b" * 64
        self.container = "c" * 64
        self.calls = []

    def inspected(self):
        return {"Id": self.container, "Image": self.image, "Name": "/" + self.name,
                "Config": {"Cmd": [runtime.NEVER_RUN]}, "HostConfig": {"NetworkMode": "default"},
                "State": {"Status": "created", "Running": False, "Pid": 0,
                          "StartedAt": "0001-01-01T00:00:00Z"}}

    def operation(self, executable, arguments, environment, state, label, payload):
        self.calls.append((arguments, label, payload))
        return {"stopped-import": self.image.encode(), "stopped-create": self.container.encode(),
                "stopped-inspect-before": json.dumps(self.inspected()).encode(),
                "stopped-inspect-after": json.dumps(self.inspected()).encode(),
                "stopped-archive": runtime.literal_rootfs(),
                "stopped-remove-container": self.container.encode(), "stopped-remove-image": b"Deleted"}[label]

    def test_fixture_is_only_a_deterministic_nonexecutable_literal(self):
        payload = runtime.literal_rootfs()
        self.assertEqual(payload, runtime.literal_rootfs())
        self.assertLessEqual(len(payload), 16384)
        with runtime.tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            entries = archive.getmembers()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].name, "probe.txt")
            self.assertTrue(entries[0].isfile())
            self.assertEqual(entries[0].mode, 0o444)
            self.assertEqual((entries[0].mtime, entries[0].uid, entries[0].gid), (0, 0, 0))
            self.assertEqual(archive.extractfile(entries[0]).read(), b"SONiC stopped-container archive probe\n")

    def test_success_uses_default_create_no_start_and_removes_only_owned_objects(self):
        with mock.patch.object(runtime, "docker_operation", side_effect=self.operation):
            result = runtime.stopped_container("docker", {"DOCKER_HOST": "private"}, self.state)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["owned_container_removed"] and result["owned_image_removed"])
        self.assertFalse(result["ever_started_observed"] or result["container_start_requested"])
        self.assertEqual(result["reported_network_mode"], "default")
        self.assertEqual(self.calls[1][0], ["create", "--pull=never", "--name", self.name, self.image, runtime.NEVER_RUN])
        self.assertEqual([x[1] for x in self.calls], list(runtime.STOPPED_LOGS))
        self.assertEqual(self.calls[-2][0], ["rm", "--force", self.container])
        self.assertEqual(self.calls[-1][0], ["image", "rm", "--force", self.image])
        for args, unused, payload in self.calls:
            self.assertNotIn(args[0], ["start", "run", "build", "pull", "exec", "network", "prune"])
            self.assertFalse(any(x.startswith("--network") for x in args))

    def test_wrong_identity_or_started_state_cannot_reach_archive(self):
        for key, value in (("Id", "d" * 64), ("Image", "sha256:" + "d" * 64),
                           ("Name", "/other"), ("State", {"Status": "running", "Running": True,
                            "Pid": 19, "StartedAt": "2026-01-01T00:00:00Z"})):
            self.calls = []
            original = self.inspected()
            original[key] = value
            with self.subTest(key=key), mock.patch.object(self, "inspected", return_value=original), mock.patch.object(runtime, "docker_operation", side_effect=self.operation):
                result = runtime.stopped_container("docker", {}, self.state)
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("stopped-archive", [x[1] for x in self.calls])
            self.assertTrue(result["owned_container_removed"] and result["owned_image_removed"])

    def test_archive_rejects_wrong_bytes_paths_and_links_without_extraction(self):
        for path, kind, data in (("probe.txt", runtime.tarfile.REGTYPE, b"wrong"),
                                 ("../probe.txt", runtime.tarfile.REGTYPE, runtime.LITERAL),
                                 ("probe.txt", runtime.tarfile.SYMTYPE, b"")):
            output = io.BytesIO()
            with runtime.tarfile.open(fileobj=output, mode="w") as archive:
                entry = runtime.tarfile.TarInfo(path)
                entry.type, entry.size, entry.linkname = kind, len(data), "/outside"
                archive.addfile(entry, io.BytesIO(data))
            with self.subTest(path=path, kind=kind), self.assertRaises(ValueError):
                runtime.literal_archive(output.getvalue())

    def test_interrupted_create_cleans_reserved_name_and_preserves_failure(self):
        def operation(*args):
            if args[4] == "stopped-create":
                self.calls.append((args[1], args[4], args[5]))
                raise subprocess.TimeoutExpired("private create", 10)
            return self.operation(*args)
        with mock.patch.object(runtime, "docker_operation", side_effect=operation):
            result = runtime.stopped_container("docker", {}, self.state)
        self.assertEqual(result["failure"], {"error_type": "TimeoutExpired", "timeout": 10})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.calls[-2][0], ["rm", "--force", self.name])
        self.assertEqual(self.calls[-1][0], ["image", "rm", "--force", self.image])

    def test_interrupted_import_cleans_reserved_tag_without_container_operation(self):
        def operation(*args):
            if args[4] == "stopped-import":
                raise KeyboardInterrupt()
            return self.operation(*args)
        with mock.patch.object(runtime, "docker_operation", side_effect=operation):
            result = runtime.stopped_container("docker", {}, self.state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["error_type"], "KeyboardInterrupt")
        self.assertIsNone(result["owned_container_removed"])
        self.assertEqual(self.calls, [(["image", "rm", "--force", self.tag], "stopped-remove-image", None)])

    def test_cleanup_failure_still_attempts_image_cleanup_and_cannot_pass(self):
        def operation(*args):
            if args[4] == "stopped-remove-container":
                raise runtime.ProbeFailure("Owned container removal failed")
            return self.operation(*args)
        with mock.patch.object(runtime, "docker_operation", side_effect=operation):
            result = runtime.stopped_container("docker", {}, self.state)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["owned_container_removed"])
        self.assertTrue(result["owned_image_removed"])
        self.assertEqual(result["cleanup_failures"][0]["operation"], "stopped-remove-container")

    def test_archive_failure_and_second_inspection_started_state_remain_failures(self):
        for failing_label in ("stopped-archive", "stopped-inspect-after"):
            self.calls = []
            def operation(*args):
                if args[4] == failing_label:
                    if failing_label == "stopped-archive":
                        return b"not a TAR"
                    observed = self.inspected()
                    observed["State"]["StartedAt"] = "2026-01-01T00:00:00Z"
                    return json.dumps(observed).encode()
                return self.operation(*args)
            with self.subTest(label=failing_label), mock.patch.object(runtime, "docker_operation", side_effect=operation):
                result = runtime.stopped_container("docker", {}, self.state)
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["owned_container_removed"] and result["owned_image_removed"])
            if failing_label == "stopped-inspect-after":
                self.assertTrue(result["ever_started_observed"])

    def test_client_is_nonroot_private_and_interruption_reaps_its_group(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(pid=501, returncode=None)
            child.communicate.side_effect = KeyboardInterrupt()
            env = {"DOCKER_HOST": "unix:///private", "DOCKER_CONFIG": "/private/client"}
            with mock.patch.object(runtime.grp, "getgrnam", return_value=mock.Mock(gr_gid=999)), mock.patch.object(runtime.subprocess, "Popen", return_value=child) as spawn, mock.patch.object(runtime, "stop_owned_session") as stop, self.assertRaises(KeyboardInterrupt):
                runtime.docker_operation("docker", ["import", "-", self.tag], env, Path(directory), "stopped-import", b"literal")
            stop.assert_called_once_with(child)
            self.assertEqual(spawn.call_args.kwargs["user"], 1000)
            self.assertEqual(spawn.call_args.kwargs["group"], 1000)
            self.assertEqual(spawn.call_args.kwargs["extra_groups"], [999])
            self.assertEqual(spawn.call_args.kwargs["env"], env)
            self.assertTrue(spawn.call_args.kwargs["start_new_session"] and spawn.call_args.kwargs["close_fds"])
            child.communicate.assert_called_once_with(input=b"literal", timeout=10)

    def test_client_failure_retains_safe_exit_status_without_tool_text(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(pid=502, returncode=7)
            with mock.patch.object(runtime.grp, "getgrnam", return_value=mock.Mock(gr_gid=999)), mock.patch.object(runtime.subprocess, "Popen", return_value=child), mock.patch.object(runtime, "wait_group_gone", return_value=True), self.assertRaises(runtime.ProbeFailure) as caught:
                runtime.docker_operation("docker", ["cp", "private", "-"], {}, Path(directory), "stopped-archive")
            self.assertEqual(runtime.safe_failure(caught.exception), {"error_type": "ProbeFailure", "reason": "stopped-archive did not pass", "returncode": 7})


def metadata_elf(interpreter=runtime.INTERPRETER):
    """Nonexecuted metadata fixture, never a container payload."""
    data = bytearray(512)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, 62, 1, 0x400180, 64, 0, 0,
                     64, 56, 2 if interpreter else 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0x400000, 0, 512, 512, 4096)
    if interpreter:
        raw = interpreter.encode() + b"\0"
        data[256:256 + len(raw)] = raw
        struct.pack_into("<IIQQQQQQ", data, 120, 3, 4, 256, 0, 0, len(raw), len(raw), 1)
    return bytes(data)


PROGRAM_DYNAMIC = b" 0x1 (NEEDED) Shared library: [libc.so.6]\n"
LIBC_DYNAMIC = (b" 0x1 (NEEDED) Shared library: [ld-linux-x86-64.so.2]\n"
                b" 0xe (SONAME) Library soname: [libc.so.6]\n")
LOADER_DYNAMIC = b" 0xe (SONAME) Library soname: [ld-linux-x86-64.so.2]\n"


class RunningFixturePolicyTest(unittest.TestCase):
    def test_only_complete_amd64_glibc_loader_graph_is_accepted(self):
        program = runtime.elf_identity(metadata_elf(), PROGRAM_DYNAMIC, "program")
        self.assertEqual(program["needed"], ["libc.so.6"])
        self.assertEqual(runtime.elf_identity(metadata_elf(), LIBC_DYNAMIC, "libc")["soname"], ["libc.so.6"])
        self.assertEqual(runtime.elf_identity(metadata_elf(None), LOADER_DYNAMIC, "loader")["needed"], [])
        for data in (b"not ELF", metadata_elf()[:100],
                     metadata_elf()[:18] + b"\xb7\0" + metadata_elf()[20:],
                     metadata_elf("/other/loader")):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                runtime.elf_identity(data, PROGRAM_DYNAMIC, "program")

    def test_extra_or_indirect_dynamic_dependencies_are_rejected(self):
        for suffix in (b" 0x1 (NEEDED) Shared library: [libextra.so]\n",
                       b" 0x1d (RUNPATH) Library runpath: [/unreviewed]\n",
                       b" 0x1 (NEEDED) unexpected syntax\n",
                       b" 0x1 (FILTER) Filter library: [other]\n"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                runtime.elf_identity(metadata_elf(), PROGRAM_DYNAMIC + suffix, "program")
        with self.assertRaises(ValueError):
            runtime.elf_identity(metadata_elf(None), PROGRAM_DYNAMIC, "program")
        with self.assertRaises(ValueError):
            runtime.elf_identity(metadata_elf(None), LIBC_DYNAMIC, "loader")

    def info(self, **changes):
        values = dict(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o755, st_uid=0, st_gid=0,
                      st_size=len(metadata_elf()), st_mtime_ns=3, st_ctime_ns=4)
        return SimpleNamespace(**dict(values, **changes))

    def test_installed_bytes_are_bounded_stable_and_inspected_through_the_held_fd(self):
        data, info = metadata_elf(), self.info()
        with mock.patch.object(runtime, "no_links"), mock.patch.object(runtime.os, "open", return_value=7), \
             mock.patch.object(runtime.os, "close") as close, mock.patch.object(runtime.os, "fstat", return_value=info), \
             mock.patch.object(runtime.Path, "lstat", return_value=info), \
             mock.patch.object(runtime.os, "read", side_effect=[data, b""]), \
             mock.patch.object(runtime, "bounded", return_value=PROGRAM_DYNAMIC) as readelf:
            actual, record = runtime.read_system_elf("/usr/bin/sleep", "program", Path("/private"))
        self.assertEqual(actual, data)
        self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(readelf.call_args.args[0], ["/usr/bin/readelf", "--wide", "--dynamic", "/proc/self/fd/7"])
        self.assertEqual(readelf.call_args.kwargs["pass_fds"], (7,))
        close.assert_called_once_with(7)

    def test_nonregular_mutable_oversized_or_changed_fixture_is_refused(self):
        for changes in ({"st_mode": stat.S_IFIFO | 0o600}, {"st_uid": 1000},
                        {"st_mode": stat.S_IFREG | 0o777}, {"st_size": runtime.ELF_FILE_CAP + 1}):
            with self.subTest(changes=changes), mock.patch.object(runtime, "no_links"), \
                 mock.patch.object(runtime.os, "open", return_value=7), mock.patch.object(runtime.os, "close"), \
                 mock.patch.object(runtime.os, "fstat", return_value=self.info(**changes)), \
                 mock.patch.object(runtime.os, "read") as read, self.assertRaises(ValueError):
                runtime.read_system_elf("/usr/bin/sleep", "program", Path("/private"))
            read.assert_not_called()
        with mock.patch.object(runtime, "no_links"), mock.patch.object(runtime.os, "open", return_value=7), \
             mock.patch.object(runtime.os, "close"), mock.patch.object(runtime.os, "fstat",
             side_effect=[self.info(), self.info(st_mtime_ns=8)]), \
             mock.patch.object(runtime.os, "read", side_effect=[metadata_elf(), b""]), \
             mock.patch.object(runtime, "bounded", return_value=PROGRAM_DYNAMIC), self.assertRaises(ValueError):
            runtime.read_system_elf("/usr/bin/sleep", "program", Path("/private"))

    def test_tar_preserves_only_captured_bytes_and_is_deterministic(self):
        def read(source, role, state):
            data = role.encode()
            return data, {"sha256": hashlib.sha256(data).hexdigest(), "source_path": source, "bytes": len(data)}
        with mock.patch.object(runtime, "read_system_elf", side_effect=read):
            payload, proof = runtime.executable_rootfs(Path("/private"))
            again, unused = runtime.executable_rootfs(Path("/private"))
        self.assertEqual(payload, again)
        self.assertFalse(proof["compiled"] or proof["downloaded"] or proof["package_generated"])
        with runtime.tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            files = {member.name: member for member in archive if member.isfile()}
            self.assertEqual(set(files), {"usr/bin/sleep", "lib/x86_64-linux-gnu/libc.so.6",
                                         "lib64/ld-linux-x86-64.so.2", "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"})
            for name, member in files.items():
                self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o555, 0, 0, 0))
            self.assertEqual(archive.extractfile(files["usr/bin/sleep"]).read(), b"program")
            self.assertEqual(archive.extractfile(files["lib64/ld-linux-x86-64.so.2"]).read(), b"loader")

    def test_root_source_is_separate_and_hash_pinned_to_fe43(self):
        self.assertEqual(runtime.NATIVE_CONTROLLER, "fe43e7a04cb1734c450091dae10fc555a296b1c0")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "controller"
            native = root.with_name(root.name + "-native-cleanup")
            source = native / "tools/bazel/ci/source_image_worker.py"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source fixture")
            with mock.patch.object(runtime, "ROOT", root), mock.patch.object(runtime, "NATIVE_WORKER_SHA256",
                    hashlib.sha256(source.read_bytes()).hexdigest()):
                self.assertEqual(runtime.native_stop_source(), native)
                source.write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    runtime.native_stop_source()

    def test_owned_child_reaper_does_not_wait_for_unrelated_processes(self):
        child, previous = mock.Mock(), object()
        with mock.patch.object(runtime.signal, "getsignal", return_value=previous), \
             mock.patch.object(runtime.signal, "signal") as set_signal:
            with runtime.reap_owned_daemon(child):
                handler = set_signal.call_args_list[0].args[1]
                handler(runtime.signal.SIGCHLD, None)
        self.assertEqual(child.poll.call_count, 3)
        self.assertEqual(set_signal.call_args_list[-1], mock.call(runtime.signal.SIGCHLD, previous))

    def test_exact_native_stop_receives_real_private_state_and_failure_is_not_success(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state"
                execution = Path(directory) / "exec"
                state.mkdir()
                execution.mkdir()
                data = state / "source-image-docker-123-1"
                paths = {"state": state, "exec": execution, "docker_data": data,
                         "identity": {"worker": "ks-fixture", "run": "123", "attempt": "1"}}
                with mock.patch.object(runtime.os, "chown"):
                    runtime.native_control(paths, Path("/native-cleanup"))
                private_path = state / "native-stop-control/worker-private.json"
                private = json.loads(private_path.read_text())
                private["docker_mount_protocol"] = 1  # Mock only the completed shared capture.
                private_path.write_text(json.dumps(private))
                data.mkdir()
                pid = execution / "dockerd.pid"
                pid.write_text("321\n")
                child = mock.Mock(pid=321)
                child.poll.return_value = 0
                def execute(*args, **kwargs):
                    if failed:
                        raise runtime.ProbeFailure("Private mount remains")
                    shutil.rmtree(data)
                    pid.unlink()
                with mock.patch.object(runtime, "reap_owned_daemon"), \
                     mock.patch.object(runtime, "bounded", side_effect=execute) as run:
                    if failed:
                        with self.assertRaises(runtime.ProbeFailure):
                            runtime.native_stop(child, paths, Path("/native-cleanup"))
                    else:
                        result = runtime.native_stop(child, paths, Path("/native-cleanup"))
                        self.assertEqual(result["controller"], runtime.NATIVE_CONTROLLER)
                        self.assertTrue(result["prospective_mount_proof"])
                        self.assertFalse(result["native_request_or_receipt_created"])
                args, kwargs = run.call_args
                self.assertEqual(args[0][:4], [runtime.sys.executable, "-B",
                                 "/native-cleanup/tools/bazel/ci/source_image_worker.py", "stop"])
                self.assertIn(runtime.NATIVE_CONTROLLER, args[0])
                self.assertEqual(args[3], 240)
                self.assertEqual(args[4]["RUNNER_TEMP"], str(state))
                self.assertEqual(args[4]["WORKER_POOL"], runtime.inspection.POOL)
                self.assertTrue(kwargs["worker_user"])
                self.assertEqual(data.exists(), failed)



class RunningContainerTest(unittest.TestCase):
    def setUp(self):
        self.state = Path("/actions/_work/_temp/vs-runtime-" + "a" * 24)
        self.name = "sonic-vs-running-" + "a" * 24
        self.image, self.container = "sha256:" + "b" * 64, "c" * 64
        self.calls = []

    def inspected(self, stopped=False):
        return {"Id": self.container, "Image": self.image, "Name": "/" + self.name,
                "Config": {"Cmd": ["/usr/bin/sleep", "60"], "User": "65534:65534", "Env": ["LC_ALL=C"]},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False, "Binds": None,
                               "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "PidsLimit": 8,
                               "Memory": 32 * 1024**2, "NanoCpus": 250000000},
                "State": {"Status": "exited" if stopped else "running", "Running": not stopped,
                          "Pid": 0 if stopped else 123, "StartedAt": "2026-01-01T00:00:00Z",
                          "FinishedAt": "2026-01-01T00:00:01Z" if stopped else "0001-01-01T00:00:00Z",
                          "ExitCode": 143 if stopped else 0, "OOMKilled": False, "Dead": False,
                          "Paused": False, "Restarting": False, "Error": ""}}

    def operation(self, executable, arguments, environment, state, label, payload):
        self.calls.append((arguments, label, payload))
        return {"running-import": self.image.encode(), "running-create": self.container.encode(),
                "running-start": self.container.encode(), "running-inspect": json.dumps(self.inspected()).encode(),
                "running-stop": self.container.encode(),
                "running-inspect-stopped": json.dumps(self.inspected(True)).encode(),
                "running-remove-container": self.container.encode(), "running-remove-image": b"Deleted"}[label]

    def invoke(self, operation=None):
        with mock.patch.object(runtime, "executable_rootfs", return_value=(b"existing bytes", {"files": []})), \
             mock.patch.object(runtime, "docker_operation", side_effect=operation or self.operation):
            return runtime.running_container("docker", {"DOCKER_HOST": "private"}, self.state)

    def test_real_running_then_normal_term_is_required_before_success(self):
        result = self.invoke()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["running_observed"] and result["normal_stop_verified"])
        self.assertEqual([row[1] for row in self.calls], list(runtime.RUNNING_LOGS))
        create = self.calls[1][0]
        for flag in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--pids-limit=8", "--memory=32m",
                     "--cpus=0.25", "--user=65534:65534"):
            self.assertIn(flag, create)
        self.assertEqual(self.calls[4][0], ["stop", "--timeout", "5", self.container])
        self.assertTrue(result["owned_container_removed"] and result["owned_image_removed"])

    def test_unexpected_stop_stdout_fails_and_owned_cleanup_still_runs(self):
        def operation(*args):
            reply = self.operation(*args)
            if args[4] == "running-stop":
                return b"unexpected diagnostic\n" + reply
            return reply
        result = self.invoke(operation)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["reason"], "Running fixture stop response differs")
        self.assertTrue(result["running_observed"])
        self.assertFalse(result["normal_stop_verified"])
        self.assertNotIn("running-inspect-stopped", [row[1] for row in self.calls])
        self.assertEqual(self.calls[4][0], ["stop", "--timeout", "5", self.container])
        self.assertEqual([row[0] for row in self.calls[-2:]],
                         [["rm", "--force", self.container], ["image", "rm", "--force", self.image]])
        self.assertTrue(result["owned_container_removed"] and result["owned_image_removed"])

    def test_wrong_network_or_unexpected_exit_cannot_pass(self):
        for mode in ("network", "killed"):
            def operation(*args):
                if args[4] == ("running-inspect" if mode == "network" else "running-inspect-stopped"):
                    row = self.inspected(mode == "killed")
                    if mode == "network":
                        row["HostConfig"]["NetworkMode"] = "host"
                    else:
                        row["State"]["ExitCode"] = 42
                    return json.dumps(row).encode()
                return self.operation(*args)
            with self.subTest(mode=mode):
                result = self.invoke(operation)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["normal_stop_verified"])

    def test_interrupted_create_cleans_only_reserved_name_and_owned_image(self):
        def operation(*args):
            if args[4] == "running-create":
                raise KeyboardInterrupt()
            return self.operation(*args)
        result = self.invoke(operation)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.calls[-2][0], ["rm", "--force", self.name])
        self.assertEqual(self.calls[-1][0], ["image", "rm", "--force", self.image])

    def test_malformed_import_reply_cannot_select_an_unowned_cleanup_target(self):
        def operation(*args):
            if args[4] == "running-import":
                return b"unrelated-image"
            return self.operation(*args)
        result = self.invoke(operation)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.calls, [(["image", "rm", "--force", self.name + ":fixture"],
                                      "running-remove-image", None)])

    def test_stop_failure_and_cleanup_failure_remain_failure(self):
        def operation(*args):
            if args[4] in ("running-stop", "running-remove-container"):
                raise runtime.ProbeFailure("Owned operation failed")
            return self.operation(*args)
        result = self.invoke(operation)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["running_observed"])
        self.assertFalse(result["normal_stop_verified"] or result["owned_container_removed"])
        self.assertTrue(result["owned_image_removed"])


    def test_pid1_bounded_kill_is_recorded_without_graceful_or_oom_claim(self):
        def operation(*args):
            if args[4] == "running-inspect-stopped":
                row = self.inspected(True)
                row["State"]["ExitCode"] = 137
                return json.dumps(row).encode()
            return self.operation(*args)
        result = self.invoke(operation)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 137)
        self.assertFalse(result["graceful_TERM_claimed"])
        row = self.inspected(True)
        row["State"]["OOMKilled"] = True
        with self.assertRaises(ValueError):
            runtime.running_identity(json.dumps(row).encode(), self.name, self.container, self.image, True)


class NativeStopDiagnosticsTest(unittest.TestCase):
    def test_only_the_closed_native_mount_schema_is_exposed(self):
        row = {"status": "failed", "phase": "private_docker_mount_teardown", "wait_limit_seconds": 30,
               "mount_count": 1, "truncated": False, "reported_mounts": [{"mount_id": 7, "parent_id": 3,
               "filesystem": "overlay", "location": "overlay2", "relative_depth": 3,
               "relative_path_sha256": "a" * 64}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-stop.stderr"
            path.write_text("private tool stderr is not public\n" + json.dumps(row) + "\nAssertionError\n")
            self.assertEqual(runtime.native_mount_diagnostics(Path(directory)), row)
            row["reported_mounts"][0]["raw_path"] = "/private/not-public"
            path.write_text(json.dumps(row))
            self.assertIsNone(runtime.native_mount_diagnostics(Path(directory)))

    def test_missing_malformed_duplicate_or_oversized_diagnostics_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertIsNone(runtime.native_mount_diagnostics(state))
            for value in ('{malformed', '{"phase":"private_docker_mount_teardown","phase":"duplicate"}',
                          'x' * (runtime.CAP + 1)):
                (state / "native-stop.stderr").write_text(value)
                self.assertIsNone(runtime.native_mount_diagnostics(state))



class NativeCaptureLifecycleTest(unittest.TestCase):
    def paths(self, directory):
        state, execution = Path(directory) / "state", Path(directory) / "exec"
        state.mkdir(mode=0o700)
        return {"state": state, "exec": execution,
                "docker_data": state / "source-image-docker-123-1",
                "identity": {"worker": "ks-fixture", "run": "123", "attempt": "1"}}

    def test_captures_share_real_endpoint_identity_and_pinned_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            private_path = paths["state"] / "native-stop-control/worker-private.json"
            def execute(command, state, label, seconds, env, **kwargs):
                self.assertEqual(seconds, 40)
                self.assertEqual(command[:3], [runtime.sys.executable, "-B",
                                 "/native-cleanup/tools/bazel/ci/source_image_worker.py"])
                self.assertEqual(env["RUNNER_TEMP"], str(paths["state"]))
                self.assertEqual(env["RUNNER_NAME"], "ks-fixture")
                self.assertEqual(env["GITHUB_RUN_ID"], "123")
                self.assertEqual(env["GITHUB_RUN_ATTEMPT"], "1")
                self.assertEqual(env["WORKER_POOL"], runtime.inspection.POOL)
                self.assertTrue(kwargs["worker_user"])
                private = json.loads(private_path.read_text())
                self.assertEqual(private["revision"], runtime.NATIVE_CONTROLLER)
                self.assertEqual(private["workspace"], "/native-cleanup")
                self.assertEqual(private["image"], "gearbox")
                self.assertEqual(private["docker_root"], str(paths["docker_data"]))
                self.assertEqual(private["pidfile"], str(paths["exec"] / "dockerd.pid"))
                self.assertEqual(private["environment"], {
                    "DOCKER_HOST": "unix://" + str(paths["exec"] / "docker.sock"),
                    "DOCKER_CONFIG": str(paths["state"] / "client")})
                if command[3] == "capture-docker-mount-before":
                    self.assertFalse(paths["docker_data"].exists())
                    self.assertNotIn("docker_mount_protocol", private)
                else:
                    self.assertEqual(command[3], "capture-docker-mount-ready")
                    private["docker_mount_protocol"] = 1
                    private_path.write_text(json.dumps(private))
            with mock.patch.object(runtime.os, "chown"), mock.patch.object(runtime, "bounded", side_effect=execute) as run:
                before = runtime.native_capture("before", paths, Path("/native-cleanup"))
                paths["docker_data"].mkdir()  # Mock daemon creation between the two captures.
                ready = runtime.native_capture("ready", paths, Path("/native-cleanup"))
            self.assertEqual([row.args[0][3] for row in run.call_args_list],
                             ["capture-docker-mount-before", "capture-docker-mount-ready"])
            self.assertEqual((before["phase"], ready["phase"]), ("before", "ready"))
            self.assertFalse(before["native_request_or_receipt_created"] or ready["native_request_or_receipt_created"])

    def test_missing_ready_protocol_cannot_claim_proved_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(directory)
            with mock.patch.object(runtime.os, "chown"), mock.patch.object(runtime, "bounded") as run:
                runtime.native_capture("before", paths, Path("/native-cleanup"))
                with self.assertRaises(ValueError):
                    runtime.native_capture("ready", paths, Path("/native-cleanup"))
                with self.assertRaises(ValueError):
                    runtime.native_stop(mock.Mock(), paths, Path("/native-cleanup"))
            self.assertEqual(run.call_count, 2)
            self.assertNotIn("stop", [row.args[0][3] for row in run.call_args_list])

    def test_preexisting_and_wrong_scope_roots_refuse_before_capture(self):
        for mode in ("existing", "wrong-name", "wrong-parent"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                paths = self.paths(directory)
                if mode == "existing":
                    paths["docker_data"].mkdir()
                elif mode == "wrong-name":
                    paths["docker_data"] = paths["state"] / "unrelated"
                else:
                    paths["docker_data"] = Path(directory) / "source-image-docker-123-1"
                with mock.patch.object(runtime, "bounded") as run, self.assertRaises(ValueError):
                    runtime.native_capture("before", paths, Path("/native-cleanup"))
                run.assert_not_called()
                self.assertFalse((paths["state"] / "native-stop-control").exists())

    def test_supervisor_capture_order_and_failure_preserve_owned_cleanup(self):
        for failure in (None, "before", "ready"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                paths = self.paths(directory)
                child = mock.Mock(pid=321, returncode=None)
                child.poll.return_value = None
                events = []
                def capture(stage, *_):
                    events.append(stage)
                    if stage == "before":
                        self.assertFalse(paths["docker_data"].exists())
                    if stage == failure:
                        raise runtime.ProbeFailure("Capture failed")
                    return {"status": "passed"}
                def spawn(*args, **kwargs):
                    events.append("spawn")
                    paths["docker_data"].mkdir()
                    (paths["exec"] / "dockerd.pid").write_text("321\n")
                    return child
                def stopped(*_):
                    events.append("stopped")
                    return {"status": "passed", "ever_started_observed": False}
                def running(*_):
                    events.append("running")
                    return {"status": "passed", "running_observed": True}
                def native_stop(*_):
                    events.append("native-stop")
                    if failure:
                        raise runtime.ProbeFailure("Missing ready proof")
                    child.returncode = 0
                    return {"status": "passed"}
                def fallback(owned):
                    self.assertIs(owned, child)
                    events.append("fallback")
                    child.returncode = 0
                    return True
                original_stat = Path.stat
                def private_stat(path, *args, **kwargs):
                    info = original_stat(path, *args, **kwargs)
                    if path == paths["state"]:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=1000, st_gid=1000)
                    return info
                with ExitStack() as stack:
                    for patch in (
                        mock.patch.object(runtime.os, "geteuid", return_value=0),
                        mock.patch.object(runtime.Path, "stat", autospec=True, side_effect=private_stat),
                        mock.patch.object(runtime.Path, "is_file", return_value=True),
                        mock.patch.object(runtime.inspection, "marker", return_value={"base_image": runtime.BASE_IMAGE, "manager_revision": runtime.MANAGER}),
                        mock.patch.object(runtime, "native_stop_source", return_value=Path("/native-cleanup")),
                        mock.patch.object(runtime, "native_capture", side_effect=capture),
                        mock.patch.object(runtime, "native_stop", side_effect=native_stop),
                        mock.patch.object(runtime, "native_mount_diagnostics", return_value=None),
                        mock.patch.object(runtime, "checked_helper", return_value=Path("/reviewed")),
                        mock.patch.object(runtime.shutil, "which", return_value="reviewed"),
                        mock.patch.object(runtime.os, "fstat", return_value=mock.Mock(st_mode=stat.S_IFIFO)),
                        mock.patch.object(runtime.os, "set_blocking"),
                        mock.patch.object(runtime.os, "chown"),
                        mock.patch.object(runtime.signal, "signal"),
                        mock.patch.object(runtime.signal, "setitimer"),
                        mock.patch.object(runtime, "root_lifeline_tick"),
                        mock.patch.object(runtime, "bounded"),
                        mock.patch.object(runtime.subprocess, "Popen", side_effect=spawn),
                        mock.patch.object(runtime, "docker_information", return_value={}),
                        mock.patch.object(runtime, "validate_daemon", return_value="28.5.2"),
                        mock.patch.object(runtime, "stopped_container", side_effect=stopped),
                        mock.patch.object(runtime, "running_container", side_effect=running),
                        mock.patch.object(runtime, "stop_owned_daemon", side_effect=fallback),
                        mock.patch.object(runtime, "diagnostics", return_value=[]),
                    ):
                        stack.enter_context(patch)
                    result = runtime.private_daemon(paths)
                if failure == "before":
                    self.assertEqual(events, ["before"])
                    self.assertFalse(result["daemon_started"])
                elif failure == "ready":
                    self.assertEqual(events, ["before", "spawn", "ready", "native-stop", "fallback"])
                    self.assertTrue(result["daemon_closed"])
                else:
                    self.assertEqual(events, ["before", "spawn", "ready", "stopped", "running", "native-stop"])
                    self.assertTrue(result["daemon_closed"])
                self.assertEqual(result["status"], "failed" if failure else "passed")

    def test_cleanup_and_job_budgets_cover_nested_deadlines(self):
        cleanup_bound = 20 + 2 * 31 + runtime.NATIVE_STOP_SECONDS + 20 + 35
        self.assertGreaterEqual(runtime.ROOT_CLEANUP_SECONDS, cleanup_bound)
        self.assertGreaterEqual(runtime.ROOT_BODY_SECONDS, 160 + 2 * runtime.NATIVE_CAPTURE_SECONDS)
        self.assertGreaterEqual(runtime.ROOT_COMMAND_SECONDS, runtime.ROOT_BODY_SECONDS + cleanup_bound)
        workflow = (runtime.ROOT / ".github/workflows/bazel-oci.yml").read_text()
        block = workflow.split("  vs_runtime_qualification:\n", 1)[1].split("\n  registry_native:", 1)[0]
        minutes = int(re.search(r"timeout-minutes: (\d+)", block).group(1))
        self.assertGreaterEqual(minutes * 60, 300 + runtime.ROOT_COMMAND_SECONDS + runtime.ROOT_CLEANUP_SECONDS + 120)


class SafeTracebackDiagnosticTest(unittest.TestCase):
    BEFORE = "native-capture-docker-mount-before.stderr"

    def test_native_guard_and_known_numeric_frames_are_recorded_without_paths(self):
        text = (b'Traceback (most recent call last):\n'
                b'  File "/private/worker/source_image_worker.py", line 1200, in main\n'
                b'  File "<string>", line 204, in capture\n'
                b'  File "/private/unknown.py", line 88, in secret\n'
                b'ValueError: Docker root must be absent before launch\n')
        result = runtime.traceback_diagnostics(text, self.BEFORE)
        self.assertEqual(result["source_frames"], [{"source": "native_worker", "line": 1200},
                                                 {"source": "native_embedded_python", "line": 204}])
        self.assertEqual(result["exception_facts"], [{"error_type": "ValueError"}])
        self.assertEqual(result["authored_guard_reasons"], ["Docker root must be absent before launch"])
        self.assertNotIn("/private/", json.dumps(result))
        self.assertNotIn("unknown.py", json.dumps(result))

    def test_called_process_error_repr_and_source_context_do_not_classify(self):
        program = ('timeout = 10\nraise ValueError("Private Docker still has a mounted child")\n'
                   'Traceback (most recent call last):\nValueError: Docker root must be absent before launch\n')
        command = ["python3", "-c", program, "https://private.invalid/?token=secret-value"]
        text = ('Traceback (most recent call last):\n'
                '    raise ValueError("Docker root must be absent before launch")\n'
                'subprocess.CalledProcessError: Command ' + repr(command) + ' returned non-zero exit status 1.\n').encode()
        result = runtime.traceback_diagnostics(text, self.BEFORE)
        for name in ("known_errors", "exception_facts", "authored_guard_reasons", "source_frames"):
            self.assertEqual(result[name], [])
        self.assertNotIn("secret-value", json.dumps(result))

    def test_actual_inner_error_survives_outer_large_command_repr(self):
        text = (b'Traceback (most recent call last):\n'
                b'  File "<string>", line 55, in write_record\n'
                b'PermissionError: [Errno 13] https://private.invalid/secret /private/path\n'
                b'Traceback (most recent call last):\n'
                b'subprocess.CalledProcessError: Command ' + b'x' * 30000 + b'timeout mounted child\n')
        result = runtime.traceback_diagnostics(text, self.BEFORE)
        self.assertEqual(result["exception_facts"], [{"error_type": "PermissionError", "errno": 13}])
        self.assertEqual(result["known_errors"], ["permission_denied"])
        self.assertEqual(result["source_frames"], [{"source": "native_embedded_python", "line": 55}])
        self.assertNotIn("secret", json.dumps(result))

    def test_guard_reason_requires_exact_stage_type_and_entire_message(self):
        for stage, kind, reason in (
                ("native-stop.stderr", "ValueError", "Docker root must be absent before launch"),
                (self.BEFORE, "RuntimeError", "Docker root must be absent before launch"),
                (self.BEFORE, "ValueError", "Docker root must be absent before launch /secret"),
                (self.BEFORE, "ValueError", "Startup mount context changed")):
            with self.subTest(stage=stage, kind=kind, reason=reason):
                text = ('Traceback (most recent call last):\n' + kind + ': ' + reason + '\n').encode()
                result = runtime.traceback_diagnostics(text, stage)
                self.assertEqual(result["authored_guard_reasons"], [])
                self.assertNotIn(reason, json.dumps(result))

    def test_plain_keywords_and_truncated_first_line_cannot_become_tracebacks(self):
        text = (b'Traceback (most recent call last):\nValueError: Docker root must be absent before launch\n'
                b'Private Docker still has a mounted child timeout\n')
        result = runtime.traceback_diagnostics(text, self.BEFORE, leading_fragment=True)
        self.assertEqual(result["exception_facts"], [])
        self.assertEqual(result["known_errors"], [])
        self.assertEqual(result["authored_guard_reasons"], [])
        no_banner = runtime.traceback_diagnostics(b'PermissionError: [Errno 13] /secret\n', self.BEFORE)
        self.assertEqual(no_banner["exception_facts"], [])

    def test_frame_records_are_bounded_deduplicated_and_unknown_names_are_opaque(self):
        text = b'Traceback (most recent call last):\n'
        for number in range(20):
            text += ('  File "<string>", line ' + str(number + 1) + ', in capture\n').encode()
        text += b'  File "<string>", line 1, in capture\nValueError: arbitrary secret text\n'
        result = runtime.traceback_diagnostics(text, self.BEFORE)
        self.assertEqual(len(result["source_frames"]), 12)
        self.assertTrue(result["records_truncated"])
        self.assertEqual(result["exception_facts"], [{"error_type": "ValueError"}])
        self.assertEqual(result["authored_guard_reasons"], [])
        self.assertNotIn("arbitrary", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
