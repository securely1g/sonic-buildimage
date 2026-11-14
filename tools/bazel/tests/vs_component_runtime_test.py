#!/usr/bin/env python3
"""Exercise the real component supervisor branch with mocked external boundaries."""
import ast
from contextlib import ExitStack, nullcontext
import io
import json
from pathlib import Path
import signal
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[3] / "tools/bazel/ci/vs_component_runtime_owner.py"


def namespace():
    # Same selected-AST convention as vs_full_reuse_supervisor_test.py.
    functions = {"require", "daemon_arguments", "native_identity", "native_action", "private_daemon"}
    constants = {"PATH", "BASE_IMAGE", "MANAGER", "NESTED_HELPER_SHA256", "NATIVE_CONTROLLER",
                 "NATIVE_WORKER_SHA256", "ORIGINAL_P4RT_NATIVE_CONTROLLER", "ROOT_BODY_SECONDS",
                 "ORIGINAL_P4RT_BODY_SECONDS", "FULL_VS_BODY_SECONDS", "COMPONENT_BODY_SECONDS"}
    body = []
    for node in ast.parse(SOURCE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "ProbeFailure":
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            body.append(node)
    values = {"Path": Path, "stat": stat, "sys": sys, "ExitStack": ExitStack, "json": json}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), values)
    return values


class ComponentRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.n = namespace()
        self.paths = {"state": Path("/actions/_work/_temp/vs-runtime-fixed-eventd"),
                      "exec": Path("/run/sonic-vs-runtime-fixed-eventd"),
                      "docker_data": Path("/actions/_work/_temp/vs-runtime-fixed-eventd/source-image-docker-12-1"),
                      "identity": {"worker": "fixed-worker", "run": "12", "attempt": "1"},
                      "component_runtime": True}
        self.child = SimpleNamespace(pid=78, returncode=None, poll=lambda: None)
        self.fake_os = SimpleNamespace(
            environ={"HOME": "/actual/invoking/home", "SECRET_TOKEN": "never-forward"},
            geteuid=mock.Mock(return_value=0), chown=mock.Mock(),
            fstat=mock.Mock(return_value=SimpleNamespace(st_mode=stat.S_IFIFO)),
            set_blocking=mock.Mock())
        self.popen = mock.Mock(return_value=self.child)
        self.clock = SimpleNamespace(monotonic=mock.Mock(return_value=100), sleep=mock.Mock())
        self.signals = SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT,
                                       SIGALRM=signal.SIGALRM, ITIMER_REAL=signal.ITIMER_REAL,
                                       signal=mock.Mock(return_value="prior"), setitimer=mock.Mock())
        self.n.update(
            ROOT=Path("/reviewed"), os=self.fake_os, time=self.clock, signal=self.signals,
            subprocess=SimpleNamespace(Popen=self.popen, DEVNULL=subprocess.DEVNULL,
                                       TimeoutExpired=subprocess.TimeoutExpired),
            inspection=SimpleNamespace(POOL="fixed-pool", marker=lambda: {
                "base_image": self.n["BASE_IMAGE"], "manager_revision": self.n["MANAGER"]}),
            shutil=SimpleNamespace(which=lambda tool, **kwargs: "/usr/bin/" + tool),
            no_links=mock.Mock(), checked_helper=lambda path, digest: path,
            native_stop_source=mock.Mock(return_value=Path("/reviewed-native-cleanup")),
            native_capture=mock.Mock(return_value={"status": "passed"}),
            native_stop=mock.Mock(side_effect=self.stop), stop_owned_daemon=mock.Mock(side_effect=self.fallback),
            native_mount_diagnostics=mock.Mock(return_value=None),
            bounded=mock.Mock(return_value=b""), root_lifeline_tick=mock.Mock(),
            cleanup_window=nullcontext, docker_information=mock.Mock(return_value={"ready": True}),
            validate_daemon=mock.Mock(return_value="fixed-version"), diagnostics=mock.Mock(return_value={}),
            safe_failure=lambda error: str(error),
            stopped_container=mock.Mock(return_value={"status": "passed", "ever_started_observed": False}),
            running_container=mock.Mock(return_value={"status": "passed", "running_observed": True}))
        self.owner_result = {"status": "passed", "family": "eventd", "owner_checks": ["real-callback-result"]}
        self.callback = mock.Mock(return_value=self.owner_result)

    def stop(self, child, paths, native):
        self.assertIs(child, self.child)
        self.assertIs(paths, self.paths)
        self.assertEqual(native, Path("/reviewed-native-cleanup"))
        child.returncode = 0
        return {"status": "passed", "daemon_reaped": True}

    def fallback(self, child):
        child.returncode = 0
        return True

    def filesystem(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(Path, "is_file", return_value=True))
        stack.enter_context(mock.patch.object(Path, "is_symlink", return_value=False))
        stack.enter_context(mock.patch.object(Path, "exists", return_value=False))
        stack.enter_context(mock.patch.object(Path, "stat", return_value=SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_gid=1000)))
        stack.enter_context(mock.patch.object(Path, "mkdir"))
        stack.enter_context(mock.patch.object(Path, "read_text", return_value="78"))
        stack.enter_context(mock.patch.object(Path, "open", autospec=True,
            side_effect=lambda path, mode: io.BytesIO() if "b" in mode else io.StringIO()))
        return stack

    def run_component(self):
        with self.filesystem():
            return self.n["private_daemon"](self.paths, component_workload=self.callback)

    def test_callback_preserves_home_and_isolated_client_and_closes_daemon(self):
        value = self.run_component()
        self.assertEqual(value["status"], "passed")
        self.assertIs(value["component_runtime"], self.owner_result)
        self.assertTrue(value["container_workload_executed"])
        self.assertTrue(value["daemon_closed"])
        self.callback.assert_called_once()
        docker, environment, paths = self.callback.call_args.args
        self.assertEqual(docker, "/usr/bin/docker")
        self.assertIs(paths, self.paths)
        self.assertEqual(environment, {"PATH": self.n["PATH"], "HOME": "/actual/invoking/home",
            "DOCKER_CONFIG": str(self.paths["state"] / "client"),
            "DOCKER_HOST": "unix://" + str(self.paths["exec"] / "docker.sock")})
        self.assertEqual(self.popen.call_args.kwargs["env"],
                         {"PATH": self.n["PATH"], "HOME": "/actual/invoking/home"})
        argv = self.popen.call_args.args[0]
        self.assertIn("--bridge=none", argv)
        self.assertIn("--iptables=false", argv)
        self.assertNotIn("unix:///var/run/docker.sock", argv)
        self.n["native_stop"].assert_called_once()
        self.assertEqual([c.args[0] for c in self.n["native_capture"].call_args_list], ["before", "ready"])
        self.n["stopped_container"].assert_not_called()
        self.n["running_container"].assert_not_called()

    def test_absent_home_stays_absent_in_all_component_child_environments(self):
        self.fake_os.environ.pop("HOME")
        self.assertEqual(self.run_component()["status"], "passed")
        self.assertNotIn("HOME", self.callback.call_args.args[1])
        self.assertNotIn("HOME", self.popen.call_args.kwargs["env"])
        self.n["native_action"]("stop", self.paths, Path("/reviewed-native-cleanup"), 240)
        self.assertNotIn("HOME", self.n["bounded"].call_args.args[4])
        self.assertNotIn("SECRET_TOKEN", self.n["bounded"].call_args.args[4])

    def test_missing_false_or_conflicting_component_selection_stops_before_state(self):
        cases = [({}, self.callback, {}), ({"component_runtime": False}, self.callback, {}),
                 ({"component_runtime": True}, None, {}), ({"component_runtime": True}, 7, {}),
                 ({"component_runtime": True, "original_p4rt_loaded": True}, self.callback,
                  {"p4rt_workload": lambda *args: {"status": "passed"}}),
                 ({"component_runtime": True, "full_vs_reuse": True}, self.callback,
                  {"full_vs_workload": lambda *args: {"status": "passed"}})]
        for flags, callback, other in cases:
            with self.subTest(flags=flags), self.assertRaises(self.n["ProbeFailure"]):
                self.n["private_daemon"](flags, component_workload=callback, **other)
        self.fake_os.geteuid.assert_not_called()
        self.popen.assert_not_called()

    def test_failed_owner_result_still_runs_exact_native_cleanup(self):
        self.callback.return_value = {"status": "failed", "family": "eventd"}
        value = self.run_component()
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["failure"], "Component runtime validation failed")
        self.assertTrue(value["daemon_closed"])
        self.assertFalse(value["container_workload_executed"])
        self.n["native_stop"].assert_called_once()
        self.signals.setitimer.assert_any_call(signal.ITIMER_REAL, 0)

    def test_owner_exception_still_closes_private_daemon(self):
        self.callback.side_effect = ValueError("fixed owner rejection")
        value = self.run_component()
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["failure"], "fixed owner rejection")
        self.assertTrue(value["daemon_closed"])
        self.n["native_stop"].assert_called_once()

    def test_native_cleanup_failure_cannot_be_hidden_by_passing_owner(self):
        self.n["native_stop"].side_effect = ValueError("fixed cleanup failure")
        value = self.run_component()
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["native_stop"]["status"], "failed")
        self.assertTrue(value["daemon_closed"])
        self.n["stop_owned_daemon"].assert_called_once_with(self.child)

    def test_component_body_budget_keeps_existing_supervisor_lifeline(self):
        self.assertEqual(self.run_component()["status"], "passed")
        self.n["root_lifeline_tick"].assert_called_once_with(1300)
        self.assertEqual(self.n["COMPONENT_BODY_SECONDS"], 1200)
        self.signals.setitimer.assert_any_call(signal.ITIMER_REAL, 1, 1)
        self.clock.sleep.assert_not_called()

    def test_capture_and_stop_keep_original_pin_and_only_forward_actual_home(self):
        for action, seconds in [("capture-docker-mount-before", 40),
                                ("capture-docker-mount-ready", 40), ("stop", 240)]:
            self.n["native_action"](action, self.paths, Path("/reviewed-native-cleanup"), seconds)
            argv, state, label, observed_seconds, environment = self.n["bounded"].call_args.args
            self.assertEqual(argv[3:8], [action, "--image", "gearbox", "--revision",
                "ba575106ea25c3a5f14a428b0326b6a3c891b5ef"])
            self.assertEqual(environment["HOME"], "/actual/invoking/home")
            self.assertNotIn("SECRET_TOKEN", environment)
            self.assertEqual(observed_seconds, seconds)
            self.assertEqual(state, self.paths["state"])
            self.assertTrue(self.n["bounded"].call_args.kwargs["worker_user"])
        for flags in ({"component_runtime": False}, {"component_runtime": True, "full_vs_reuse": True}):
            with self.subTest(flags=flags), self.assertRaises(self.n["ProbeFailure"]):
                self.n["native_identity"](flags)

    def test_unselected_component_mode_keeps_original_probe_workloads(self):
        self.paths.pop("component_runtime")
        with self.filesystem():
            value = self.n["private_daemon"](self.paths)
        self.assertEqual(value["status"], "passed")
        self.assertNotIn("component_runtime", value)
        self.n["stopped_container"].assert_called_once()
        self.n["running_container"].assert_called_once()
        self.callback.assert_not_called()
        self.n["root_lifeline_tick"].assert_called_once_with(100 + self.n["ROOT_BODY_SECONDS"])


if __name__ == "__main__":
    unittest.main()
