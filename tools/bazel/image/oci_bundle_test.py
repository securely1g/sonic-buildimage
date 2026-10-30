#!/usr/bin/env python3
"""Exercise the OCI interchange contract and real Skopeo conversions."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_bundle


OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
SKOPEO = os.environ.get("SKOPEO", shutil.which("skopeo"))


def write_tar(path, entries, compressed=False):
    with tarfile.open(path, "w:gz" if compressed else "w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))


def fixture(root, architecture="amd64", tags=None):
    """Create one tiny image in both standard input formats."""
    root.mkdir(parents=True)
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        payload = b"SONiC archive import regression\n"
        entry = tarfile.TarInfo("etc/sonic-archive-probe")
        entry.size, entry.uid, entry.gid, entry.mode = len(payload), 100, 101, 0o640
        archive.addfile(entry, io.BytesIO(payload))
    raw_layer = layer.getvalue()
    compressed = gzip.compress(raw_layer, mtime=0)
    config = {"architecture": architecture, "os": "linux",
              "created": "2026-01-01T00:00:00Z",
              "config": {"User": "100:101", "Env": ["SONIC_PROBE=preserved"],
                         "Cmd": ["/bin/true"]},
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(raw_layer).hexdigest()]},
              "history": [{"created": "2026-01-01T00:00:00Z", "created_by": "OCI contract fixture"}]}
    config_bytes = oci_bundle.canonical(config)
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    layer_hash = hashlib.sha256(compressed).hexdigest()
    manifest = {"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": "sha256:" + config_hash, "size": len(config_bytes)},
                "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                            "digest": "sha256:" + layer_hash, "size": len(compressed)}]}
    manifest_bytes = oci_bundle.canonical(manifest)
    manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    index = {"schemaVersion": 2, "manifests": [{"mediaType": OCI_MANIFEST,
             "digest": manifest_digest, "size": len(manifest_bytes),
             "annotations": {"org.opencontainers.image.ref.name": "docker.io/library/docker-probe:latest"}}]}
    entries = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
               "index.json": oci_bundle.canonical(index),
               "blobs/sha256/" + manifest_digest[7:]: manifest_bytes,
               "blobs/sha256/" + config_hash: config_bytes,
               "blobs/sha256/" + layer_hash: compressed}
    layout = root / "layout"
    for name, data in entries.items():
        path = layout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    archive = root / "image.oci.tar"
    write_tar(archive, entries)
    docker_archive = root / "image.docker.tar.gz"
    docker_manifest = [{"Config": config_hash + ".json", "Layers": ["layer.tar"],
                        "RepoTags": tags or ["docker-probe:latest", "docker-probe:old"]}]
    write_tar(docker_archive, {"manifest.json": oci_bundle.canonical(docker_manifest),
                              config_hash + ".json": config_bytes, "layer.tar": raw_layer}, compressed=True)
    return {"oci": layout, "oci-archive": archive, "docker-archive": docker_archive,
            "config": config, "digest": manifest_digest, "entries": entries,
            "layer": "blobs/sha256/" + layer_hash}


class OCIContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = fixture(self.root / "fixture")

    def test_docker_archive_retains_all_tags(self):
        self.assertEqual(oci_bundle.docker_tags(self.image["docker-archive"]),
                         ["docker.io/library/docker-probe:latest", "docker.io/library/docker-probe:old"])

    def test_references_are_normalized_for_docker_lookup(self):
        for value, expected in [("probe:latest", "docker.io/library/probe:latest"),
                                ("team/probe:v1", "docker.io/team/probe:v1"),
                                ("index.docker.io/probe:v1", "docker.io/library/probe:v1"),
                                ("localhost:5000/team/probe:v1", "localhost:5000/team/probe:v1")]:
            self.assertEqual(oci_bundle.reference(value), expected)
        for value in ["probe", "probe@sha256:" + "a" * 64, "../probe:v1", "bad name:v1"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                oci_bundle.reference(value)

    def test_oci_integrity_and_platform(self):
        self.assertEqual(oci_bundle.inspect_oci(self.image["oci-archive"], "amd64"),
                         (self.image["digest"], self.image["config"]))
        with self.assertRaisesRegex(ValueError, "target platform"):
            oci_bundle.inspect_oci(self.image["oci-archive"], "arm64")
        entries = dict(self.image["entries"])
        entries.pop(self.image["layer"])
        damaged = self.root / "missing-layer.tar"
        write_tar(damaged, entries)
        with self.assertRaisesRegex(ValueError, "layer is missing"):
            oci_bundle.inspect_oci(damaged, "amd64")
        entries = dict(self.image["entries"])
        manifest_path = "blobs/sha256/" + self.image["digest"][7:]
        entries[manifest_path] = entries[manifest_path].replace(b'"schemaVersion":2', b'"schemaVersion":3')
        write_tar(damaged, entries)
        with self.assertRaisesRegex(ValueError, "integrity mismatch"):
            oci_bundle.inspect_oci(damaged, "amd64")

@unittest.skipUnless(SKOPEO, "real Skopeo is required for conversion integration tests")
class SkopeoConversionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_all_input_formats_preserve_platform_config_layers_and_tags(self):
        # ARM64 here validates archive metadata; this does not execute ARM64 code.
        for architecture in ("amd64", "arm64"):
            source = fixture(self.root / architecture, architecture,
                             tags=["docker-probe:latest", "docker-probe:old", "registry.example/team/probe:v1"])
            for source_format in ("oci", "oci-archive", "docker-archive"):
                with self.subTest(architecture=architecture, source_format=source_format):
                    part = self.root / (architecture + "-" + source_format)
                    record = oci_bundle.convert(source[source_format], source_format, "docker-probe",
                                                architecture, "sonic-version", part, SKOPEO)
                    digest, config = oci_bundle.inspect_oci(part / "docker-probe.oci.tar", architecture)
                    self.assertEqual(digest, record["digest"])
                    self.assertEqual(config, source["config"])
                    with tarfile.open(part / "docker-probe.oci.tar", "r:") as archive:
                        manifest = json.load(archive.extractfile("./blobs/sha256/" + digest[7:]))
                    self.assertTrue(all(layer["mediaType"] == "application/vnd.oci.image.layer.v1.tar+zstd"
                                        for layer in manifest["layers"]))
                    self.assertIn("docker.io/library/docker-probe:sonic-version", record["tags"])
                    if source_format == "docker-archive":
                        self.assertIn("docker.io/library/docker-probe:old", record["tags"])
                        self.assertIn("registry.example/team/probe:v1", record["tags"])
                    self.assertEqual(record["sha256"], oci_bundle.sha256(part / "docker-probe.oci.tar"))

    def test_wrong_architecture_never_produces_a_success_manifest(self):
        source = fixture(self.root / "arm64-input", "arm64")
        output = self.root / "wrong-platform"
        with self.assertRaises(ValueError):
            oci_bundle.convert(source["oci"], "oci", "docker-probe", "amd64", "v1", output, SKOPEO)
        self.assertFalse((output / "manifest.json").exists())

    def test_bazel_precreated_empty_tree_output_is_accepted(self):
        source = fixture(self.root / "source")
        output = self.root / "bazel-tree-output"
        output.mkdir()
        record = oci_bundle.convert(source["oci"], "oci", "docker-probe", "amd64", "v1", output, SKOPEO)
        self.assertEqual(record["sha256"], oci_bundle.sha256(output / "docker-probe.oci.tar"))
        self.assertTrue((output / "manifest.json").is_file())

    def test_nonempty_output_is_rejected_without_overwriting_it(self):
        source = fixture(self.root / "source")
        output = self.root / "existing-output"
        output.mkdir()
        sentinel = output / "existing"
        sentinel.write_bytes(b"preserve previous output")
        with self.assertRaises(ValueError):
            oci_bundle.convert(source["oci"], "oci", "docker-probe", "amd64", "v1", output, SKOPEO)
        self.assertEqual(sentinel.read_bytes(), b"preserve previous output")
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_conversion_is_reproducible_across_output_directories_and_time(self):
        source = fixture(self.root / "source")
        first, second = self.root / "first", self.root / "second"
        oci_bundle.convert(source["oci"], "oci", "docker-probe", "amd64", "v1", first, SKOPEO)
        # TAR headers must not inherit Skopeo's newly created temporary files.
        time.sleep(1.1)
        oci_bundle.convert(source["oci"], "oci", "docker-probe", "amd64", "v1", second, SKOPEO)
        for name in ("docker-probe.oci.tar", "manifest.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

    def test_skopeo_rejects_damaged_source_blob(self):
        source = fixture(self.root / "source")
        layer = source["oci"] / source["layer"]
        data = bytearray(layer.read_bytes())
        data[-1] ^= 1
        layer.write_bytes(data)
        output = self.root / "damaged-source"
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            oci_bundle.convert(source["oci"], "oci", "docker-probe", "amd64", "v1", output, SKOPEO)
        self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
