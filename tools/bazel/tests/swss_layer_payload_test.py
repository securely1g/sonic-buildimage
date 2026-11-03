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
from tools.bazel.ci.swss_layer import verify_source_runtime
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


if __name__ == "__main__":
    unittest.main()
