#!/usr/bin/env python3
"""Exercise the native boundary handoff without mounts or compiled packages."""

import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import producer


class NativeHandoffTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = {"source_commit": "a" * 40, "source_branch": "ci-test",
                         "source_submodules": {"src/example": "b" * 40},
                         "native_transformations": {}}
        self.make_environment = {
            "BAZEL_PLATFORM": "vs", "BAZEL_ARCH": "amd64", "BAZEL_DISTRO": "trixie",
            "BAZEL_IMAGE_VERSION": "test", "BAZEL_INSTALLED_DOCKERS": "docker-orchagent.gz docker-sysmgr.gz",
            "BAZEL_CONFIG_FLAGS": "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y SECURE_UPGRADE_MODE=no_sign",
            "SOURCE_DATE_EPOCH": "1",
        }
        self.environment = {
            "CONFIGURED_ARCH": "amd64", "CONFIGURED_PLATFORM": "vs", "TARGET_MACHINE": "vs",
            "IMAGE_TYPE": "onie", "IMAGE_DISTRO": "trixie", "SONIC_IMAGE_VERSION": "test",
            "SONIC_BAZEL_SOURCE_COMMIT": "a" * 40, "SONIC_BAZEL_SOURCE_BRANCH": "ci-test",
            "SOURCE_DATE_EPOCH": "1", "ONIE_IMAGE_PART_SIZE": "32768",
            "installer_start_scripts": "swss.sh", "installer_services": "swss.service",
            "installer_images": "swss|dockers/swss||target/docker-orchagent.gz:test sysmgr|dockers/sysmgr||target/docker-sysmgr.gz:test",
            "sonic_local_packages": "", "debs_path": "target/debs/trixie",
        }
        for directory in ("files/build_templates", "scripts", "src/sonic-build-hooks", "target/debs/trixie"):
            (self.root / directory).mkdir(parents=True)
        for name, content in {
            ".arch": "amd64", ".platform": "vs", "functions.sh": "", "onie-image.conf": "",
            "build_debian.sh": "\n".join(self.environment), "slave.mk": "",
            "swss.sh": "#!/bin/sh\nexit 0\n", "swss.service": "current service definition\n",
            "files/build_templates/docker_image_ctl.j2": "initial producer service-control template\n",
            "target/docker-sysmgr.gz": "native sysmgr bytes",
            "target/docker-orchagent.gz": "current component orchagent bytes",
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.identity_patch = mock.patch.object(producer, "source_identity", return_value=self.identity)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def begin(self):
        producer.begin(self.root, self.make_environment)
        self.output = self.root / producer.OUTPUT
        self.snapshot = self.output / "host-onie.squashfs"
        self.snapshot.write_bytes(b"hsqs-fresh-native-snapshot")

    def test_real_preparer_keeps_same_snapshot_and_hashes_current_generated_services(self):
        self.begin()
        original_inode = self.snapshot.stat().st_ino
        (self.root / "swss.service").write_text("changed current service definition\n")
        (self.root / "files/build_templates/docker_image_ctl.j2").write_text("current producer service-control template\n")
        receipt = producer.finish(self.root, self.environment)
        self.assertEqual(original_inode, self.snapshot.stat().st_ino)
        self.assertEqual(b"hsqs-fresh-native-snapshot", self.snapshot.read_bytes())
        with tarfile.open(self.output / "host-source.tar") as archive:
            self.assertEqual(b"changed current service definition\n", archive.extractfile("swss.service").read())
            self.assertEqual(b"current producer service-control template\n",
                             archive.extractfile("files/build_templates/docker_image_ctl.j2").read())
        self.assertEqual({"docker-sysmgr.gz": "target/docker-sysmgr.gz",
                          "docker-orchagent.gz": "target/docker-orchagent.gz"},
                         json.loads((self.output / "images.json").read_text()))
        self.assertEqual(self.identity["source_submodules"], receipt["source_submodules"])
        self.assertEqual("y", receipt["config_flags"]["BUILD_WITH_BAZEL_WHEN_AVAILABLE"])
        for name, metadata in receipt["files"].items():
            contents = (self.root / name).read_bytes()
            self.assertEqual(len(contents), metadata["bytes"])
            self.assertEqual(hashlib.sha256(contents).hexdigest(), metadata["sha256"])
        self.assertIn("target/docker-orchagent.gz", receipt["files"])
        self.assertEqual({"target/docker-orchagent.gz", "target/docker-sysmgr.gz"},
                         {name for name in receipt["files"] if name.endswith(".gz")})

    def test_preexisting_native_inputs_are_not_adopted(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "already exists"):
            producer.begin(self.root, self.make_environment)
        self.assertEqual(b"hsqs-fresh-native-snapshot", self.snapshot.read_bytes())

    def test_native_builder_selection_is_recorded_without_changing_the_archive_boundary(self):
        self.make_environment["BAZEL_CONFIG_FLAGS"] = "BUILD_WITH_BAZEL_WHEN_AVAILABLE=n SECURE_UPGRADE_MODE=no_sign"
        self.begin()
        receipt = producer.finish(self.root, self.environment)
        self.assertEqual("n", receipt["config_flags"]["BUILD_WITH_BAZEL_WHEN_AVAILABLE"])
        self.assertIn("target/docker-orchagent.gz", receipt["files"])

    def test_invalid_builder_value_fails_before_creating_native_outputs(self):
        self.make_environment["BAZEL_CONFIG_FLAGS"] = "BUILD_WITH_BAZEL_WHEN_AVAILABLE=invalid"
        with self.assertRaisesRegex(ValueError, "BUILD_WITH_BAZEL_WHEN_AVAILABLE"):
            producer.begin(self.root, self.make_environment)
        self.assertFalse((self.root / producer.OUTPUT).exists())

    def test_changed_source_commit_cannot_publish_a_passed_handoff(self):
        self.begin()
        with mock.patch.object(producer, "source_identity", return_value=dict(self.identity, source_commit="c" * 40)):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                producer.finish(self.root, self.environment)
        self.assertFalse((self.output / "provenance.json").exists())

    def test_verified_native_transformation_after_inventory_preserves_recorded_source(self):
        self.begin()
        transformation = {"src/sonic-frr/frr": {"recorded_commit": "b" * 40, "actual_commit": "c" * 40}}
        with mock.patch.object(producer, "source_identity", return_value=dict(
                self.identity, native_transformations=transformation)):
            receipt = producer.finish(self.root, self.environment)
        self.assertEqual(self.identity["source_submodules"], receipt["source_submodules"])
        self.assertEqual(transformation, receipt["native_transformations"])

    def test_transformation_observed_before_inventory_cannot_be_removed_or_replaced(self):
        transformation = {"src/sonic-frr/frr": {"actual_commit": "c" * 40}}
        self.identity["native_transformations"] = transformation
        self.begin()
        for changed in ({}, {"src/sonic-frr/frr": {"actual_commit": "d" * 40}}):
            with self.subTest(changed=changed), mock.patch.object(
                    producer, "source_identity", return_value=dict(self.identity, native_transformations=changed)):
                with self.assertRaisesRegex(ValueError, "transformation changed"):
                    producer.finish(self.root, self.environment)
        self.assertFalse((self.output / "provenance.json").exists())

    def test_missing_native_archive_fails_without_success_receipt(self):
        self.begin()
        (self.root / "target/docker-sysmgr.gz").unlink()
        with self.assertRaisesRegex(ValueError, "regular native input"):
            producer.finish(self.root, self.environment)
        self.assertFalse((self.output / "provenance.json").exists())

    def test_frozen_host_environment_must_match_current_version(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "current source identity"):
            producer.finish(self.root, dict(self.environment, SONIC_IMAGE_VERSION="stale"))
        self.assertFalse((self.output / "provenance.json").exists())


if __name__ == "__main__":
    unittest.main()
