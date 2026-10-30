#!/usr/bin/env python3
"""Keep verified input provenance intact while switching the assembly graph."""

import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import backend


class BackendTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "target/bazel-image-inputs"
        self.package = self.root / "tools/bazel/image/vs"
        self.inputs.mkdir(parents=True)
        self.package.mkdir(parents=True)
        self.template = "# generated fixture\nsonic_vs_image(\n" + backend.BUILD_LINE + ")\n"
        (self.package / "BUILD.bazel.in").write_text(self.template)
        (self.package / "BUILD.bazel").write_text(self.template.replace(backend.BUILD_LINE, ""))
        self.archive = self.inputs / "service.gz"
        self.archive.write_bytes(b"already prepared service archive")
        self.attributes = {"images": {"docker-service.gz": "//target/bazel-image-inputs:service.gz"},
                           "image_version": "native.0-aaaa", "epoch": 1}
        self.receipt = {
            "schema": 1, "native_provenance_sha256": "a" * 64,
            "native_source_identity": {"source_commit": "b" * 40},
            "assembler_source_identity": {"source_commit": "c" * 40},
            "native_config_flags": {"BUILD_WITH_BAZEL_WHEN_AVAILABLE": "n"},
            "files": {"service.gz": {"bytes": self.archive.stat().st_size,
                                      "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest()}},
        }
        (self.inputs / "inputs.bzl").write_text("IMAGE_INPUTS = " + repr(self.attributes) + "\n")
        (self.inputs / "provenance.json").write_text(json.dumps(self.receipt))

    def select(self, selected, build="y"):
        return backend.select_backend(self.root, build_with_bazel_when_available=build,
                                      use_container_image_store_as_backend=selected)

    def test_mode_switch_preserves_every_input_and_both_source_identities(self):
        before = self.archive.read_bytes(), self.archive.stat().st_mtime_ns
        for selected in ("y", "n", "y"):
            with self.subTest(selected=selected):
                self.assertEqual(self.select(selected), selected == "y")
                tree = ast.parse((self.inputs / "inputs.bzl").read_text())
                attributes = ast.literal_eval(tree.body[0].value)
                receipt = json.loads((self.inputs / "provenance.json").read_text())
                self.assertEqual(attributes.pop(backend.KEY), selected == "y")
                self.assertEqual(receipt.pop(backend.KEY), selected == "y")
                self.assertEqual(attributes, self.attributes)
                self.assertEqual(receipt, self.receipt)
                self.assertEqual(before, (self.archive.read_bytes(), self.archive.stat().st_mtime_ns))
                self.assertEqual((self.package / "BUILD.bazel").read_text(), self.template)

    def test_repeated_selection_does_not_rewrite_metadata(self):
        self.select("y")
        paths = [self.inputs / "inputs.bzl", self.inputs / "provenance.json", self.package / "BUILD.bazel"]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.select("y")
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_invalid_and_native_containerd_selection_fail_before_writing(self):
        before = (self.inputs / "inputs.bzl").read_bytes()
        for build, selected in (("n", "y"), ("yes", "n"), ("y", "yes"), ("y", "y n"), ("y", "")):
            with self.subTest(build=build, selected=selected), self.assertRaises(ValueError):
                self.select(selected, build)
        self.assertEqual((self.inputs / "inputs.bzl").read_bytes(), before)

    def test_legacy_bundle_without_verified_native_provenance_is_not_adopted(self):
        before = (self.inputs / "inputs.bzl").read_bytes()
        for key in ("native_provenance_sha256", "native_source_identity", "assembler_source_identity", "native_config_flags"):
            receipt = dict(self.receipt)
            del receipt[key]
            (self.inputs / "provenance.json").write_text(json.dumps(receipt))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "verified native-input"):
                self.select("y")
        self.assertEqual((self.inputs / "inputs.bzl").read_bytes(), before)

    def test_malformed_provenance_does_not_select_a_graph(self):
        for receipt in ([], {**self.receipt, "native_provenance_sha256": None},
                        {**self.receipt, "native_provenance_sha256": "not-a-digest"}):
            (self.inputs / "provenance.json").write_text(json.dumps(receipt))
            with self.subTest(receipt=receipt), self.assertRaisesRegex(ValueError, "verified native-input"):
                self.select("y")

    def test_recorded_backend_is_a_boolean(self):
        (self.inputs / "inputs.bzl").write_text("IMAGE_INPUTS = " + repr({**self.attributes, backend.KEY: "y"}))
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.select("y")
        (self.inputs / "inputs.bzl").write_text("IMAGE_INPUTS = " + repr(self.attributes))
        (self.inputs / "provenance.json").write_text(json.dumps({**self.receipt, backend.KEY: 1}))
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.select("y")

    def test_only_literal_input_assignment_is_accepted(self):
        for text in ("IMAGE_INPUTS = dict()", "OTHER = {}", "IMAGE_INPUTS = {}; X = 1", "IMAGE_INPUTS = []"):
            (self.inputs / "inputs.bzl").write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.select("y")

    def test_custom_or_missing_generated_target_is_not_replaced(self):
        path = self.package / "BUILD.bazel"
        path.write_text("# customized target\n")
        with self.assertRaisesRegex(ValueError, "customized or missing"):
            self.select("y")
        self.assertEqual(path.read_text(), "# customized target\n")
        path.unlink()
        with self.assertRaisesRegex(ValueError, "customized or missing"):
            self.select("y")

    def test_template_without_selector_fails_closed(self):
        (self.package / "BUILD.bazel.in").write_text(self.template.replace(backend.BUILD_LINE, ""))
        with self.assertRaisesRegex(ValueError, "does not declare"):
            self.select("y")

    def test_linked_metadata_is_not_followed(self):
        path = self.inputs / "provenance.json"
        other = self.root / "saved-provenance.json"
        path.replace(other)
        path.symlink_to(other)
        before = other.read_bytes()
        with self.assertRaisesRegex(ValueError, "must not be linked"):
            self.select("y")
        self.assertEqual(other.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
