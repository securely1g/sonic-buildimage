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
                         "source_submodules": {"src/example": "b" * 40}}
        self.make_environment = {
            "BAZEL_PLATFORM": "vs", "BAZEL_ARCH": "amd64", "BAZEL_DISTRO": "trixie",
            "BAZEL_IMAGE_VERSION": "test", "BAZEL_INSTALLED_DOCKERS": "docker-orchagent.gz docker-sysmgr.gz",
            "BAZEL_SWSS_PREREQUISITES": "target/docker-config-engine-trixie.gz target/python-wheels/trixie/scapy.whl",
            "BAZEL_CONFIG_FLAGS": "BAZEL_MIN_READINESS=bazel_disabled SECURE_UPGRADE_MODE=no_sign",
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
            "target/docker-sysmgr.gz": "native sysmgr bytes",
            "target/docker-config-engine-trixie.gz": "native config-engine bytes",
            "target/python-wheels/trixie/scapy.whl": "native scapy bytes",
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
        receipt = producer.finish(self.root, self.environment)
        self.assertEqual(original_inode, self.snapshot.stat().st_ino)
        self.assertEqual(b"hsqs-fresh-native-snapshot", self.snapshot.read_bytes())
        with tarfile.open(self.output / "host-source.tar") as archive:
            self.assertEqual(b"changed current service definition\n", archive.extractfile("swss.service").read())
        self.assertEqual({"docker-sysmgr.gz": "target/docker-sysmgr.gz"},
                         json.loads((self.output / "images.json").read_text()))
        self.assertEqual(self.identity["source_submodules"], receipt["source_submodules"])
        for name, metadata in receipt["files"].items():
            contents = (self.root / name).read_bytes()
            self.assertEqual(len(contents), metadata["bytes"])
            self.assertEqual(hashlib.sha256(contents).hexdigest(), metadata["sha256"])
        self.assertIn("target/docker-config-engine-trixie.gz", receipt["files"])
        self.assertIn("target/python-wheels/trixie/scapy.whl", receipt["files"])
        self.assertNotIn("target/docker-orchagent.gz", receipt["files"])

    def test_preexisting_native_inputs_are_not_adopted(self):
        self.begin()
        with self.assertRaisesRegex(ValueError, "already exists"):
            producer.begin(self.root, self.make_environment)
        self.assertEqual(b"hsqs-fresh-native-snapshot", self.snapshot.read_bytes())

    def test_changed_source_commit_cannot_publish_a_passed_handoff(self):
        self.begin()
        with mock.patch.object(producer, "source_identity", return_value=dict(self.identity, source_commit="c" * 40)):
            with self.assertRaisesRegex(ValueError, "identity changed"):
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
