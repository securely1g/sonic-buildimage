"""Exercise VS consumer integrity using in-memory reference OCI descriptors and real archive bytes."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import vs_container_validation as validate


def digest(content):
    return hashlib.sha256(content).hexdigest()


class VSContainerValidationTest(unittest.TestCase):
    image = "docker-example"
    revision = "1" * 40
    version = "HEAD.0-1111111"

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.directory = Path(root.name)
        self.bundle = {
            "image": self.image, "revision": self.revision, "architecture": "amd64",
            "debug_enabled": False, "supports_debug": True, "build_version": self.version,
            "archives": {}, "fixtures": {},
        }
        self.add_image("runtime", [b"base", b"runtime"])

    def add_image(self, variant, layers):
        name = self.image + ("-dbg" if variant == "debug" else "")
        config = {
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + digest(blob) for blob in layers]},
            "config": {"Labels": {"Tag": self.version}}, "os": "linux", "architecture": "amd64",
        }
        raw = json.dumps(config).encode()
        blobs = {f"blobs/sha256/{digest(blob)}": blob for blob in layers}
        manifest = {"Layers": list(blobs), "RepoTags": [name + ":latest"], "Config": digest(raw) + ".json"}
        blobs[manifest["Config"]] = raw
        blobs["manifest.json"] = json.dumps([manifest]).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for path, value in blobs.items():
                entry = tarfile.TarInfo(path)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
        exported = gzip.compress(output.getvalue(), mtime=0)
        path = self.directory / (name + ".gz")
        path.write_bytes(exported)
        descriptors = [{"digest": "sha256:" + digest(blob), "size": len(blob)} for blob in layers]
        spec = {
            "file": path.name, "sha256": digest(exported), "config_digest": "sha256:" + digest(raw),
            "layers": descriptors, "rootfs": config["rootfs"],
        }
        self.bundle["archives"][variant] = spec
        oci = {"config": config, "config_digest": spec["config_digest"], "layers": descriptors, "files": {}}
        metadata = self.directory / (variant + ".oci.json.gz")
        metadata.write_bytes(gzip.compress(json.dumps(oci).encode()))
        self.bundle["fixtures"][metadata.name] = digest(metadata.read_bytes())

    def validate(self, debug=False, revision=None):
        (self.directory / "bundle.json").write_text(json.dumps(self.bundle))
        return validate.validate_artifacts(self.directory, self.image, revision or self.revision, debug, self.version)

    def test_runtime_only_needs_no_debug_inputs(self):
        self.assertEqual(set(self.validate()["archives"]), {"runtime"})

    def test_enabled_debug_extends_the_published_runtime(self):
        self.bundle["debug_enabled"] = True
        self.add_image("debug", [b"base", b"runtime", b"debug tools"])
        self.assertEqual(set(self.validate(True)["archives"]), {"runtime", "debug"})
        self.add_image("debug", [b"base", b"other runtime", b"debug tools"])
        with self.assertRaisesRegex(ValueError, "does not extend"):
            self.validate(True)

    def test_selected_debug_cannot_be_missing_or_silently_downgraded(self):
        with self.assertRaisesRegex(ValueError, "debug setting"):
            self.validate(True)
        self.bundle["debug_enabled"] = True
        with self.assertRaisesRegex(ValueError, "selected variants"):
            self.validate(True)
        self.bundle["supports_debug"] = False  # An explicitly runtime-only parent.
        self.assertEqual(set(self.validate(True)["archives"]), {"runtime"})

    def test_extra_debug_rejected_when_disabled(self):
        self.add_image("debug", [b"base", b"runtime", b"debug tools"])
        with self.assertRaisesRegex(ValueError, "selected variants"):
            self.validate()

    def test_wrong_revision_or_release_fails_before_probing(self):
        with self.assertRaisesRegex(ValueError, "checkout"):
            self.validate(revision="2"*40)
        self.bundle["build_version"] = "some-other-build"
        with self.assertRaisesRegex(ValueError, "wrong VS release"):
            self.validate()

    def test_rejects_archive_with_wrong_transport_hash(self):
        self.bundle["archives"]["runtime"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "VS archive checksum"):
            self.validate()

    def test_checks_layers_against_the_producer_layout(self):
        spec = self.bundle["archives"]["runtime"]
        oci = validate.read_oci(self.directory, "runtime")
        spec["layers"].reverse()
        oci["layers"] = spec["layers"]
        encoded = gzip.compress(json.dumps(oci).encode())
        (self.directory / "runtime.oci.json.gz").write_bytes(encoded)
        self.bundle["fixtures"]["runtime.oci.json.gz"] = digest(encoded)
        with self.assertRaisesRegex(ValueError, "Docker archive layer differs"):
            self.validate()

    def test_rejects_swapped_fixture_and_references_outside_artifact(self):
        fixture = self.directory / "test-redis.tar"
        fixture.write_bytes(b"new version")
        self.bundle["fixtures"][fixture.name] = digest(b"original version")
        with self.assertRaisesRegex(ValueError, "fixture checksum"):
            self.validate()
        del self.bundle["fixtures"][fixture.name]
        self.bundle["fixtures"]["../other-test-redis.tar"] = digest(b"")
        with self.assertRaisesRegex(ValueError, "Invalid VS artifact filename"):
            self.validate()

    def test_releases_private_docker_tag_on_probe_failure(self):
        spec = self.bundle["archives"]["runtime"]
        config = validate.read_oci(self.directory, "runtime")["config"]
        inspected = {"Id": spec["config_digest"], "Config": config["config"], "Os": "linux",
                     "Architecture": "amd64", "RootFS": {"Type": "layers", "Layers": config["rootfs"]["diff_ids"]}}
        with patch.object(validate.validate_runtime, "load_archive", return_value=(inspected, config, "")), \
                patch.object(validate.subprocess, "run") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "service failed"):
                with validate.loaded_image(self.directory, spec, self.bundle) as tag:
                    self.assertTrue(tag.startswith("sonic-vs-probe:"))
                    raise RuntimeError("service failed")
            self.assertEqual(cleanup.call_args.args[0], ["docker", "image", "rm", tag])


if __name__ == "__main__":
    unittest.main()
