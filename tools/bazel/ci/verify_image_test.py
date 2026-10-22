#!/usr/bin/env python3
"""Reject corrupt, incomplete and stale installer chains using small archives."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import warnings
import zipfile

import verify_image


def tar_bytes(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name.rstrip("/"))
            info.mode = 0o755 if name.endswith(("/", ".sh")) else 0o644
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


class VerifyImageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installer = self.root / "sonic-vs.bin"
        self.payload = self.root / "fs.zip"
        self.dockerfs = self.root / "dockerfs.tar.gz"
        self.squashfs = self.root / "fs.squashfs"
        self.boot = self.root / "boot.tar"
        self.platform = self.root / "platform.tar.gz"
        self.squashfs.write_bytes(b"hsqs" + bytes(range(256)) * 4097)
        self.dockerfs.write_bytes(gzip.compress(tar_bytes({"image/config.json": b"{}"}), mtime=0))
        self.platform.write_bytes(gzip.compress(tar_bytes({"platform/config": b"VS"}), mtime=0))
        self.boot_files = {"boot/": b"", "boot/vmlinuz-test": b"kernel", "boot/initrd.img-test": b"initramfs"}
        self.boot.write_bytes(tar_bytes(self.boot_files))
        self.build()

    def build(self, separate_store=False, extra=None, omitted=()):
        files = dict(self.boot_files) | {"fs.squashfs": self.squashfs.read_bytes(),
                                        "platform.tar.gz": self.platform.read_bytes()}
        if not separate_store:
            files["dockerfs.tar.gz"] = self.dockerfs.read_bytes()
        files.update(extra or {})
        with zipfile.ZipFile(self.payload, "w", allowZip64=False) as archive:
            for name, data in files.items():
                if name in omitted:
                    continue
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = ((stat.S_IFDIR | 0o755) if name.endswith("/")
                                      else (stat.S_IFREG | 0o644)) << 16
                archive.writestr(info, data)
        self.wrap(separate_store)

    def wrap(self, separate_store=False):
        files = {
            "installer/": b"",
            "installer/fs.zip": self.payload.read_bytes(),
            "installer/install.sh": b'#!/bin/sh\narch="amd64"\nimage_version="test.1"\n',
            "installer/machine.conf": b"machine=vs\nplatform=x86_64-vs-r0\n",
            "installer/default_platform.conf": b"platform=vs\n",
            "installer/onie-image.conf": b"onie_image=vs\n",
            "installer/platforms_asic": b"x86_64-kvm_x86_64-r0\n",
        }
        if separate_store:
            files["installer/dockerfs.tar.gz"] = self.dockerfs.read_bytes()
        payload = tar_bytes(files)
        header = ("#!/bin/sh\npayload_sha1=" + hashlib.sha1(payload).hexdigest()
                  + "\npayload_image_size=" + str(len(payload)) + "\nexit_marker\n")
        self.installer.write_bytes(header.encode() + payload)

    def verify(self):
        return verify_image.verify(self.installer, self.payload, self.dockerfs,
                                   self.squashfs, self.boot, self.platform)

    def test_complete_streamed_byte_chain_and_separate_store(self):
        for separate in (False, True):
            with self.subTest(separate_store=separate):
                self.build(separate_store=separate)
                result = self.verify()
                self.assertEqual(result["installer"]["sha256"], hashlib.sha256(self.installer.read_bytes()).hexdigest())
                self.assertEqual(result["payload"]["kernel_versions"], ["test"])
                self.assertEqual(result["payload"]["dockerfs_in_zip"], not separate)
                self.assertEqual(result["archives"], {"dockerfs_members": 1, "platform_members": 1})

    def test_declared_input_mismatch_fails_even_with_valid_archive_checksums(self):
        self.squashfs.write_bytes(b"hsqs" + b"other filesystem")
        with self.assertRaisesRegex(ValueError, "ZIP bytes differ.*fs.squashfs"):
            self.verify()

    def test_boot_input_mismatch_fails(self):
        self.boot.write_bytes(tar_bytes(self.boot_files | {"boot/vmlinuz-test": b"new kernel"}))
        with self.assertRaisesRegex(ValueError, "boot members differ"):
            self.verify()

    def test_embedded_payload_must_match_declared_payload(self):
        original = self.installer.read_bytes()
        self.build(extra={"boot/config-test": b"new config"})
        self.installer.write_bytes(original)
        self.boot_files["boot/config-test"] = b"new config"
        self.boot.write_bytes(tar_bytes(self.boot_files))
        with self.assertRaisesRegex(ValueError, "ONIE bytes differ.*fs.zip"):
            self.verify()

    def test_onie_checksum_corruption_fails(self):
        data = bytearray(self.installer.read_bytes())
        data[-512] ^= 1
        self.installer.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "ONIE payload SHA1 mismatch"):
            self.verify()

    def test_onie_trailing_bytes_fail(self):
        with self.installer.open("ab") as stream:
            stream.write(b"uncovered trailer")
        with self.assertRaisesRegex(ValueError, "ONIE payload length mismatch"):
            self.verify()

    def test_zip_crc_corruption_fails(self):
        with zipfile.ZipFile(self.payload) as archive:
            info = archive.getinfo("fs.squashfs")
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        with self.payload.open("r+b") as stream:
            stream.seek(offset + 100)
            stream.write(b"corrupt")
        self.wrap()  # An intact ONIE SHA1 must not hide a corrupt ZIP member.
        with self.assertRaisesRegex(zipfile.BadZipFile, "Bad CRC-32"):
            self.verify()

    def test_missing_kernel_duplicate_and_unsafe_zip_paths_fail(self):
        for name in ("../escape", "/absolute", "other/file"):
            with self.subTest(name=name):
                self.build(extra={name: b"bad"})
                with self.assertRaises(ValueError):
                    self.verify()
        self.build(omitted={"boot/vmlinuz-test"})
        with self.assertRaisesRegex(ValueError, "kernel and initramfs"):
            self.verify()
        self.build()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.payload, "a") as archive:
                archive.writestr("fs.squashfs", b"duplicate")
        self.wrap()
        with self.assertRaisesRegex(ValueError, "duplicate ZIP"):
            self.verify()

    def test_gzip_footer_corruption_fails_even_after_tar_eof(self):
        data = bytearray(self.dockerfs.read_bytes())
        data[-8] ^= 1
        self.dockerfs.write_bytes(data)
        self.build()
        with self.assertRaises(gzip.BadGzipFile):
            self.verify()

    def test_cli_failure_retains_receipt(self):
        self.squashfs.write_bytes(b"invalid format")
        output = self.root / "receipts/verify.json"
        result = verify_image.main([
            "--installer", str(self.installer), "--payload", str(self.payload),
            "--dockerfs", str(self.dockerfs), "--squashfs", str(self.squashfs), "--output", str(output),
        ])
        self.assertEqual(result, 1)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "failed")
        self.assertIn("unexpected input format", report["error"])


if __name__ == "__main__":
    unittest.main()
