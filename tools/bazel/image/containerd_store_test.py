#!/usr/bin/env python3
"""Integrity, cache compatibility, and containerd inventory regressions."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import containerd_import as ci
import containerd_store as store
from oci_bundle_test import fixture


class StoreContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = fixture(self.root / "input")
        self.part = self.root / "part"
        self.part.mkdir()
        self.archive = self.part / "docker-probe.oci.tar"
        shutil.copyfile(self.image["oci-archive"], self.archive)
        self.record = {"archive": "images/docker-probe.oci.tar", "reference": "docker.io/library/docker-probe:latest",
            "tags": ["docker.io/library/docker-probe:latest"], "digest": self.image["digest"], "sha256": ci.sha256(self.archive)}
        self.manifest = {"schema": 1, "architecture": "amd64", "images": [self.record]}
        self.write_manifest()
        self.expected = {"schema": 1, "platform": "linux/amd64", "worker_image": "sha256:" + "a" * 64,
                         "docker_version": "28.5.2", "distribution": "trixie"}

    def write_manifest(self):
        (self.part / "manifest.json").write_text(json.dumps(self.manifest))

    def base(self):
        archive = self.root / "cached-store.tar.gz"
        archive.write_bytes(b"base archive checksum fixture")
        receipt = {"schema": 1, "kind": "sonic-containerd-store", "architecture": "amd64", "namespace": "moby",
            "snapshotter": "overlayfs", "containerd_version": ci.VERSION, "execution_environment": self.expected,
            "root": "containerd-store", "images": [self.record], "tags": ci.validate_records([self.record]),
            "unpacked": False,
            "sha256": ci.sha256(archive), "bytes": archive.stat().st_size}
        path = self.root / "cached-store.json"
        path.write_text(json.dumps(receipt))
        return archive, path, receipt

    def test_real_oci_part_integrity(self):
        records, sources = store.parts_inventory([self.part], "amd64")
        self.assertEqual(records, [self.record])
        self.assertEqual(sources[self.record["archive"]], self.archive)

    def test_damaged_or_missing_archive_is_rejected(self):
        self.archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            store.parts_inventory([self.part], "amd64")
        self.archive.unlink()
        with self.assertRaises(FileNotFoundError):
            store.parts_inventory([self.part], "amd64")

    def test_symlink_archive_is_rejected(self):
        self.archive.unlink()
        self.archive.symlink_to(self.image["oci-archive"])
        with self.assertRaisesRegex(ValueError, "regular file"):
            store.parts_inventory([self.part], "amd64")

    def test_descriptor_and_actual_platform_must_match(self):
        self.record["digest"] = "sha256:" + "b" * 64
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "descriptor"):
            store.parts_inventory([self.part], "amd64")
        self.record["digest"] = self.image["digest"]
        self.manifest["architecture"] = "arm64"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "target platform"):
            store.parts_inventory([self.part], "arm64")

    def test_manifest_platform_and_schema_are_checked(self):
        with self.assertRaisesRegex(ValueError, "architecture"):
            store.parts_inventory([self.part], "arm64")
        self.manifest["schema"] = True
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "schema"):
            store.parts_inventory([self.part], "amd64")

    def test_alias_collision_and_archive_escape_are_rejected(self):
        second = copy.deepcopy(self.record)
        second["archive"] = "images/docker-second.oci.tar"
        with self.assertRaisesRegex(ValueError, "conflicting image tag"):
            ci.validate_records([self.record, second])
        for name in ("../outside.tar", "images/../outside.tar", "/images/docker-probe.oci.tar"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "archive path"):
                ci.validate_records([dict(self.record, archive=name)])

    def test_cached_base_identity_and_integrity(self):
        archive, path, receipt = self.base()
        self.assertEqual(store.base_inventory(archive, path, self.expected, "amd64"), receipt)
        archive.write_bytes(b"different base data")
        with self.assertRaisesRegex(ValueError, "checksum"):
            store.base_inventory(archive, path, self.expected, "amd64")

    def test_base_from_different_worker_or_runtime_is_rejected(self):
        archive, path, receipt = self.base()
        changed = dict(self.expected, worker_image="sha256:" + "b" * 64)
        with self.assertRaisesRegex(ValueError, "another worker"):
            store.base_inventory(archive, path, changed, "amd64")
        for field, value in (("architecture", "arm64"), ("snapshotter", "native"),
                             ("containerd_version", "2.0.0"), ("namespace", "default")):
            modified = dict(receipt, **{field: value})
            path.write_text(json.dumps(modified))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "incompatible"):
                store.base_inventory(archive, path, self.expected, "amd64")

    def test_base_inventory_cannot_omit_recorded_tags(self):
        archive, path, receipt = self.base()
        receipt["tags"] = {}
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "inventory"):
            store.base_inventory(archive, path, self.expected, "amd64")

    def test_cached_unpacked_base_is_rejected(self):
        archive, path, receipt = self.base()
        for value in (True, None):
            receipt["unpacked"] = value
            path.write_text(json.dumps(receipt))
            with self.subTest(unpacked=value), self.assertRaisesRegex(ValueError, "defer unpacking"):
                store.base_inventory(archive, path, self.expected, "amd64")

    def test_worker_identity_and_native_architecture(self):
        self.assertEqual(store.environment(self.expected, self.expected, "x86_64"), "amd64")
        arm = dict(self.expected, platform="linux/arm64")
        self.assertEqual(store.environment(arm, arm, "aarch64"), "arm64")
        with self.assertRaisesRegex(ValueError, "architecture"):
            store.environment(self.expected, self.expected, "aarch64")
        with self.assertRaisesRegex(ValueError, "does not match"):
            store.environment(self.expected, arm, "x86_64")

    def test_containerd_version_requires_complete_release_token(self):
        for version in ("1.7.28", "v1.7.28"):
            store.containerd_version("containerd github.com/containerd/containerd " + version + " build-sha")
        for version in ("1.7.28-rc.1", "1.7.28.0", "1.7.280", "1.7.27", "v1.7.28-sonic"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "wrong containerd version"):
                store.containerd_version("containerd github.com/containerd/containerd " + version + " build-sha")


class CtrInventoryTest(unittest.TestCase):
    tag = "docker.io/library/docker-probe:latest"
    digest = "sha256:" + "a" * 64
    header = "REF TYPE DIGEST STATUS SIZE UNPACKED\n"

    def ctr(self, stdout, returncode=0):
        return ci.Ctr(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout, ""))

    def test_only_complete_unpacked_images_are_ready(self):
        for status, unpacked, expected in (("complete", "true", {self.tag:self.digest}),
                                            ("complete", "false", {}), ("incomplete", "false", {})):
            row = f"{self.tag} manifest {self.digest} {status} 10 B {unpacked}\n"
            self.assertEqual(self.ctr(self.header+row).ready_images({self.tag}), expected)

    def test_empty_inventory_is_success_only_on_zero_status(self):
        self.assertEqual(self.ctr("").ready_images(set()), {})
        with self.assertRaisesRegex(ValueError, "failed without"):
            self.ctr("", 1).ready_images(set())

    def test_imported_content_is_required_before_and_after_first_use(self):
        for status in ("complete", "incomplete"):
            for actual in ("true", "false"):
                row = f"{self.tag} manifest {self.digest} {status} 10 B {actual}\n"
                for expected_state in (False, None):
                    expected = ({self.tag: self.digest}
                                if status == "complete" and (expected_state is None or actual == "false") else {})
                    with self.subTest(status=status, actual=actual, expected=expected_state):
                        self.assertEqual(self.ctr(self.header + row).ready_images(
                            {self.tag}, unpacked=expected_state), expected)

    def test_malformed_or_duplicate_rows_are_rejected(self):
        row = f"{self.tag} manifest {self.digest} complete 10 B true\n"
        for output in ("unexpected table", self.header+row+row):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.ctr(output).ready_images({self.tag})

    def test_partial_failed_check_retains_usable_rows(self):
        row = f"{self.tag} manifest {self.digest} incomplete 10 B false\n"
        self.assertEqual(self.ctr(self.header+row, 1).ready_images({self.tag}), {})


if __name__ == "__main__":
    unittest.main()
