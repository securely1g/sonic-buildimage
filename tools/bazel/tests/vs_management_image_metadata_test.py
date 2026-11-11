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
            self.assertEqual(argv[-5:], ["imagetools", "inspect", "--format", subject.FORMAT, subject.IMAGE])
            self.assertNotIn(":latest", subject.IMAGE)
            self.assertNotIn("--raw", argv)
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


class ConfigurationProjectionTests(unittest.TestCase):
    def fixture(self):
        return {"manifest": {"digest": subject.MANIFEST_DIGEST, "size": subject.MANIFEST_SIZE,
                             "mediaType": "application/vnd.docker.distribution.manifest.v2+json"},
                "architecture": "amd64", "os": "linux", "created": None,
                "labels": {"com.azure.sonic.manifest": "opaque source record"},
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64] * 28},
                "history": [{"created_by": "COPY fixture /debs/"}] * 28 + [{"empty_layer": True}]}

    def test_configuration_counts_history_without_claiming_package_acceptance(self):
        result = subject.validate_projection(json.dumps(self.fixture()).encode())
        self.assertEqual(result["history_entries"], 29)
        self.assertEqual(result["nonempty_history_entries"], 28)
        self.assertEqual(result["rootfs_diff_ids"], 28)
        self.assertFalse(result["original_config_bytes_or_digest_independently_verified"])
        self.assertFalse(result["device_data_layer_identified"])
        self.assertFalse(result["package_or_source_compatibility_established"])

    def test_changed_descriptor_or_platform_is_rejected(self):
        for field, value in (("digest", "sha256:" + "b" * 64), ("size", 6180), ("mediaType", "index")):
            with self.subTest(field=field):
                data = self.fixture()
                data["manifest"][field] = value
                with self.assertRaises(ValueError):
                    subject.validate_projection(json.dumps(data).encode())
        for field, value in (("architecture", "arm64"), ("os", "windows")):
            with self.subTest(field=field):
                data = self.fixture()
                data[field] = value
                with self.assertRaises(ValueError):
                    subject.validate_projection(json.dumps(data).encode())

    def test_history_and_diff_id_mismatches_are_rejected(self):
        for change in (lambda data: data["history"].pop(0),
                       lambda data: data["history"][-1].update(empty_layer=1),
                       lambda data: data["rootfs"]["diff_ids"].pop(),
                       lambda data: data["rootfs"]["diff_ids"].__setitem__(0, "not-a-digest")):
            data = self.fixture()
            change(data)
            with self.assertRaises(ValueError):
                subject.validate_projection(json.dumps(data).encode())

    def test_ambiguous_nonfinite_and_oversized_projection_is_rejected(self):
        raw = json.dumps(self.fixture()).encode()
        for bad in (raw.replace(b'"architecture": "amd64"', b'"architecture": "amd64", "architecture": "amd64"'),
                    raw.replace(b'"created": null', b'"created": NaN'), b' ' * (subject.STDOUT_CAP + 1)):
            with self.assertRaises(ValueError):
                subject.validate_projection(bad)

    def test_version_difference_is_provenance_without_a_network_gate(self):
        data = subject.version_metadata(b"github.com/docker/buildx v0.29.0 abcdef1234567\n")
        self.assertEqual(data["reported_version"], "v0.29.0")
        self.assertEqual(data["reported_revision"], "abcdef1234567")
        self.assertFalse(data["version_difference_blocks_metadata"])
        self.assertFalse(subject.version_metadata(b"other upstream format")["version_difference_blocks_metadata"])

    def test_version_then_single_metadata_invocation_and_failed_version_stop(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            calls = []
            def observed(path, argv, env):
                calls.append(argv)
                raw = b"github.com/docker/buildx v0.29.0 abcdef1234567\n" if path.name == "version" else json.dumps(self.fixture()).encode()
                (path / "stdout.data").write_bytes(raw)
                return {"status": "CAPTURED"}
            argv = ["docker", "--config", "empty", "buildx", "imagetools", "inspect", "--format", subject.FORMAT, subject.IMAGE]
            with mock.patch.object(subject, "observe_client", side_effect=observed):
                result, version_result, version, metadata = subject.collect_metadata(directory, argv, {})
            self.assertEqual(calls, [argv[:4] + ["version"], argv])
            self.assertEqual(metadata["nonempty_history_entries"], 28)
            self.assertEqual(version["reported_version"], "v0.29.0")
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(subject, "observe_client", return_value={"status": "FAILED_STOP_NO_RETRY"}) as observe:
                with self.assertRaises(ValueError):
                    subject.collect_metadata(Path(root), argv, {})
            observe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
