#!/usr/bin/env python3
"""Exercise upstream regctl import and SONiC's platform guard."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from import_fixture import add_bytes, digest, docker_archive, image_fixture, oci_archive


def snapshot(layout):
    return {str(path.relative_to(layout)): path.read_bytes() for path in sorted(layout.rglob("*")) if path.is_file()}


def read_blob(layout, descriptor):
    algorithm, value = descriptor["digest"].split(":")
    assert algorithm == "sha256", descriptor
    data = (layout / "blobs" / algorithm / value).read_bytes()
    assert hashlib.sha256(data).hexdigest() == value, "blob digest mismatch"
    assert len(data) == descriptor["size"], "blob size mismatch"
    return data


def check_layout(layout, expected_config, expected_layers):
    assert json.loads((layout / "oci-layout").read_bytes())["imageLayoutVersion"] == "1.0.0"
    index = json.loads((layout / "index.json").read_bytes())
    assert len(index["manifests"]) == 1
    manifest = json.loads(read_blob(layout, index["manifests"][0]))
    config_bytes = read_blob(layout, manifest["config"])
    assert config_bytes == expected_config, "image configuration bytes changed during import"
    actual_layers = []
    for descriptor in manifest["layers"]:
        layer = read_blob(layout, descriptor)
        if descriptor["mediaType"].endswith(("+gzip", ".gzip")):
            layer = gzip.decompress(layer)
        actual_layers.append(layer)
    assert actual_layers == expected_layers, "uncompressed layer contents or layer order changed"
    config = json.loads(config_bytes)
    assert config["rootfs"]["diff_ids"] == [digest(layer) for layer in actual_layers]
    return manifest


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name)

    def run_import(self, src, out, expected="linux/amd64"):
        args = [str(ARGS.wrapper), "--regctl", str(ARGS.regctl), "--src", str(src), "--out", str(out)]
        if expected:
            args += ["--expected-platform", expected]
        return subprocess.run(args, capture_output=True, text=True)

    def test_docker_tar_and_gzip_preserve_contents_and_are_reproducible(self):
        config, layers = image_fixture()
        first = self.workdir / "first.tar"
        second = self.workdir / "renamed.tar.gz"
        docker_archive(first, config, layers, mtime=946684800)
        docker_archive(second, config, layers, mtime=1700000000)
        self.assertNotEqual(first.stat().st_mtime, second.stat().st_mtime)
        outputs = []
        for i, archive in enumerate((first, second, first)):
            out = self.workdir / ("layout-" + str(i))
            result = self.run_import(archive, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            check_layout(out, config, layers)
            outputs.append(snapshot(out))
        self.assertEqual(outputs[0], outputs[1], "source filename, tar metadata or compression changed the OCI layout")
        self.assertEqual(outputs[0], outputs[2], "repeated import changed the OCI layout")

    def test_valid_oci_archive_preserves_contents(self):
        config, layers = image_fixture()
        archive = self.workdir / "oci.tar.gz"
        oci_archive(archive, config, layers)
        outputs = []
        for i in range(2):
            out = self.workdir / ("oci-" + str(i))
            result = self.run_import(archive, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            check_layout(out, config, layers)
            outputs.append(snapshot(out))
        self.assertEqual(*outputs)

    def test_both_formats_check_os_and_architecture(self):
        for os_name, architecture in (("linux", "amd64"), ("linux", "arm64"), ("windows", "amd64")):
            for builder in (docker_archive, oci_archive):
                with self.subTest(os=os_name, architecture=architecture, format=builder.__name__):
                    config, layers = image_fixture(architecture, os_name)
                    name = "-".join((os_name, architecture, builder.__name__))
                    archive = self.workdir / (name + ".tar")
                    builder(archive, config, layers)
                    result = self.run_import(archive, self.workdir / (name + "-out"))
                    if (os_name, architecture) == ("linux", "amd64"):
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, "accepted a base for another platform")
                        self.assertIn("platform", result.stderr.lower())
                    matched = self.run_import(archive, self.workdir / (name + "-matched"), os_name + "/" + architecture)
                    self.assertEqual(matched.returncode, 0, matched.stderr)

    def test_missing_config_platform_fails_closed(self):
        for missing in ("os", "architecture"):
            for builder in (docker_archive, oci_archive):
                with self.subTest(missing=missing, format=builder.__name__):
                    config, layers = image_fixture()
                    parsed = json.loads(config)
                    del parsed[missing]
                    config = json.dumps(parsed).encode()
                    name = missing + "-" + builder.__name__
                    archive = self.workdir / (name + ".tar")
                    options = {"descriptor_platform": {"os": "linux", "architecture": "amd64"}} if builder is oci_archive else {}
                    builder(archive, config, layers, **options)
                    result = self.run_import(archive, self.workdir / (name + "-out"))
                    self.assertNotEqual(result.returncode, 0, "accepted a config with missing " + missing)
                    self.assertIn("platform", result.stderr.lower())

    def test_oci_descriptor_cannot_hide_different_config_platform(self):
        for config_arch, descriptor_arch in (("arm64", "amd64"), ("amd64", "arm64")):
            with self.subTest(config=config_arch, descriptor=descriptor_arch):
                config, layers = image_fixture(config_arch)
                archive = self.workdir / (config_arch + ".tar")
                oci_archive(archive, config, layers, descriptor_platform={"os": "linux", "architecture": descriptor_arch})
                result = self.run_import(archive, self.workdir / (config_arch + "-out"))
                self.assertNotEqual(result.returncode, 0, "accepted disagreeing descriptor and config platforms")
                self.assertIn("platform", result.stderr.lower())

    def test_malformed_archive_fails(self):
        archive = self.workdir / "malformed.tar"
        with tarfile.open(archive, "w") as contents:
            add_bytes(contents, "manifest.json", b"invalid json")
        result = self.run_import(archive, self.workdir / "out")
        self.assertNotEqual(result.returncode, 0)

    def test_unrelated_traversal_members_cannot_write_outside_layout(self):
        # Upstream may ignore unknown members; the required boundary is that
        # none can create a file outside the declared output directory.
        config, layers = image_fixture()
        for builder in (docker_archive, oci_archive):
            with self.subTest(format=builder.__name__):
                archive = self.workdir / (builder.__name__ + ".tar")
                outside = self.workdir / (builder.__name__ + "-escaped")
                builder(archive, config, layers, extras=(("../" + outside.name, b"escape"), (str(outside), b"escape")))
                out = self.workdir / (builder.__name__ + "-out")
                result = self.run_import(archive, out)
                self.assertFalse(outside.exists(), "archive member escaped the output directory")
                if result.returncode == 0:
                    check_layout(out, config, layers)

    def test_referenced_traversal_cannot_import_host_file(self):
        config, layers = image_fixture()
        outside = self.workdir / "host-config.json"
        outside.write_bytes(config)
        archive = self.workdir / "referenced-traversal.tar"
        with tarfile.open(archive, "w") as contents:
            add_bytes(contents, "layer.tar", layers[0])
            manifest = [{"Config": "../host-config.json", "RepoTags": ["bad:latest"], "Layers": ["layer.tar"]}]
            add_bytes(contents, "manifest.json", json.dumps(manifest).encode())
        result = self.run_import(archive, self.workdir / "out")
        self.assertNotEqual(result.returncode, 0, "import read config from outside the archive")
        self.assertEqual(outside.read_bytes(), config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regctl", type=Path, required=True)
    parser.add_argument("--wrapper", type=Path, required=True)
    ARGS, rest = parser.parse_known_args()
    ARGS.regctl = ARGS.regctl.resolve()
    ARGS.wrapper = ARGS.wrapper.resolve()
    unittest.main(argv=[__file__, *rest])
