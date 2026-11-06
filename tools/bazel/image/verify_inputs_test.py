#!/usr/bin/env python3
"""Check receipt-bound copies and the generated graph-control boundary."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_inputs


class VerifyInputsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / verify_inputs.INPUT_DIRECTORY
        names = verify_inputs.REQUIRED_INPUTS | {
            "images/docker-a.gz", "images/docker-b.gz", "installer/files/install.sh",
            "installer/files/empty.conf",
        }
        self.inputs = {name: self.directory / name for name in names}
        for name, path in self.inputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"" if name.endswith("empty.conf") else (name + " bytes\n").encode())
        for name in verify_inputs.GRAPH_CONTROLS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# assembly graph\n" if "/vs/" in name else "# graph control " + name + "\n")
        self.receipt_path = self.directory / "provenance.json"
        self.receipt = {
            "schema": verify_inputs.SCHEMA,
            "native_config_flags": {},
            "files": {name: verify_inputs.file_identity(path) for name, path in self.inputs.items()},
            "graph_controls": {name: verify_inputs.file_identity(self.root / name)
                               for name in verify_inputs.GRAPH_CONTROLS},
        }
        self.write_receipt()
        self.outputs = {name: self.root / "bazel-out/verified" / name for name in names}
        self.receipt_output = self.root / "bazel-out/verified/provenance.json"

    def write_receipt(self):
        self.receipt_path.write_text(json.dumps(self.receipt, indent=2, sort_keys=True) + "\n")

    def copy(self, inputs=None):
        return verify_inputs.verify_and_copy(self.receipt_path, self.inputs if inputs is None else inputs,
                                             self.outputs, self.receipt_output)

    def assert_no_outputs(self):
        self.assertFalse(self.receipt_output.exists())
        self.assertTrue(all(not path.exists() for path in self.outputs.values()))

    def snapshot(self):
        value, _ = verify_inputs.verify_bundle(self.root)
        return verify_inputs.verified_graph_snapshot(self.root, value)

    def test_copies_verified_bytes_and_receipt_to_individual_outputs(self):
        receipt_bytes, receipt = verify_inputs.verify_bundle(self.root)
        self.assertEqual(self.receipt, receipt)
        self.copy()
        for name, source in self.inputs.items():
            with self.subTest(name=name):
                self.assertEqual(source.read_bytes(), self.outputs[name].read_bytes())
                self.assertEqual(0o555, self.outputs[name].stat().st_mode & 0o777)
        self.assertEqual(receipt_bytes, self.receipt_output.read_bytes())
        original = self.outputs["images/docker-a.gz"].read_bytes()
        self.inputs["images/docker-a.gz"].write_bytes(b"later replacement archive")
        self.assertEqual(original, self.outputs["images/docker-a.gz"].read_bytes())
        self.assertEqual(self.inputs["images/docker-b.gz"].read_bytes(),
                         self.outputs["images/docker-b.gz"].read_bytes())

    def test_rejects_tampered_or_missing_staged_bytes_without_partial_outputs(self):
        path = self.inputs["images/docker-a.gz"]
        original = path.read_bytes()
        for change in ("tampered", "missing"):
            with self.subTest(change=change):
                if change == "tampered":
                    path.write_bytes(b"x" * len(original))
                else:
                    path.unlink()
                with self.assertRaisesRegex(ValueError, "size/SHA256|missing regular input"):
                    self.copy()
                self.assert_no_outputs()
                path.write_bytes(original)

    def test_rejects_changed_bytes_read_during_copy_even_when_source_is_restored(self):
        path = self.inputs["images/docker-a.gz"]
        original = path.read_bytes()
        real_open = Path.open

        class TransientReader:
            def __enter__(self):
                path.write_bytes(b"x" * len(original))
                self.stream = real_open(path, "rb")
                return self

            def read(self, size):
                block = self.stream.read(size)
                path.write_bytes(original)
                return block

            def __exit__(self, *arguments):
                self.stream.close()

        def transient_open(candidate, mode="r", *arguments, **keywords):
            return TransientReader() if candidate == path and mode == "rb" else real_open(candidate, mode, *arguments, **keywords)

        with mock.patch.object(Path, "open", transient_open):
            with self.assertRaisesRegex(ValueError, "size/SHA256"):
                self.copy()
        self.assertEqual(original, path.read_bytes())
        self.assert_no_outputs()

    def test_rejects_missing_or_extra_declared_names(self):
        missing = {name: path for name, path in self.inputs.items() if name != "images/docker-a.gz"}
        extra = dict(self.inputs, **{"images/docker-extra.gz": self.inputs["images/docker-a.gz"]})
        for inputs in (missing, extra):
            with self.subTest(names=sorted(inputs)):
                with self.assertRaisesRegex(ValueError, "declared input names differ from receipt"):
                    self.copy(inputs)
                self.assert_no_outputs()

    def test_rejects_missing_or_extra_bundle_files(self):
        path = self.inputs["images/docker-a.gz"]
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "inventory differs.*missing"):
            verify_inputs.verify_bundle(self.root)
        path.write_bytes(original)
        extra = self.directory / "images/docker-extra.gz"
        extra.write_bytes(b"extra archive")
        with self.assertRaisesRegex(ValueError, "inventory differs.*extra"):
            verify_inputs.verify_bundle(self.root)

    def test_rejects_changed_or_missing_graph_controls(self):
        for name in sorted(verify_inputs.GRAPH_CONTROLS):
            path = self.root / name
            original = path.read_bytes()
            with self.subTest(name=name, change="tampered"):
                path.write_bytes(b"different graph\n")
                with self.assertRaisesRegex(ValueError, "graph control failed"):
                    verify_inputs.verify_bundle(self.root)
                path.write_bytes(original)
            with self.subTest(name=name, change="missing"):
                path.unlink()
                with self.assertRaisesRegex(ValueError, "graph control|inventory differs"):
                    verify_inputs.verify_bundle(self.root)
                path.write_bytes(original)

    def test_rejects_invalid_graph_control_records(self):
        original = self.receipt["graph_controls"]
        missing = {name: value for name, value in original.items() if not name.endswith("inputs.bzl")}
        extra = dict(original, **{"tools/bazel/image/extra.bzl": verify_inputs.bytes_identity(b"extra")})
        for controls in (missing, extra):
            with self.subTest(names=sorted(controls)):
                self.receipt["graph_controls"] = controls
                self.write_receipt()
                with self.assertRaisesRegex(ValueError, "exactly the generated graph controls"):
                    verify_inputs.verify_bundle(self.root)
        self.receipt["graph_controls"] = original
        for name in ("installer/files/BUILD", "installer/files/BUILD.bazel"):
            with self.subTest(staged_package_marker=name):
                self.receipt["files"][name] = verify_inputs.bytes_identity(b"subpackage marker\n")
                self.write_receipt()
                with self.assertRaisesRegex(ValueError, "cannot define a Bazel subpackage"):
                    verify_inputs.verify_bundle(self.root)
                del self.receipt["files"][name]

    def test_rejects_active_graph_rebound_away_from_template(self):
        name = "tools/bazel/image/vs/BUILD.bazel"
        (self.root / name).write_text("# customized active graph\n")
        self.receipt["graph_controls"][name] = verify_inputs.file_identity(self.root / name)
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, "must match its bound template"):
            verify_inputs.verify_bundle(self.root)

    def test_rejects_old_unbound_bundle_without_changing_it(self):
        self.receipt["schema"] = 1
        del self.receipt["graph_controls"]
        self.write_receipt()
        before = {path.relative_to(self.directory): path.read_bytes()
                  for path in self.directory.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(ValueError, "unbound or unsupported.*fresh receipt-bound"):
            verify_inputs.verify_bundle(self.root)
        with self.assertRaisesRegex(ValueError, "unbound or unsupported.*fresh receipt-bound"):
            self.copy()
        self.assertEqual(before, {path.relative_to(self.directory): path.read_bytes()
                                  for path in self.directory.rglob("*") if path.is_file()})
        self.assert_no_outputs()

    def test_rejects_linked_bundle_input(self):
        path = self.inputs["images/docker-a.gz"]
        actual = self.root / "archive"
        path.rename(actual)
        path.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, "prepared input is linked"):
            verify_inputs.verify_bundle(self.root)

    def test_snapshot_retains_verified_graph_and_stable_input_links(self):
        overlay = self.snapshot()
        value = self.receipt_path.read_bytes()
        self.assertEqual(self.root / verify_inputs.GRAPH_DIRECTORY /
                         verify_inputs.bytes_identity(value)["sha256"] / "workspace", overlay)
        controls = verify_inputs.GRAPH_CONTROLS | {verify_inputs.INPUT_DIRECTORY + "/provenance.json"}
        before = {}
        for name in controls:
            path = overlay / name
            self.assertFalse(path.is_symlink())
            self.assertEqual(1, path.stat().st_nlink)
            self.assertEqual(0o555, path.stat().st_mode & 0o777)
            self.assertEqual((self.root / name).read_bytes(), path.read_bytes())
            before[name] = path.stat().st_mtime_ns
        for name, source in self.inputs.items():
            path = overlay / verify_inputs.INPUT_DIRECTORY / name
            self.assertTrue(path.is_symlink())
            self.assertEqual(source, path.resolve(strict=True))
        graph = self.directory / "inputs.bzl"
        original = graph.read_bytes()
        graph.write_bytes(b"IMAGE_INPUTS = {\"epoch\": 999}\n")
        self.assertEqual(original, (overlay / verify_inputs.INPUT_DIRECTORY / "inputs.bzl").read_bytes())
        graph.write_bytes(original)
        self.assertEqual(overlay, self.snapshot())
        self.assertEqual(before, {name: (overlay / name).stat().st_mtime_ns for name in controls})
        verify_inputs.verify_and_copy(
            overlay / verify_inputs.INPUT_DIRECTORY / "provenance.json",
            {name: overlay / verify_inputs.INPUT_DIRECTORY / name for name in self.inputs},
            self.outputs, self.receipt_output)
        self.assertEqual(value, self.receipt_output.read_bytes())

    def test_rejects_and_preserves_mismatched_retained_graph_copy(self):
        overlay = self.snapshot()
        graph = overlay / verify_inputs.INPUT_DIRECTORY / "inputs.bzl"
        graph.chmod(0o644)
        graph.write_bytes(b"different retained graph\n")
        graph.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "retained image graph snapshot.*move it aside"):
            self.snapshot()
        self.assertEqual(b"different retained graph\n", graph.read_bytes())

    def test_rejects_and_preserves_mismatched_retained_input_link(self):
        overlay = self.snapshot()
        path = overlay / verify_inputs.INPUT_DIRECTORY / "images/docker-a.gz"
        path.parent.chmod(0o755)
        path.unlink()
        path.symlink_to(self.inputs["images/docker-b.gz"])
        path.parent.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "retained image graph snapshot.*move it aside"):
            self.snapshot()
        self.assertEqual(self.inputs["images/docker-b.gz"], path.readlink())

    def test_rejects_and_preserves_incomplete_snapshot_claim(self):
        value = self.receipt_path.read_bytes()
        directory = self.root / verify_inputs.GRAPH_DIRECTORY / verify_inputs.bytes_identity(value)["sha256"]
        directory.mkdir(parents=True)
        sentinel = directory / "interrupted-creation"
        sentinel.write_text("preserve for inspection\n")
        with self.assertRaisesRegex(ValueError, "retained image graph snapshot.*move it aside"):
            self.snapshot()
        self.assertEqual("preserve for inspection\n", sentinel.read_text())


if __name__ == "__main__":
    unittest.main()
