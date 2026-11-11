"""Original-blob integrity and upstream-client boundaries; no network or binaries."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.ci import vs_management_image_blobs as subject


def config_value():
    history = [{"created_by": "fixture"} for _ in range(28)]
    history[13]["created_by"] = subject.COPY_COMMAND
    ids = ["sha256:" + "0" * 64 for _ in range(28)]
    ids[13] = subject.DIFF_ID
    return {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": ids},
            "history": [{"empty_layer": True}] + history}


def descriptor(raw):
    return {"size": len(raw), "digest": "sha256:" + subject.metadata.sha(raw), "mediaType": "fixture"}


class ManagementBlobTest(unittest.TestCase):
    def test_original_blob_size_and_digest_are_both_required(self):
        expected = descriptor(b"original")
        subject.verify_blob(b"original", expected)
        for raw in (b"changed!", b"original\n", b"origina"):
            with self.assertRaisesRegex(ValueError, "size or digest"):
                subject.verify_blob(raw, expected)

    def test_config_proves_platform_and_copy_layer_position(self):
        value = config_value()
        raw = json.dumps(value).encode()
        with patch.object(subject, "CONFIG", descriptor(raw)):
            subject.verify_config(raw)
        for change in ("architecture", "copy", "diff_id", "count"):
            value = config_value()
            if change == "architecture": value["architecture"] = "arm64"
            elif change == "copy": value["history"][14]["created_by"] = "different"
            elif change == "diff_id": value["rootfs"]["diff_ids"][13] = "sha256:" + "1" * 64
            else: value["history"].pop()
            raw = json.dumps(value).encode()
            with patch.object(subject, "CONFIG", descriptor(raw)), self.assertRaises(ValueError):
                subject.verify_config(raw)

    def test_anonymous_context_does_not_forward_existing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,
                {"GH_TOKEN": "fixture-secret", "BAZEL_REMOTE_AUTH": "fixture-secret", "HTTP_PROXY": "fixture-secret"}):
            root = Path(tmp)
            env = subject.anonymous_environment(root)
            self.assertNotIn("fixture-secret", json.dumps(env))
            self.assertEqual(set(env), {"HOME", "DOCKER_CONFIG", "PATH", "LANG", "LC_ALL", "TERM"})
            self.assertEqual(json.loads((Path(env["DOCKER_CONFIG"]) / "config.json").read_text()),
                             {"auths": {"sonicdev-microsoft.azurecr.io": {}}})
            with self.assertRaises(FileExistsError): subject.anonymous_environment(root)

    def test_only_two_exact_raw_blob_reads_and_original_bytes_are_retained(self):
        config, layer = json.dumps(config_value()).encode(), b"original compressed layer fixture"
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = {"sha256": subject.REGCTL_SHA256, "path": str(root / "regctl")}
            def observe(directory, argv, env, *, stdout_cap, seconds):
                calls.append((argv, stdout_cap, seconds))
                raw = config if directory.name == "config" else layer
                self.assertEqual(stdout_cap, len(raw))
                self.assertEqual(argv[1:6], ["blob", "get", "--format", "raw-body", subject.REPOSITORY])
                self.assertEqual(argv[6], descriptor(raw)["digest"])
                (directory / "stdout.data").write_bytes(raw)
                return {"status": "CAPTURED"}
            with patch.object(subject, "CONFIG", descriptor(config)), patch.object(subject, "LAYER", descriptor(layer)), \
                 patch.object(subject.metadata, "file_identity", return_value=identity), \
                 patch.object(subject.metadata, "observe_client", side_effect=observe):
                receipt = subject.collect(root, root / "regctl")
            self.assertEqual(len(calls), 2)
            self.assertEqual([c[2] for c in calls], [30, 120])
            self.assertEqual((root / "config/stdout.data").read_bytes(), config)
            self.assertEqual((root / "layer/stdout.data").read_bytes(), layer)
            self.assertEqual(receipt["status"], "ORIGINAL_CONFIG_AND_COPY_LAYER_VERIFIED")
            self.assertFalse(receipt["package_presence_verified"])
            self.assertFalse(receipt["source_compatibility_verified"])
            self.assertFalse(receipt["package_generation"])
            self.assertFalse(receipt["uncompressed_diff_id_verified"])

    def test_failed_or_changed_config_prevents_layer_request(self):
        config = json.dumps(config_value()).encode()
        for fail in (True, False):
            calls = []
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                def observe(directory, *args, **kwargs):
                    calls.append(directory.name)
                    (directory / "stdout.data").write_bytes(config + b"changed")
                    return {"status": "FAILED_STOP_NO_RETRY" if fail else "CAPTURED"}
                with patch.object(subject, "CONFIG", descriptor(config)), \
                     patch.object(subject.metadata, "file_identity", return_value={"sha256": subject.REGCTL_SHA256}), \
                     patch.object(subject.metadata, "observe_client", side_effect=observe), self.assertRaises(ValueError):
                    subject.collect(root, root / "regctl")
                self.assertEqual(calls, ["config"])
                self.assertEqual(json.loads((root / "summary.json").read_text())["status"], "FAILED_NOT_RECOVERED")

    def test_different_tool_pin_stops_before_registry_client(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(subject.metadata, "file_identity", return_value={"sha256": "0" * 64}), \
             patch.object(subject.metadata, "observe_client") as invoke, self.assertRaisesRegex(ValueError, "rules_oci pin"):
            subject.collect(Path(tmp), Path(tmp) / "regctl")
        invoke.assert_not_called()

    def test_upstream_client_rejects_unbounded_capture_before_spawn(self):
        for cap, seconds in ((0, 30), (33 * 1024 * 1024, 30), (100, 121), (True, 30)):
            with patch.object(subject.metadata.subprocess, "Popen") as invoke, self.assertRaises(ValueError):
                subject.metadata.observe_client(Path("/unused"), ["unused"], {}, stdout_cap=cap, seconds=seconds)
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
