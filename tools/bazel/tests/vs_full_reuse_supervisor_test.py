#!/usr/bin/env python3
"""Pure namespace/argv fixtures; never invoke unshare, ip, Docker, or assembly."""
import ast
from contextlib import ExitStack
import io
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tools/bazel/ci/vs_runtime_check.py"


def namespace():
    selected = {"require", "daemon_arguments", "full_vs_endpoint_absent",
                "full_vs_daemon_arguments", "hold_network_namespace",
                "network_interface_names", "full_vs_network_ready"}
    constants = {"PATH", "FULL_VS_SOCKET", "FULL_VS_NETWORK_FLAGS", "FULL_VS_NETWORK_BOOTSTRAP"}
    body = []
    for node in ast.parse(SOURCE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in selected:
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "ProbeFailure":
            body.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            body.append(node)
    values = {name: globals()[name] for name in ("os", "Path", "re", "shutil", "stat", "sys")}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE), "exec"), values)
    return values


def network_data(*names):
    header = "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
    return (header + "".join(name + ": " + " ".join(["0"] * 16) + "\n" for name in names)).encode()


class FullVSNetworkTest(unittest.TestCase):
    def setUp(self):
        self.n = namespace()
        self.paths = {"state": Path("/actions/_work/_temp/vs-runtime-fixed"),
                      "exec": Path("/run/sonic-vs-runtime-fixed"),
                      "docker_data": Path("/actions/_work/_temp/vs-runtime-fixed/source-image-docker-12-1")}
        self.parent = {"device": 4, "inode": 101}
        self.daemon = {"device": 4, "inode": 102}
        self.child = SimpleNamespace(pid=78, poll=lambda: None)

    def tools(self, mode=stat.S_IFREG | 0o755):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(shutil, "which", side_effect=["/usr/bin/unshare", "/usr/sbin/ip"]))
        stack.enter_context(mock.patch.object(Path, "resolve", autospec=True, side_effect=lambda p, **kw: p))
        stack.enter_context(mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=mode)))
        stack.enter_context(mock.patch.object(os, "access", return_value=True))
        return stack

    def test_fixed_network_entrypoint_preserves_owned_pid_and_two_sockets(self):
        with self.tools():
            argv = self.n["full_vs_daemon_arguments"]("/usr/bin/dockerd", self.paths)
        self.assertEqual(argv[:8], ["/usr/bin/unshare", "--net", "--", sys.executable, "-I", "-S", "-B", "-c"])
        self.assertEqual(argv[8], self.n["FULL_VS_NETWORK_BOOTSTRAP"])
        self.assertEqual(argv[9:11], ["/usr/sbin/ip", "/usr/bin/dockerd"])
        self.assertNotIn("--fork", argv)
        self.assertNotIn("--mount", argv)
        self.assertEqual(argv.count("--host"), 2)
        self.assertIn("unix:///run/sonic-vs-runtime-fixed/docker.sock", argv)
        self.assertIn("unix:///var/run/docker.sock", argv)
        self.assertEqual(argv[-4:], ["--bip", "192.168.240.1/24", "--default-address-pool", "base=192.168.248.0/21,size=24"])

    def test_missing_namespace_tool_is_not_replaced_with_host_daemon(self):
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Missing fixed network namespace tool"):
                self.n["full_vs_daemon_arguments"]("/usr/bin/dockerd", self.paths)

    def test_writable_namespace_tool_is_refused(self):
        with self.tools(stat.S_IFREG | 0o775):
            with self.assertRaisesRegex(ValueError, "tool ownership differs"):
                self.n["full_vs_daemon_arguments"]("/usr/bin/dockerd", self.paths)

    def test_changed_network_none_baseline_is_refused(self):
        self.n["daemon_arguments"] = lambda executable, paths: [executable, "--changed"]
        with mock.patch.object(shutil, "which") as lookup:
            with self.assertRaisesRegex(ValueError, "network baseline differs"):
                self.n["full_vs_daemon_arguments"]("/usr/bin/dockerd", self.paths)
            lookup.assert_not_called()

    def test_existing_or_dangling_default_endpoint_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "docker.sock"
            self.n["FULL_VS_SOCKET"] = path
            for kind in ("file", "dangling"):
                with self.subTest(kind=kind):
                    if kind == "file":
                        path.write_bytes(b"preserve")
                    else:
                        path.symlink_to("missing-target")
                    with mock.patch.object(Path, "resolve", return_value=Path("/run")):
                        with self.assertRaisesRegex(ValueError, "Preserve existing default Docker socket or link"):
                            self.n["full_vs_endpoint_absent"]()
                    self.assertTrue(os.path.lexists(path))
                    path.unlink()

    def test_absent_endpoint_check_does_not_create_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "docker.sock"
            self.n["FULL_VS_SOCKET"] = path
            with mock.patch.object(Path, "resolve", return_value=Path("/run")):
                self.n["full_vs_endpoint_absent"]()
            self.assertFalse(os.path.lexists(path))

    def test_namespace_descriptor_closes_after_failed_fstat(self):
        with mock.patch.object(os, "open", return_value=57), mock.patch.object(os, "fstat", side_effect=OSError), mock.patch.object(os, "close") as close:
            with self.assertRaises(OSError):
                with ExitStack() as handles:
                    self.n["hold_network_namespace"](handles, 78)
            close.assert_called_once_with(57)

    def test_invalid_namespace_pid_refuses_before_open(self):
        with mock.patch.object(os, "open") as open_fd:
            for pid in (0, 1, -1, True, "78", "../1"):
                with self.subTest(pid=pid), self.assertRaises(ValueError), ExitStack() as handles:
                    self.n["hold_network_namespace"](handles, pid)
            open_fd.assert_not_called()

    def test_interface_metadata_names_and_duplicate_refusal(self):
        self.assertEqual(self.n["network_interface_names"](network_data("lo", "docker0")), {"lo", "docker0"})
        for data in (network_data("lo", "lo"), network_data("lo").replace(b"0 0", b"x 0", 1), b"x" * 65537):
            with self.subTest(data_bytes=len(data)), self.assertRaises(ValueError):
                self.n["network_interface_names"](data)

    def ready(self, namespaces, interfaces=("lo", "docker0"), mode=stat.S_IFSOCK | 0o660):
        self.n["hold_network_namespace"] = mock.Mock(side_effect=namespaces)
        with mock.patch.object(Path, "open", return_value=io.BytesIO(network_data(*interfaces))), mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=0, st_dev=1, st_ino=44)), ExitStack() as handles:
            return self.n["full_vs_network_ready"](self.child, self.paths, handles, self.parent)

    def test_same_network_namespace_is_refused(self):
        with self.assertRaisesRegex(ValueError, "distinct network namespace"):
            self.ready([self.parent, self.parent])

    def test_changed_parent_network_namespace_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Supervisor left the worker"):
            self.ready([self.daemon])

    def test_external_interface_or_missing_bridge_is_refused(self):
        for interfaces in (("lo", "docker0", "eth0"), ("lo",)):
            with self.subTest(interfaces=interfaces), self.assertRaisesRegex(ValueError, "unexpected network interface"):
                self.ready([self.parent, self.daemon], interfaces)

    def test_socket_symlink_is_not_an_owned_endpoint(self):
        with self.assertRaisesRegex(ValueError, "root-owned socket"):
            self.ready([self.parent, self.daemon], mode=stat.S_IFLNK | 0o777)

    def test_distinct_namespace_and_local_bridge_proof_is_numeric(self):
        value = self.ready([self.parent, self.daemon])
        self.assertEqual(value["status"], "passed")
        self.assertEqual(value["parent"], self.parent)
        self.assertEqual(value["daemon"], self.daemon)
        self.assertFalse(value["external_interface_present"])
        self.assertEqual(value["default_socket"], {"device": 1, "inode": 44})

    def test_exited_owned_child_refuses_before_namespace_read(self):
        self.child = SimpleNamespace(pid=78, poll=lambda: 0)
        with self.assertRaisesRegex(ValueError, "exited before namespace proof"):
            self.ready([])
        self.n["hold_network_namespace"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
