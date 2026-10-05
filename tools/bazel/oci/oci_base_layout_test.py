#!/usr/bin/env python3
"""Reject incomplete, corrupt or incompatible Make OCI bases."""

import json
from pathlib import Path
import tempfile
import unittest

from oci_base_fixture import digest, image_fixture, oci_files, write_layout
from oci_layout import validate_layout


class LayoutTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.layout = Path(temporary.name)

    def prepare(self, architecture="amd64", os_name="linux", **kwargs):
        config, layers = image_fixture(architecture, os_name)
        files = oci_files(config, layers, **kwargs)
        write_layout(self.layout, files)
        return files

    def test_valid_layout_is_not_modified(self):
        files = self.prepare()
        validate_layout(self.layout, "linux/amd64")
        self.assertEqual(files, {str(path.relative_to(self.layout)): path.read_bytes()
                                 for path in self.layout.rglob("*") if path.is_file()})

    def test_actual_config_and_descriptor_platform_must_both_match(self):
        for os_name, config_arch, descriptor_arch in (
            ("linux", "arm64", "amd64"),
            ("linux", "amd64", "arm64"),
            ("windows", "amd64", "amd64"),
        ):
            with self.subTest(os=os_name, config=config_arch, descriptor=descriptor_arch):
                self.prepare(config_arch, os_name, descriptor_platform={"os": "linux", "architecture": descriptor_arch})
                with self.assertRaisesRegex(ValueError, "platform"):
                    validate_layout(self.layout, "linux/amd64")

    def test_missing_config_platform_fails_closed(self):
        for missing in ("os", "architecture"):
            with self.subTest(missing=missing):
                config, layers = image_fixture()
                parsed = json.loads(config)
                del parsed[missing]
                files = oci_files(json.dumps(parsed).encode(), layers,
                                  descriptor_platform={"os": "linux", "architecture": "amd64"})
                write_layout(self.layout, files)
                with self.assertRaisesRegex(ValueError, "platform"):
                    validate_layout(self.layout, "linux/amd64")

    def test_multiple_images_are_rejected(self):
        self.prepare()
        index_path = self.layout / "index.json"
        index = json.loads(index_path.read_bytes())
        index["manifests"] *= 2
        index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "single OCI image"):
            validate_layout(self.layout, "linux/amd64")

    def test_corrupt_and_missing_layer_fail(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.prepare()
                _, layers = image_fixture()
                path = self.layout / "blobs" / digest(layers[0]).replace(":", "/")
                if missing:
                    path.unlink()
                else:
                    path.write_bytes(b"corrupt layer")
                with self.assertRaises((ValueError, FileNotFoundError)):
                    validate_layout(self.layout, "linux/amd64")

    def test_bad_descriptor_cannot_escape_layout(self):
        self.prepare()
        path = self.layout / "index.json"
        index = json.loads(path.read_bytes())
        index["manifests"][0]["digest"] = "sha256:../../host-file"
        path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "blob reference"):
            validate_layout(self.layout, "linux/amd64")

    def test_wrong_blob_size_fails(self):
        self.prepare()
        path = self.layout / "index.json"
        index = json.loads(path.read_bytes())
        index["manifests"][0]["size"] += 1
        path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            validate_layout(self.layout, "linux/amd64")

    def test_invalid_metadata_is_reported_as_value_error(self):
        for name, contents in (("oci-layout", b"null"), ("index.json", b"[]")):
            with self.subTest(name=name):
                self.prepare()
                (self.layout / name).write_bytes(contents)
                with self.assertRaises(ValueError):
                    validate_layout(self.layout, "linux/amd64")


if __name__ == "__main__":
    unittest.main()
