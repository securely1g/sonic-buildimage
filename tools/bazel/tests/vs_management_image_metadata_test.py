"""Pure fixtures for the bounded anonymous upstream-client boundary."""
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import vs_management_image_metadata as subject


class FakeProcess:
    def __init__(self, stdout=b"{\"mediaType\":\"fixture\"}", stderr=b""):
        self.pid = 991999
        self.returncode = None
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            reader, writer = os.pipe()
            os.write(writer, data)
            os.close(writer)
            setattr(self, name, os.fdopen(reader, "rb"))

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


class ClientBoundaryTests(unittest.TestCase):
    def test_fresh_config_has_no_credentials_or_proxy_forwarding(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            argv, env = subject.client_context(directory, {
                "HOME": "/fixture-home", "GITHUB_TOKEN": "secret", "DOCKER_AUTH_CONFIG": "secret",
                "HTTPS_PROXY": "https://credential@proxy.invalid", "SSH_AUTH_SOCK": "private",
                "DOCKER_HOST": "tcp://elsewhere", "DOCKER_CONFIG": "/existing"})
            config = json.loads((directory / "docker-config/config.json").read_text())
            self.assertEqual(config["auths"], {"sonicdev-microsoft.azurecr.io": {}})
            self.assertFalse({"credsStore", "credHelpers"} & config.keys())
            self.assertFalse({"GITHUB_TOKEN", "DOCKER_AUTH_CONFIG", "HTTPS_PROXY", "SSH_AUTH_SOCK"} & env.keys())
            self.assertEqual(argv[-4:], ["imagetools", "inspect", "--raw", subject.IMAGE])
            self.assertEqual(env["DOCKER_HOST"], "unix://" + str(directory / "no-daemon.sock"))

    def test_existing_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            config = directory / "docker-config"
            config.mkdir()
            (config / "config.json").write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                subject.client_context(directory, {})
            self.assertEqual((config / "config.json").read_bytes(), b"original")

    def test_tool_symlink_and_size_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "tool"
            target.write_bytes(b"fixture executable")
            target.chmod(0o755)
            link = Path(root) / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                subject.file_identity(link, require_root=False)
            with mock.patch.object(subject, "TOOL_CAP", 3), self.assertRaises(ValueError):
                subject.file_identity(target, require_root=False)

    def test_tool_name_replacement_during_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "tool"
            target.write_bytes(b"original")
            target.chmod(0o755)
            original_read = os.read
            replaced = False
            def read(fd, size):
                nonlocal replaced
                value = original_read(fd, size)
                if not replaced:
                    replaced = True
                    target.unlink()
                    target.write_bytes(b"replaced")
                    target.chmod(0o755)
                return value
            with mock.patch.object(subject.os, "read", side_effect=read), self.assertRaises(ValueError):
                subject.file_identity(target, require_root=False)

    def test_raw_success_is_retained_without_parsing(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeProcess(stdout=b"opaque upstream bytes")
            with mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject, "gone", return_value=True), mock.patch.object(subject.os, "killpg") as kill:
                result = subject.observe_client(Path(root), ["fixture-only"], {})
            self.assertEqual(result["status"], "CAPTURED")
            self.assertEqual((Path(root) / "stdout.data").read_bytes(), b"opaque upstream bytes")
            self.assertTrue(result["leader_reaped"] and result["group_closed"])
            kill.assert_not_called()

    def test_output_cap_closes_owned_group_and_fails(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeProcess(stdout=b"beyond cap")
            with mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject, "gone", side_effect=[False, True, True, True]), mock.patch.object(subject.os, "killpg") as kill, mock.patch.object(subject, "STDOUT_CAP", 3):
                result = subject.observe_client(Path(root), ["fixture-only"], {})
            self.assertEqual(result["reason"], "CLIENT_OUTPUT_CAP")
            self.assertEqual(result["status"], "FAILED_STOP_NO_RETRY")
            self.assertEqual(result["stdout_bytes"], 3)
            kill.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_zero_exit_with_descendants_still_fails(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeProcess()
            with mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject, "gone", side_effect=[False, True, True, True]), mock.patch.object(subject.os, "killpg"):
                result = subject.observe_client(Path(root), ["fixture-only"], {})
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["reason"], "DESCENDANTS_AFTER_CLIENT_EXIT")
            self.assertEqual(result["status"], "FAILED_STOP_NO_RETRY")

    def test_deadline_closes_owned_group_and_fails(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeProcess()
            with mock.patch.object(subject.subprocess, "Popen", return_value=process), mock.patch.object(subject, "gone", side_effect=[False, True, True, True]), mock.patch.object(subject.os, "killpg") as kill, mock.patch.object(subject.time, "monotonic", side_effect=[0.0] + [31.0] * 10):
                result = subject.observe_client(Path(root), ["fixture-only"], {})
            self.assertEqual(result["reason"], "CLIENT_DEADLINE")
            self.assertTrue(result["leader_reaped"] and result["group_closed"])
            kill.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_late_spawn_interruption_retains_handle_and_closes_group(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeProcess()
            def spawn(*args, **kwargs):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                return process
            with mock.patch.object(subject.subprocess, "Popen", side_effect=spawn), mock.patch.object(subject, "gone", side_effect=[False, True, True, True]), mock.patch.object(subject.os, "killpg") as kill, self.assertRaises(InterruptedError):
                subject.observe_client(Path(root), ["fixture-only"], {})
            result = json.loads((Path(root) / "result.json").read_text())
            self.assertEqual(result["deferred_spawn_signal"], signal.SIGTERM)
            self.assertEqual(result["status"], "FAILED_STOP_NO_RETRY")
            self.assertTrue(process.stdout.closed and process.stderr.closed)
            kill.assert_called_once_with(process.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
