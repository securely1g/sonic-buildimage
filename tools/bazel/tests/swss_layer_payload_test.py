"""Keep source-TAR checks consistent with installed OCI link semantics."""

import copy
import io
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.swss_layer import verify_source_runtime, verify_libteam_metadata
from tools.bazel.oci import image_validation


class SwssSourcePayloadTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.archive = Path(self.temporary.name) / "source.tar"
        self.binary = "usr/lib/x86_64-linux-gnu/libprotobuf.so.32.0.12"
        self.link = "usr/lib/x86_64-linux-gnu/libprotobuf.so.32"
        content = b"\x7fELF\x02\x01" + bytes(10) + struct.pack("<HH", 3, 62) + bytes(44)
        with tarfile.open(self.archive, "w") as tar:
            binary = tarfile.TarInfo(self.binary)
            binary.mode, binary.size = 0o644, len(content)
            tar.addfile(binary, io.BytesIO(content))
            link = tarfile.TarInfo(self.link)
            link.type, link.mode = tarfile.SYMTYPE, 0
            link.linkname = "./libprotobuf.so.32.0.12"
            tar.addfile(link)
        self.files = {}
        image_validation.apply_layer(self.archive, self.files)

    def test_owner_link_is_compared_with_the_same_installed_semantics(self):
        runtime, native = verify_source_runtime(self.files, self.archive)
        self.assertEqual(runtime[self.link]["mode"], 0)
        self.assertEqual(runtime[self.link]["linkname"], "libprotobuf.so.32.0.12")
        self.assertEqual(set(native), {self.binary})
        self.assertEqual(native[self.binary]["mode"], 0o644)

    def test_link_target_and_ownership_changes_are_rejected(self):
        for key, value in (("linkname", "libprotobuf.so.999"), ("uid", 1000), ("gid", 1000)):
            with self.subTest(key=key):
                files = copy.deepcopy(self.files)
                files[self.link][key] = value
                with self.assertRaisesRegex(ValueError, "OCI changed source-owned payload"):
                    verify_source_runtime(files, self.archive)

    def test_file_bytes_permissions_and_missing_payload_are_rejected(self):
        for key, value in (("sha256", "0" * 64), ("mode", 0o755), ("uid", 1000)):
            with self.subTest(key=key):
                files = copy.deepcopy(self.files)
                files[self.binary][key] = value
                with self.assertRaisesRegex(ValueError, "OCI changed source-owned payload"):
                    verify_source_runtime(files, self.archive)
        files = copy.deepcopy(self.files)
        del files[self.link]
        with self.assertRaisesRegex(ValueError, "OCI changed source-owned payload"):
            verify_source_runtime(files, self.archive)


class LibteamMetadataTest(unittest.TestCase):
    def setUp(self):
        self.native = {}
        self.metadata = {"schema": 1, "architecture": "amd64",
                         "source_modules": {"libteam": {"version": "1.31.0.sonic.1", "module_sha256": "a" * 64}},
                         "source_packages": []}
        for package, target, filename in (
                ("libteam5", "libteam_runtime", "libteam.so.5.6.1"),
                ("libteamdctl0", "libteamdctl_runtime", "libteamdctl.so.0.1.5")):
            path = "usr/lib/x86_64-linux-gnu/" + filename
            item = {"sha256": "b" * 64, "mode": 0o755, "uid": 0, "gid": 0}
            self.native[path] = item
            self.metadata["source_packages"].append({
                "package": package, "target": "@libteam//:" + target,
                "source_revision": "1.31.0.sonic.1", "payload_sha256": "c" * 64,
                "native_files": [{"path": path, **item}]})

    def test_two_library_records_match_selected_owner_and_installed_native_bytes(self):
        report = verify_libteam_metadata(self.metadata, self.native)
        self.assertEqual(report["owner"], self.metadata["source_modules"]["libteam"])
        self.assertEqual(len(report["packages"]), 2)

    def test_old_owner_missing_or_extra_utils_records_are_rejected(self):
        for change in ("owner", "missing", "utils", "duplicate"):
            with self.subTest(change=change):
                metadata = copy.deepcopy(self.metadata)
                if change == "owner":
                    metadata["source_modules"] = {"sonic-buildimage": {"version": "0.0.1"}}
                elif change == "missing":
                    metadata["source_packages"].pop()
                elif change == "utils":
                    metadata["source_packages"].append({"package": "libteam-utils"})
                else:
                    metadata["source_packages"][1] = metadata["source_packages"][0]
                with self.assertRaises(ValueError):
                    verify_libteam_metadata(metadata, self.native)

    def test_wrong_target_revision_and_native_bytes_are_rejected(self):
        for field, value in (("target", "@sonic_swss//dist:libteam_runtime"),
                             ("source_revision", "0.0.1"), ("payload_sha256", "")):
            with self.subTest(field=field):
                metadata = copy.deepcopy(self.metadata)
                metadata["source_packages"][0][field] = value
                with self.assertRaises(ValueError):
                    verify_libteam_metadata(metadata, self.native)
        for field, value in (("sha256", "d" * 64), ("mode", 0o644), ("uid", 1000), ("gid", 1000)):
            with self.subTest(field=field):
                metadata = copy.deepcopy(self.metadata)
                metadata["source_packages"][0]["native_files"][0][field] = value
                with self.assertRaises(ValueError):
                    verify_libteam_metadata(metadata, self.native)


if __name__ == "__main__":
    unittest.main()
