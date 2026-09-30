#!/usr/bin/env python3
"""Contract tests for the native-compatible ZIP and ONIE wrapper actions."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

import installer


ROOT = Path(__file__).resolve().parents[3]


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.squashfs = self.root / "fs.squashfs"
        self.squashfs.write_bytes(b"hsqs" + b"host filesystem" * 200)
        self.dockerfs = self.root / "dockerfs.tar.gz"
        self.dockerfs.write_bytes(gzip.compress(b"test docker store", mtime=0))
        self.platform = self.root / "platform.tar.gz"
        self.platform.write_bytes(gzip.compress(b"test platform payload", mtime=0))
        self.boot = self.root / "boot.tar"
        self.write_boot()
        self.payload = self.root / "fs.zip"
        self.files = self.root / "files.json"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"image_version": "bazel.unit-test"}))
        entries = []
        for name in ("install.sh", "sharch_body.sh", "default_platform.conf", "efi_sbatlevel.py"):
            entries.append({"path": name, "source": str(ROOT / "installer" / name)})
        entries.append({"path": "onie-image.conf", "source": str(ROOT / "onie-image.conf")})
        self.platforms = self.root / "platforms_asic"
        self.platforms.write_text("x86_64-kvm_x86_64-r0\n")
        entries.append({"path": "platforms_asic", "source": str(self.platforms)})
        self.files.write_text(json.dumps(entries))
        self.output = self.root / "sonic-vs.bin"

    def write_boot(self, extra=None):
        with tarfile.open(self.boot, "w") as archive:
            for name, content in [
                ("boot/vmlinuz-test", b"kernel" * 2000),
                ("boot/initrd.img-test", b"initramfs" * 2000),
            ] + (extra or []):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                info.mtime = 987654321
                archive.addfile(info, io.BytesIO(content))

    def build_payload(self):
        installer.create_payload(self.squashfs, self.dockerfs, self.boot, self.platform, self.payload)

    def build_onie(self):
        self.build_payload()
        installer.create_onie(self.payload, self.files, self.config, self.output, self.dockerfs)

    def read_wrapper(self):
        header, archive = self.output.read_bytes().split(b"\nexit_marker\n", 1)
        return header.decode(), archive

    def test_payload_members_content_and_reproducibility(self):
        self.build_payload()
        first = self.payload.read_bytes()
        with zipfile.ZipFile(self.payload) as archive:
            self.assertEqual(archive.testzip(), None)
            self.assertEqual(set(archive.namelist()), {
                "boot/vmlinuz-test", "boot/initrd.img-test", "platform.tar.gz",
                "fs.squashfs", "dockerfs.tar.gz",
            })
            self.assertEqual(archive.read("fs.squashfs"), self.squashfs.read_bytes())
            self.assertEqual(archive.read("dockerfs.tar.gz"), self.dockerfs.read_bytes())
            self.assertEqual(archive.read("platform.tar.gz"), self.platform.read_bytes())
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertLess(info.extract_version, 45)
                self.assertEqual(info.external_attr >> 16, 0o100644)
                if info.filename.endswith((".gz", ".squashfs")):
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
        os.utime(self.squashfs, (1777777777, 1777777777))
        self.build_payload()
        self.assertEqual(first, self.payload.read_bytes())

    def test_wrapper_checksum_size_permissions_and_native_script_render(self):
        entries = json.loads(self.files.read_text())
        long_name = "platforms/" + "platform-name-" * 12
        entries.append({"path": long_name, "source": str(self.platforms)})
        self.files.write_text(json.dumps(entries))
        self.build_onie()
        first = self.output.read_bytes()
        header, archive = self.read_wrapper()
        checksum = re.search(r"^payload_sha1=(\w+)", header, re.M).group(1)
        size = int(re.search(r"^payload_image_size=(\d+)$", header, re.M).group(1))
        self.assertEqual(hashlib.sha1(archive).hexdigest(), checksum)
        self.assertEqual(size, len(archive))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o755)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            self.assertEqual(bundle.extractfile("installer/" + long_name).read(), self.platforms.read_bytes())
            script = bundle.extractfile("installer/install.sh").read().decode()
            self.assertIn('image_version="bazel.unit-test"', script)
            self.assertIn('arch="amd64"', script)
            self.assertIn('ONIE_IMAGE_PART_SIZE="32768"', script)
            self.assertIn('demo_dev=$cur_wd/"target/sonic-vs.raw"', script)
            self.assertEqual(bundle.getmember("installer/install.sh").mode, 0o755)
            self.assertEqual(bundle.extractfile("installer/fs.zip").read(), self.payload.read_bytes())
            self.assertEqual(bundle.extractfile("installer/machine.conf").read(), b"machine=vs\nplatform=x86_64-vs-r0\n")
            for member in bundle.getmembers():
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
        installer.create_onie(self.payload, self.files, self.config, self.output, self.dockerfs)
        self.assertEqual(first, self.output.read_bytes())

    def test_real_native_shell_wrapper_extracts_and_rejects_corruption(self):
        self.build_onie()
        # Force the wrapper's ordinary-user extraction branch even under a root
        # test runner; this exercises the real wrapper without mounting tmpfs.
        commands = self.root / "commands"
        commands.mkdir()
        fake_id = commands / "id"
        fake_id.write_text("#!/bin/sh\necho 1000\n")
        fake_id.chmod(0o755)
        environment = dict(os.environ, extract="1", TMPDIR=str(self.root), PATH=str(commands) + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["sh", str(self.output)], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extracted = Path(re.search(r"Image extracted to: (.+)", result.stdout).group(1))
        self.assertEqual((extracted / "installer/fs.zip").read_bytes(), self.payload.read_bytes())
        shutil.rmtree(extracted)
        with self.output.open("r+b") as stream:
            stream.seek(-1024, os.SEEK_END)
            stream.write(b"corruption")
        result = subprocess.run(["sh", str(self.output)], env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to verify archive checksum", result.stdout)

    def test_separate_large_store_contract(self):
        self.build_payload()
        trimmed = self.root / "without-dockerfs.zip"
        with zipfile.ZipFile(self.payload) as source, zipfile.ZipFile(trimmed, "w") as dest:
            for member in source.infolist():
                if member.filename != "dockerfs.tar.gz":
                    dest.writestr(member, source.read(member))
        with self.assertRaisesRegex(ValueError, "separate dockerfs"):
            installer.create_onie(trimmed, self.files, self.config, self.output)
        installer.create_onie(trimmed, self.files, self.config, self.output, self.dockerfs)
        _, archive = self.read_wrapper()
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            self.assertEqual(bundle.extractfile("installer/dockerfs.tar.gz").read(), self.dockerfs.read_bytes())

    def test_bad_boot_paths_duplicates_and_missing_kernel_rejected(self):
        for name in ("../escape", "boot/../../escape", "/boot/absolute", "other/file", "boot/vmlinuz-test"):
            with self.subTest(name=name):
                self.write_boot([(name, b"invalid")])
                with self.assertRaises(ValueError):
                    self.build_payload()
                self.assertFalse(self.payload.exists())
        with tarfile.open(self.boot, "w"):
            pass
        with self.assertRaisesRegex(ValueError, "no kernel"):
            self.build_payload()

    def test_invalid_configuration_and_manifest_rejected(self):
        self.build_payload()
        for change in (
            {"arch": "arm64"}, {"secure_upgrade_mode": "prod"},
            {"image_version": 'bad"; command'}, {"partition_size": -1},
            {"raw_image": "../../disk"}, {"extra_cmdline": "bad\ncommand"},
        ):
            with self.subTest(change=change):
                config = {"image_version": "valid"}
                config.update(change)
                self.config.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    installer.create_onie(self.payload, self.files, self.config, self.output)
        self.config.write_text(json.dumps({"image_version": "valid"}))
        entries = json.loads(self.files.read_text())
        entries.append({"path": "../escape", "source": str(self.squashfs)})
        self.files.write_text(json.dumps(entries))
        with self.assertRaisesRegex(ValueError, "invalid archive path"):
            installer.create_onie(self.payload, self.files, self.config, self.output)

    def test_invalid_input_magic_and_partial_output_cleanup(self):
        self.squashfs.write_bytes(b"not a squashfs")
        with self.assertRaisesRegex(ValueError, "unexpected input format"):
            self.build_payload()
        self.assertFalse(self.payload.exists())

    def test_prepare_inputs_derives_single_asic_and_copies_platform_overrides(self):
        source = self.root / "source"
        for relative in (
            "installer/install.sh", "installer/sharch_body.sh", "installer/default_platform.conf",
            "installer/efi_sbatlevel.py", "onie-image.conf", "platform/vs/platform.conf",
        ):
            dest = source / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, dest)
        declaration = source / "platform/vs/platform-modules-vs.mk"
        declaration.write_text("$(VS_PLATFORM_MODULE)_PLATFORM = x86_64-kvm_x86_64-r0\n")
        device = source / "device/virtual/x86_64-kvm_x86_64_4_asic-r0"
        device.mkdir(parents=True)
        (device / "platform_asic").write_text("vs\n")
        (device / "installer.conf").write_text("CONSOLE_SPEED=115200\n")
        (device / "installer.conf.override").write_text("VAR_LOG_SIZE=1024\n")
        bundle = self.root / "bundle"
        installer.prepare_inputs(source, self.config, bundle)
        self.assertEqual((bundle / "files/platforms_asic").read_text().splitlines(), [
            "x86_64-kvm_x86_64-r0", "x86_64-kvm_x86_64_4_asic-r0",
        ])
        self.assertEqual((bundle / "files/platforms/x86_64-kvm_x86_64_4_asic-r0.override").read_text(), "VAR_LOG_SIZE=1024\n")
        self.assertEqual(json.loads((bundle / "provenance.json").read_text())["platform_override_files"], 2)
        manifest = json.loads((bundle / "files-manifest.json").read_text())
        for entry in manifest:
            self.assertTrue((bundle / entry["source"]).is_file())
        self.assertEqual((bundle / "files/install.sh").stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
