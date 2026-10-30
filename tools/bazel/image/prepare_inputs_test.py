#!/usr/bin/env python3
"""Check the producer-to-assembly boundary with real small archive fixtures."""

import argparse
import ast
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prepare_inputs
from native import producer


class PrepareInputsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = self.root / "native"
        self.assembler = self.root / "assembler"
        self.source = {"source_commit": "a" * 40, "source_branch": "native-source",
                       "source_submodules": {"src/example": "b" * 40}, "native_transformations": {}}
        self.assembler_source = dict(self.source, source_commit="c" * 40, source_branch="assembler-source")
        self.environment = {
            "CONFIGURED_ARCH": "amd64", "CONFIGURED_PLATFORM": "vs", "TARGET_MACHINE": "vs",
            "IMAGE_TYPE": "onie", "IMAGE_DISTRO": "trixie", "SONIC_IMAGE_VERSION": "native.0-aaaa",
            "SONIC_BAZEL_SOURCE_COMMIT": self.source["source_commit"],
            "SONIC_BAZEL_SOURCE_BRANCH": self.source["source_branch"],
            "SOURCE_DATE_EPOCH": "1", "ONIE_IMAGE_PART_SIZE": "32768",
            "installer_start_scripts": "swss.sh", "installer_services": "swss.service",
            "installer_images": "swss|dockers/swss||target/docker-orchagent.gz:test sysmgr|dockers/sysmgr||target/docker-sysmgr.gz:test",
            "sonic_local_packages": "local|target/docker-local.gz|owner|true", "debs_path": "target/debs/trixie",
        }
        make_environment = {
            "BAZEL_PLATFORM": "vs", "BAZEL_ARCH": "amd64", "BAZEL_DISTRO": "trixie",
            "BAZEL_IMAGE_VERSION": self.environment["SONIC_IMAGE_VERSION"],
            "BAZEL_INSTALLED_DOCKERS": "docker-orchagent.gz docker-sysmgr.gz",
            "BAZEL_LOCAL_PACKAGES": "docker-local.gz",
            "BAZEL_CONFIG_FLAGS": "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y SECURE_UPGRADE_MODE=no_sign",
            "SOURCE_DATE_EPOCH": "1",
        }
        for name in ("files/build_templates", "scripts", "src/sonic-build-hooks", "target/debs/trixie"):
            (self.native / name).mkdir(parents=True)
        for name, text in {
            ".arch": "amd64", ".platform": "vs", "functions.sh": "", "onie-image.conf": "",
            "build_debian.sh": "\n".join(self.environment), "slave.mk": "",
            "swss.sh": "#!/bin/sh\nexit 0\n", "swss.service": "current native service\n",
            "target/docker-orchagent.gz": "orchagent archive bytes",
            "target/docker-sysmgr.gz": "sysmgr archive bytes",
            "target/docker-local.gz": "local archive bytes",
        }.items():
            (self.native / name).write_text(text)
        self.directory = self.native / producer.OUTPUT
        with mock.patch.object(producer, "source_identity", return_value=self.source), contextlib.redirect_stdout(io.StringIO()):
            producer.begin(self.native, make_environment)
            (self.directory / "host-onie.squashfs").write_bytes(b"hsqs-native-snapshot")
            producer.finish(self.native, self.environment)
        (self.assembler / "device").mkdir(parents=True)
        (self.assembler / "platform/vs").mkdir(parents=True)
        (self.assembler / "installer").mkdir()
        for name, text in {
            "onie-image.conf": "assembler ONIE config\n",
            "platform/vs/platform.conf": "assembler platform config\n",
            "platform/vs/platform-modules-vs.mk": "$(VS_PLATFORM_MODULE)_PLATFORM = x86_64-kvm_x86_64-r0\n",
            "installer/install.sh": "assembler install script\n",
            "installer/sharch_body.sh": "assembler shar script\n",
            "installer/default_platform.conf": "assembler default platform\n",
        }.items():
            (self.assembler / name).write_text(text)
        package = self.assembler / "tools/bazel/image/vs"
        package.mkdir(parents=True)
        (package / "BUILD.bazel.in").write_text("# fixture assembly package\n")
        self.output = self.assembler / "target/bazel-image-inputs"
        self.worker = self.root / "worker.json"
        self.worker.write_text(json.dumps(dict(prepare_inputs.ENVIRONMENT, worker_image="sha256:" + "d" * 64)))
        self.args = argparse.Namespace(native_directory=self.directory, installer_source=self.assembler,
                                       execution_environment=self.worker, output=self.output)
        self.identity_patch = mock.patch.object(prepare_inputs, "source_identity", side_effect=self.identity)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def identity(self, path):
        return self.source if path == self.native else self.assembler_source

    def prepare(self):
        with mock.patch.object(prepare_inputs, "ROOT", self.assembler):
            return prepare_inputs.prepare(self.args)

    def rewrite(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value))
        receipt_path = self.directory / "provenance.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["files"][prepare_inputs.NATIVE + name] = prepare_inputs.file_info(path)
        receipt_path.write_text(json.dumps(receipt))

    def test_real_producer_stages_every_archive_and_preserves_both_source_identities(self):
        receipt = self.prepare()
        assignment = ast.parse((self.output / "inputs.bzl").read_text()).body[0]
        attributes = ast.literal_eval(assignment.value)
        for name in ("docker-orchagent.gz", "docker-sysmgr.gz", "docker-local.gz"):
            self.assertEqual("//target/bazel-image-inputs:images/" + name, attributes["images"][name])
            self.assertEqual((self.native / "target" / name).read_bytes(), (self.output / "images" / name).read_bytes())
        self.assertEqual(["docker-local.gz"], attributes["local_images"])
        self.assertNotIn("//dockers/", (self.output / "inputs.bzl").read_text())
        self.assertEqual(self.source["source_commit"], receipt["native_source_identity"]["source_commit"])
        self.assertEqual(self.assembler_source["source_commit"], receipt["assembler_source_identity"]["source_commit"])
        self.assertEqual("y", receipt["native_config_flags"]["BUILD_WITH_BAZEL_WHEN_AVAILABLE"])
        self.assertEqual("native.0-aaaa", attributes["image_version"])
        self.assertEqual(1, attributes["epoch"])
        self.assertEqual("assembler install script\n", (self.output / "installer/files/install.sh").read_text())
        self.assertTrue((self.assembler / "tools/bazel/image/vs/BUILD.bazel").is_file())
        (self.native / "target/docker-orchagent.gz").write_bytes(b"later component change")
        self.assertEqual(b"orchagent archive bytes", (self.output / "images/docker-orchagent.gz").read_bytes())

    def test_missing_producer_provenance_is_rejected(self):
        (self.directory / "provenance.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_receipt_hashes_every_declared_input_and_all_installer_metadata(self):
        (self.assembler / "installer/empty.conf").write_bytes(b"")
        receipt = self.prepare()
        assignment = ast.parse((self.output / "inputs.bzl").read_text()).body[0]
        attributes = ast.literal_eval(assignment.value)
        labels = set(attributes["images"].values()) | set(attributes["installer_files"])
        labels.update(attributes[name] for name in (
            "source", "snapshot", "host_config", "execution_environment", "installer_config"))
        declared = {label.removeprefix("//target/bazel-image-inputs:") for label in labels}
        self.assertLessEqual(declared, receipt["files"].keys())
        installer_files = {path.relative_to(self.output).as_posix()
                           for path in (self.output / "installer").rglob("*") if path.is_file()}
        self.assertLessEqual({"installer/config.json", "installer/files-manifest.json",
                             "installer/provenance.json", "installer/files/platforms_asic"}, installer_files)
        self.assertLessEqual(installer_files, receipt["files"].keys())
        for name in declared | installer_files:
            with self.subTest(name=name):
                actual = prepare_inputs.file_info(self.output / name, allow_empty=True)
                self.assertEqual(actual, {key: receipt["files"][name][key] for key in ("bytes", "sha256")})
        copied = receipt["files"]["installer/files/install.sh"]
        self.assertEqual("assembler-copy", copied["source_kind"])
        self.assertEqual(str(self.assembler), copied["source"])
        self.assertEqual("installer/provenance.json", copied["source_provenance"])
        self.assertEqual("generated", receipt["files"]["installer/files/platforms_asic"]["source_kind"])
        self.assertEqual(str(self.directory / "installer-config.json"), receipt["files"]["installer/config.json"]["source"])
        self.assertEqual(0, receipt["files"]["installer/files/empty.conf"]["bytes"])

    def test_assembler_revision_change_during_staging_prevents_publication(self):
        original = prepare_inputs.installer.prepare_inputs

        def changed(*arguments):
            original(*arguments)
            self.assembler_source = dict(self.assembler_source, source_commit="e" * 40)

        with mock.patch.object(prepare_inputs.installer, "prepare_inputs", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "assembler source identity changed"):
                self.prepare()
        self.assertFalse((self.output / "inputs.bzl").exists())
        self.assertFalse((self.output / "provenance.json").exists())
        self.assertFalse((self.assembler / "tools/bazel/image/vs/BUILD.bazel").exists())

    def test_assembler_file_change_without_commit_change_prevents_publication(self):
        original = prepare_inputs.installer.prepare_inputs

        def changed(*arguments):
            original(*arguments)
            (self.assembler / "installer/install.sh").write_text("changed after staging\n")

        with mock.patch.object(prepare_inputs.installer, "prepare_inputs", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "assembler input changed.*installer/install.sh"):
                self.prepare()
        self.assertFalse((self.output / "inputs.bzl").exists())
        self.assertFalse((self.output / "provenance.json").exists())

    def test_substituted_archive_fails_its_producer_hash(self):
        (self.native / "target/docker-orchagent.gz").write_bytes(b"unexpected SWSS archive")
        with self.assertRaisesRegex(ValueError, "size/SHA256"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_changed_source_or_transformation_cannot_reuse_snapshot(self):
        for changes in ({"source_commit": "e" * 40}, {"source_submodules": {}},
                        {"native_transformations": {"src/changed": {}}}):
            with self.subTest(changes=changes), mock.patch.object(
                    prepare_inputs, "source_identity", return_value=dict(self.source, **changes)):
                with self.assertRaisesRegex(ValueError, "source identity|transformations"):
                    self.prepare()
        self.assertFalse(self.output.exists())

    def test_missing_or_extra_archive_mapping_fails_even_with_updated_metadata(self):
        original = json.loads((self.directory / "images.json").read_text())
        for images in ({name: value for name, value in original.items() if name != "docker-orchagent.gz"},
                       dict(original, **{"docker-extra.gz": "target/docker-extra.gz"})):
            with self.subTest(images=images):
                self.rewrite("images.json", images)
                with self.assertRaisesRegex(ValueError, "exactly match"):
                    self.prepare()
        self.assertFalse(self.output.exists())

    def test_source_label_cannot_replace_an_archive(self):
        images = json.loads((self.directory / "images.json").read_text())
        images["docker-orchagent.gz"] = "//dockers/docker-orchagent:docker-orchagent.gz"
        self.rewrite("images.json", images)
        with self.assertRaisesRegex(ValueError, "not declared"):
            self.prepare()

    def test_archive_symlink_is_rejected(self):
        path = self.native / "target/docker-orchagent.gz"
        actual = path.with_suffix(".actual")
        path.rename(actual)
        path.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, "linked"):
            self.prepare()

    def test_host_installer_version_or_epoch_mismatch_is_rejected(self):
        original = json.loads((self.directory / "installer-config.json").read_text())
        for config in (dict(original, image_version="stale"), dict(original, epoch=2)):
            with self.subTest(config=config):
                self.rewrite("installer-config.json", config)
                with self.assertRaisesRegex(ValueError, "identities disagree"):
                    self.prepare()

    def test_host_identity_cannot_be_relabelled_to_assembler_revision(self):
        config = json.loads((self.directory / "host-config.json").read_text())
        config["identity"]["source_commit"] = self.assembler_source["source_commit"]
        config["environment"]["SONIC_BAZEL_SOURCE_COMMIT"] = self.assembler_source["source_commit"]
        self.rewrite("host-config.json", config)
        with self.assertRaisesRegex(ValueError, "host identity differs"):
            self.prepare()

    def test_provenance_cannot_relabel_the_native_builder_selection(self):
        inventory = json.loads((self.directory / "inventory.json").read_text())
        inventory["config_flags"]["BUILD_WITH_BAZEL_WHEN_AVAILABLE"] = "n"
        self.rewrite("inventory.json", inventory)
        with self.assertRaisesRegex(ValueError, "build configuration differs"):
            self.prepare()

    def test_worker_must_be_pinned_to_supported_classic_environment(self):
        original = json.loads(self.worker.read_text())
        for worker in (dict(original, worker_image="worker:latest"), dict(original, storage_driver="vfs"),
                       dict(original, platform="linux/arm64"), dict(original, docker_version="29.0.0")):
            with self.subTest(worker=worker):
                self.worker.write_text(json.dumps(worker))
                with self.assertRaisesRegex(ValueError, "worker environment"):
                    self.prepare()
        self.assertFalse(self.output.exists())

    def test_prepared_inputs_are_never_replaced(self):
        self.prepare()
        before = (self.output / "inputs.bzl").read_bytes()
        with self.assertRaisesRegex(ValueError, "fresh unlinked"):
            self.prepare()
        self.assertEqual(before, (self.output / "inputs.bzl").read_bytes())

    def test_copy_failure_does_not_publish_a_bazel_package(self):
        with mock.patch.object(prepare_inputs.shutil, "copyfileobj", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                self.prepare()
        self.assertFalse((self.output / "inputs.bzl").exists())
        self.assertFalse((self.assembler / "tools/bazel/image/vs/BUILD.bazel").exists())


if __name__ == "__main__":
    unittest.main()
