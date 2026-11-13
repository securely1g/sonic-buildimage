"""Exercise real archive readers with plain-text members, never generated DEBs."""
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

HELPER = Path(__file__).resolve().parents[1] / "ci/recover_vs_original_inputs.py"
spec = importlib.util.spec_from_file_location("recover_inputs", HELPER)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as result:
        for name, content, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            item.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                item.linkname = "outside"
            result.addfile(item, io.BytesIO(content) if item.isreg() else None)
    return output.getvalue()


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir(mode=0o700)
        self.payload = b"plain test text: this is not a Debian package\n"

    def platform(self, append=b"", bad_wrapper=False):
        package = recovery.PLATFORM["package"]
        platform = gzip.compress(archive([
            ("common/", b"", tarfile.DIRTYPE),
            ("./common/" + package, self.payload, tarfile.REGTYPE),
        ]), mtime=0)
        fs = io.BytesIO()
        with zipfile.ZipFile(fs, "w", compression=zipfile.ZIP_STORED) as zipped:
            zipped.writestr("platform.tar.gz", platform)
            zipped.writestr("ignored-rootfs.bin", b"not opened by recovery")
        payload = archive([("installer/fs.zip", fs.getvalue(), tarfile.REGTYPE)])
        template = (b"#!/bin/sh\npayload_image_size=%%PAYLOAD_IMAGE_SIZE%%\n"
                    b"payload_sha1=%%IMAGE_SHA1%%\nexit_marker\n")
        header = template.replace(b"%%PAYLOAD_IMAGE_SIZE%%", str(len(payload)).encode())
        header = header.replace(b"%%IMAGE_SHA1%%", hashlib.sha1(payload).hexdigest().encode())
        data = header + payload + append
        path, wrapper = self.root / "original.bin", self.root / "wrapper.sh"
        path.write_bytes(data)
        if bad_wrapper:
            template = template.replace(b"#!/bin/sh", b"#!/bin/xx")
        wrapper.write_bytes(template)
        self.enterContext(mock.patch.dict(recovery.PLATFORM, {
            "bytes": len(data), "sha256": sha(data),
            "package_bytes": len(self.payload), "package_sha256": sha(self.payload),
        }))
        self.enterContext(mock.patch.dict(recovery.WRAPPER, {
            "bytes": len(template), "sha256": sha(template),
        }))
        return path, wrapper, data

    def device(self, *, extra=False, link=False, corrupt=False):
        entries = [("debs/", b"", tarfile.DIRTYPE),
                   ("debs/" + recovery.DEVICE["package"], self.payload,
                    tarfile.SYMTYPE if link else tarfile.REGTYPE)]
        if extra:
            entries.append(("unexpected", b"extra", tarfile.REGTYPE))
        expanded = archive(entries)
        data = gzip.compress(expanded, mtime=0)
        if corrupt:
            data = data[:-8] + bytes([data[-8] ^ 1]) + data[-7:]
        path = self.root / "original-layer.gz"
        path.write_bytes(data)
        self.enterContext(mock.patch.dict(recovery.DEVICE, {
            "bytes": len(data), "sha256": sha(data),
            "expanded_bytes": len(expanded), "diff_id": sha(expanded),
            "package_bytes": len(self.payload), "package_sha256": sha(self.payload),
        }))
        return path, data

    def test_platform_preserves_input_and_copies_only_the_original_member(self):
        path, wrapper, original = self.platform()
        result = recovery.recover("platform", path, self.output, wrapper)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((self.output / "execution" / recovery.PLATFORM["package"]).read_bytes(), self.payload)
        self.assertEqual(result["original_package"]["declared_member"], "./common/" + recovery.PLATFORM["package"])
        self.assertFalse((self.output / "execution/original-sonic-vs.bin").exists())
        self.assertFalse(result["package_generation"])
        self.assertFalse(result["installer_execution"])
        self.assertFalse(result["current_source_or_full_vs_admission"])

    def test_changed_original_rejected_before_creating_output(self):
        path, wrapper, _ = self.platform()
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(recovery.RecoveryError, "byte identity"):
            recovery.recover("platform", path, self.output, wrapper)
        self.assertFalse((self.output / "execution").exists())

    def test_wrapper_must_match_the_literal_installer_header(self):
        path, wrapper, _ = self.platform(bad_wrapper=True)
        with self.assertRaisesRegex(recovery.RecoveryError, "two-substitution template"):
            recovery.recover("platform", path, self.output, wrapper)

    def test_appended_signature_or_payload_is_not_silently_ignored(self):
        path, wrapper, _ = self.platform(append=b"extra")
        with self.assertRaisesRegex(recovery.RecoveryError, "appended signature"):
            recovery.recover("platform", path, self.output, wrapper)

    def test_device_layer_checks_complete_stream_and_keeps_original(self):
        path, original = self.device()
        result = recovery.recover("device-data", path, self.output)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result["nested_member"], "debs/" + recovery.DEVICE["package"])
        self.assertEqual((self.output / "execution" / recovery.DEVICE["package"]).read_bytes(), self.payload)
        metadata = json.loads((self.output / "execution/device-layer-inventory.json").read_bytes())
        self.assertEqual(metadata["diff_id"], recovery.DEVICE["diff_id"])
        self.assertTrue(metadata["gzip_trailer_checked"])

    def test_extra_device_layer_member_cannot_be_recovered_as_qualified(self):
        path, _ = self.device(extra=True)
        with self.assertRaisesRegex(recovery.RecoveryError, "Unexpected original COPY-layer member"):
            recovery.recover("device-data", path, self.output)
        self.assertFalse((self.output / "execution/recovered-original-input.json").exists())

    def test_package_symlink_is_not_followed(self):
        path, _ = self.device(link=True)
        with self.assertRaisesRegex(recovery.RecoveryError, "Unexpected original COPY-layer member"):
            recovery.recover("device-data", path, self.output)

    def test_bad_package_digest_never_gets_a_success_record(self):
        path, _ = self.device()
        with mock.patch.dict(recovery.DEVICE, {"package_sha256": "0" * 64}):
            with self.assertRaisesRegex(recovery.RecoveryError, "device package identity"):
                recovery.recover("device-data", path, self.output)
        self.assertFalse((self.output / "execution/recovered-original-input.json").exists())

    def test_gzip_checksum_failure_never_gets_a_success_record(self):
        path, _ = self.device(corrupt=True)
        with self.assertRaises((gzip.BadGzipFile, EOFError, recovery.RecoveryError)):
            recovery.recover("device-data", path, self.output)
        self.assertFalse((self.output / "execution/recovered-original-input.json").exists())

    def test_existing_output_is_preserved(self):
        path, _ = self.device()
        existing = self.output / "execution"
        existing.mkdir()
        marker = existing / "keep"
        marker.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            recovery.recover("device-data", path, self.output)
        self.assertEqual(marker.read_bytes(), b"original")

    def test_symlinked_output_parent_is_refused(self):
        path, _ = self.device()
        alias = self.root / "alias"
        alias.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(OSError):
            recovery.recover("device-data", path, alias)
        self.assertFalse((self.output / "execution").exists())


if __name__ == "__main__":
    unittest.main()
