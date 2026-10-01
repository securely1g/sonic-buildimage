#!/usr/bin/env python3
"""Verify that only this job's source-built native outputs reach image assembly."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import image_inputs


class InputVerificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        self.native = self.workspace / image_inputs.NATIVE
        self.native.mkdir(parents=True)
        self.invocation = "this-job"
        self.source = {"source_commit": "a" * 40, "components": {
            "src/component": {"commit": "b" * 40, "gitlink": "b" * 40}}}
        self.identity = {"source_commit": "a" * 40, "source_branch": "ci",
                         "source_date_epoch": "123", "image_version": "source.fixture"}
        for name in image_inputs.REQUIRED:
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"source-built native bytes")
        (self.native / "host-config.json").write_text(json.dumps({"identity": self.identity}))
        self.images = {"docker-config-engine-trixie.gz": image_inputs.CONFIG_ENGINE}
        (self.native / "images.json").write_text(json.dumps(self.images))
        self.provenance = {"schema": 1, **self.identity,
                           "source_submodules": {"src/component": "b" * 40}, "files": {}}
        self.provenance_path = self.native / "provenance.json"
        self.rehash()
        self.native_receipt = self.root / "native-receipt.json"
        self.run = {"schema": 1, "status": "passed", "invocation": self.invocation,
                    "source_commit": self.source["source_commit"],
                    "native_provenance": image_inputs.NATIVE + "provenance.json",
                    "worker_image": "sha256:" + "c" * 64}
        self.write_run()
        self.spec = self.root / "execution-environment.json"
        self.worker = dict(image_inputs.ENVIRONMENT, worker_image="sha256:" + "c" * 64)
        self.spec.write_text(json.dumps(self.worker))
        self.receipt = self.root / "input-receipt.json"

    def write_run(self):
        self.native_receipt.write_text(json.dumps(self.run))

    def write_provenance(self):
        self.provenance_path.write_text(json.dumps(self.provenance))

    def rehash(self):
        self.provenance["files"] = {name: {"bytes": (self.workspace / name).stat().st_size,
                                           "sha256": image_inputs.sha256(self.workspace / name)}
                                    for name in image_inputs.REQUIRED}
        self.write_provenance()

    def verify(self):
        return image_inputs.verify_native(self.native_receipt, self.workspace, self.source, self.invocation)

    def prepare(self):
        return image_inputs.prepare(self.native_receipt, self.workspace, self.root / "scratch",
                                    self.receipt, self.spec, self.source, self.invocation)

    def test_exact_same_job_native_outputs_are_accepted(self):
        provenance, images, digest = self.verify()
        self.assertEqual(provenance, self.provenance)
        self.assertEqual(images, self.images)
        self.assertEqual(digest, image_inputs.sha256(self.provenance_path))

    def test_failed_foreign_invocation_and_foreign_source_receipts_are_rejected(self):
        for field, value in (("status", "failed"), ("invocation", "old-job"),
                             ("source_commit", "d" * 40), ("native_provenance", "../elsewhere")):
            with self.subTest(field=field):
                saved = self.run[field]
                self.run[field] = value
                self.write_run()
                with self.assertRaises(ValueError):
                    self.verify()
                self.run[field] = saved
                self.write_run()

    def test_native_source_commit_submodules_and_host_identity_must_match(self):
        for field, value in (("source_commit", "d" * 40), ("source_submodules", {}),
                             ("source_branch", "different"), ("image_version", "old"),
                             ("source_date_epoch", "999")):
            with self.subTest(field=field):
                saved = self.provenance[field]
                self.provenance[field] = value
                self.write_provenance()
                with self.assertRaises(ValueError):
                    self.verify()
                self.provenance[field] = saved
                self.write_provenance()

    def test_native_file_content_and_length_are_verified(self):
        path = self.native / "host-source.tar"
        content = path.read_bytes()
        for actual in (content[:-1], content + b"x", b"X" + content[1:]):
            with self.subTest(actual=actual):
                path.write_bytes(actual)
                with self.assertRaisesRegex(ValueError, "size/SHA256"):
                    self.verify()
        path.write_bytes(content)

    def test_missing_required_input_is_rejected(self):
        del self.provenance["files"][image_inputs.CONFIG_ENGINE]
        self.write_provenance()
        with self.assertRaisesRegex(ValueError, "omitted required"):
            self.verify()

    def test_input_paths_cannot_escape_or_point_to_completed_installers(self):
        for name in ("../escape", "/absolute", "target/../escape", "target//bazel-native/file",
                     "target/bazel-native/./file", "target/sonic-vs.bin", "bazel-out/image"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                image_inputs.local_input(self.workspace, name)

    def test_symlinked_file_or_parent_cannot_supply_inputs(self):
        path = self.native / "host-source.tar"
        outside = self.root / "outside"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink|escapes"):
            self.verify()
        path.unlink()
        path.write_bytes(outside.read_bytes())
        moved = self.root / "moved-native"
        self.native.rename(moved)
        self.native.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink|escapes"):
            self.verify()

    def test_service_archives_must_be_declared_and_swss_cannot_be_imported(self):
        for images in ({"docker-other.gz": "target/docker-other.gz"},
                       {"docker-orchagent.gz": image_inputs.CONFIG_ENGINE},
                       {"different.gz": image_inputs.CONFIG_ENGINE}):
            with self.subTest(images=images):
                (self.native / "images.json").write_text(json.dumps(images))
                self.rehash()
                with self.assertRaises(ValueError):
                    self.verify()

    def test_prepare_resolves_native_archives_and_uses_current_installer_sources(self):
        def prepare(command, **kwargs):
            self.assertEqual(kwargs, {"cwd": self.workspace, "check": True})
            self.assertEqual(command[command.index("--installer-source") + 1], str(self.workspace))
            images = json.loads(Path(command[command.index("--images") + 1]).read_text())
            self.assertEqual(images, {name: str(self.workspace / path) for name, path in self.images.items()})
            output = Path(command[command.index("--output") + 1])
            output.mkdir()
            (output / "inputs.bzl").write_text('IMAGE_INPUTS = {"images": {"docker-orchagent.gz": "//dockers/docker-orchagent:docker-orchagent.gz"}}\n')
            (output / "execution-environment.json").write_text(self.spec.read_text())
            (output / "provenance.json").write_text('{"schema": 1}')
        with mock.patch.object(image_inputs.subprocess, "run", side_effect=prepare) as run:
            receipt = self.prepare()
        run.assert_called_once()
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["invocation"], self.invocation)
        self.assertEqual(json.loads(self.receipt.read_text()), receipt)

    def test_existing_prepared_bundle_is_not_reused(self):
        output = self.workspace / "target/bazel-image-inputs"
        output.mkdir()
        retained = output / "host-source.tar"
        retained.write_bytes(b"old input")
        with mock.patch.object(image_inputs.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "already exist"):
            self.prepare()
        run.assert_not_called()
        self.assertEqual(retained.read_bytes(), b"old input")
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_failed_verification_does_not_prepare_and_records_failure(self):
        (self.native / "host-source.tar").write_bytes(b"changed native output")
        with mock.patch.object(image_inputs.subprocess, "run") as run, self.assertRaises(ValueError):
            self.prepare()
        run.assert_not_called()
        self.assertFalse((self.workspace / "target/bazel-image-inputs").exists())
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_different_native_execution_worker_is_rejected_before_preparation(self):
        self.run["worker_image"] = "sha256:" + "d" * 64
        self.write_run()
        with mock.patch.object(image_inputs.subprocess, "run") as run, \
                self.assertRaisesRegex(ValueError, "different execution worker"):
            self.prepare()
        run.assert_not_called()
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_preparer_failure_records_failure(self):
        with mock.patch.object(image_inputs.subprocess, "run", side_effect=RuntimeError("preparation failed")), \
                self.assertRaisesRegex(RuntimeError, "preparation failed"):
            self.prepare()
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_generated_inputs_are_literal_data_not_executable_code(self):
        path = self.root / "inputs.bzl"
        for text in ('IMAGE_INPUTS = dict(images={})', 'import os\nIMAGE_INPUTS = {}', 'OTHER = {}'):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                image_inputs.read_image_inputs(path)


if __name__ == "__main__":
    unittest.main()
