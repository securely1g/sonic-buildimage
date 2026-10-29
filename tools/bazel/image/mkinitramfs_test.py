"""Hook expansion and rooted staging regressions; no image tools executed."""
import io
import hashlib
import json
import lzma
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from mkinitramfs import compress_initrd, Mkinitramfs, Rootfs, gnu_tsort, script_prereqs, read_kernel_metadata, build_initrd


class InitrdTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'
        self.root.mkdir()
        self.fs = Rootfs(str(self.root))

    def test_rooted_absolute_links_and_merged_usr(self):
        (self.root / 'usr/lib').mkdir(parents=True)
        (self.root / 'lib').symlink_to('/usr/lib')
        (self.root / 'usr/lib/actual').write_bytes(b'rootfs bytes')
        (self.root / 'usr/lib/link').symlink_to('/lib/actual')
        self.assertEqual(self.fs.read('/lib/link'), b'rootfs bytes')
        self.assertEqual(self.fs.canon('/../../lib/link'), '/usr/lib/actual')
        self.assertFalse(self.fs.exists('/etc/passwd'))
        (self.root / 'usr/lib/loop').symlink_to('/lib/loop')
        self.assertIsNone(self.fs.canon('/lib/loop'))

    def test_layer_writes_follow_image_links_not_host_links(self):
        layer = Path(self.temp.name) / 'layer.tar'
        with tarfile.open(layer, 'w') as archive:
            for name, kind, target, data in (
                ('usr/lib', tarfile.DIRTYPE, '', None),
                ('lib', tarfile.SYMTYPE, '/usr/lib', None),
                ('lib/staged', tarfile.REGTYPE, '', b'new bytes'),
            ):
                member = tarfile.TarInfo(name)
                member.type, member.linkname, member.mode = kind, target, 0o755
                member.size = len(data) if data else 0
                archive.addfile(member, io.BytesIO(data) if data else None)
        self.fs.apply_layer(layer)
        self.assertEqual((self.root / 'usr/lib/staged').read_bytes(), b'new bytes')
        self.assertEqual(self.fs.rmeta('/usr/lib/staged')[0], stat.S_IFREG | 0o755)

    def test_script_prereqs_and_tsort(self):
        script = 'PREREQ="udev"\nprereqs() { echo "$PREREQ"; }\ncase "$1" in\nprereqs) prereqs\nexit 0;;\nesac\n'
        self.assertEqual(script_prereqs(script, 'synthetic-hook'), 'udev')
        self.assertEqual(gnu_tsort([('udev', 'dmsetup'), ('dmsetup', 'dmsetup'), ('zz-busybox', 'klibc-utils')]),
                         ['udev', 'zz-busybox', 'dmsetup', 'klibc-utils'])
        with self.assertRaisesRegex(ValueError, 'loop'):
            gnu_tsort([('a', 'b'), ('b', 'a')])
        with self.assertRaisesRegex(ValueError, 'cannot determine'):
            script_prereqs('case "$1" in\nprereqs) /bin/target-tool;;\nesac', 'unknown')

    def test_xz_module_decompression_preserves_signed_bytes_and_mode(self):
        modules = self.root / 'modules'
        modules.mkdir()
        payload = b'module ELF bytes\x00~Module signature appended~'
        source = modules / 'virtio.ko.xz'
        source.write_bytes(lzma.compress(payload))
        source.chmod(0o644)
        mk = object.__new__(Mkinitramfs)
        mk.dmeta = {}
        mk.decompress_modules(str(modules))
        output = modules / 'virtio.ko'
        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertFalse(source.exists())
        self.assertEqual(mk.dmeta[output.stat().st_ino][0], stat.S_IFREG | 0o644)

    def test_unknown_hook_is_rejected_before_any_execution(self):
        layer = Path(self.temp.name) / 'unknown-hook.tar'
        content = b'#!/bin/sh\necho unreviewed\n'
        with tarfile.open(layer, 'w') as archive:
            member = tarfile.TarInfo('etc/initramfs-tools/hooks/unreviewed-hook')
            member.mode, member.size = 0o755, len(content)
            archive.addfile(member, io.BytesIO(content))
        self.fs.apply_layer(layer)
        mk = object.__new__(Mkinitramfs)
        mk.r, mk.hooks_run = self.fs, []
        with self.assertRaisesRegex(ValueError, 'unpinned initramfs hook'):
            mk.run_hooks('/etc/initramfs-tools/hooks')
        self.assertEqual(mk.hooks_run, [])

    def test_trixie_udev_copies_dlopen_library_hwdb_and_merges_link_dropins(self):
        # udev 257's installed hook explicitly requires these resources even
        # when libkmod is absent from systemd-udevd's DT_NEEDED entries.
        files = {
            'usr/lib/systemd/systemd-udevd': b'daemon',
            'usr/bin/udevadm': b'udevadm',
            'usr/lib/x86_64-linux-gnu/libkmod.so.2': b'dlopen library',
            'usr/lib/udev/ata_id': b'ata helper',
            'usr/lib/udev/scsi_id': b'scsi helper',
            'usr/sbin/blkid': b'blkid',
            'usr/lib/udev/hwdb.bin': b'compiled hardware database',
            'usr/lib/systemd/network/10-test.link.d/package.conf': b'package',
            'etc/systemd/network/10-test.link.d/local.conf': b'local',
            'usr/lib/udev/rules.d/60-persistent-storage.rules': b'package rule',
            'etc/udev/rules.d/60-persistent-storage.rules': b'override rule',
            'etc/udev/rules.d/70-local.rules': b'custom rule',
        }
        layer = Path(self.temp.name) / 'udev.tar'
        with tarfile.open(layer, 'w') as archive:
            for path, data in files.items():
                member = tarfile.TarInfo(path)
                member.mode, member.size = 0o644, len(data)
                archive.addfile(member, io.BytesIO(data))
        self.fs.apply_layer(layer)
        (self.root / 'sbin').symlink_to('usr/sbin')
        (self.root / 'staged/usr/sbin').mkdir(parents=True)
        (self.root / 'staged/sbin').symlink_to('usr/sbin')
        mk = object.__new__(Mkinitramfs)
        mk.r, mk.D, mk.dmeta, mk.log = self.fs, '/staged', {}, []
        mk.hook_udev()
        out = self.root / 'staged'
        for path in ('usr/lib/x86_64-linux-gnu/libkmod.so.2', 'usr/lib/udev/hwdb.bin',
                     'usr/lib/systemd/network/10-test.link.d/package.conf'):
            self.assertEqual((out / path).read_bytes(), files[path])
        self.assertEqual((out / 'usr/lib/systemd/network/10-test.link.d/local.conf').read_bytes(), b'local')
        self.assertEqual((out / 'usr/lib/udev/rules.d/60-persistent-storage.rules').read_bytes(), b'override rule')
        self.assertEqual((out / 'usr/lib/udev/rules.d/70-local.rules').read_bytes(), b'custom rule')

    def test_kernel_release_comes_from_producer_metadata(self):
        metadata = Path(self.temp.name) / 'kernel.json'
        metadata.write_text(json.dumps({'schema': 1, 'architecture': 'arm64',
                                       'kernel_release': 'different-release+vendor.7'}))
        parsed = read_kernel_metadata(metadata)
        self.assertEqual(parsed['kernel_release'], 'different-release+vendor.7')
        self.assertEqual(parsed['architecture'], 'arm64')
        metadata.write_text(json.dumps({'schema': 1, 'architecture': 'arm64',
                                       'kernel_release': '../../host'}))
        with self.assertRaisesRegex(ValueError, 'invalid kernel release'):
            read_kernel_metadata(metadata)

    def test_rootfs_boot_bytes_and_module_directory_must_match_the_producer(self):
        layer = Path(self.temp.name) / 'kernel.tar'
        content = b'kernel from a different build'
        with tarfile.open(layer, 'w') as archive:
            member = tarfile.TarInfo('boot/vmlinuz-vendor-release')
            member.mode, member.size = 0o644, len(content)
            archive.addfile(member, io.BytesIO(content))
        metadata = Path(self.temp.name) / 'producer.json'
        value = {'schema': 1, 'architecture': 'amd64', 'kernel_release': 'vendor-release',
                 'module_directory': 'usr/lib/modules/vendor-release',
                 'boot': {'vmlinuz': {'path': 'boot/vmlinuz-vendor-release', 'sha256': '0' * 64}}}
        metadata.write_text(json.dumps(value))
        spec = {'layers': [str(layer)], 'kernel_metadata': str(metadata)}
        with self.assertRaisesRegex(ValueError, 'boot file differs'):
            build_initrd(spec, str(self.root / 'initrd'), str(self.root / 'audit.json'))
        value['boot']['vmlinuz']['sha256'] = hashlib.sha256(content).hexdigest()
        metadata.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'lacks matching producer module directory'):
            build_initrd(spec, str(self.root / 'initrd'), str(self.root / 'audit.json'))

    def test_busybox_source_identity_ignores_only_binary_rebuild_suffix(self):
        (self.root / 'var/lib/dpkg').mkdir(parents=True)
        (self.root / 'staged/bin').mkdir(parents=True)
        status = self.root / 'var/lib/dpkg/status'
        binary = self.root / 'staged/bin/busybox'
        mk = object.__new__(Mkinitramfs)
        mk.r, mk.D, mk.conf = self.fs, '/staged', {}
        mk.busybox_manifest = {'version': '1:1.37.0-6', 'applets': ['bin/cat']}
        for version, valid in (
            ('1:1.37.0-6', True),
            ('1:1.37.0-6+b1', True),
            ('1:1.37.0-6+b12', True),
            ('1:1.37.0-7+b1', False),
            ('1:1.37.0-6+b1extra', False),
        ):
            with self.subTest(version=version):
                status.write_text('Package: busybox\nVersion: ' + version + '\n\n')
                with patch.object(mk, 'copy_exec', side_effect=lambda *args: binary.write_bytes(b'busybox')):
                    if valid:
                        mk.hook_zz_busybox()
                        alias = self.root / 'staged/bin/cat'
                        self.assertEqual(alias.stat().st_ino, binary.stat().st_ino)
                        alias.unlink()
                    else:
                        with self.assertRaisesRegex(ValueError, 'deb/source version mismatch'):
                            mk.hook_zz_busybox()


class CompressionContractTest(unittest.TestCase):
    def test_zstd_module_requires_explicit_decoder(self):
        mk = object.__new__(Mkinitramfs)
        mk.zstd = None
        with self.assertRaisesRegex(ValueError, 'declared zstd executable'):
            mk.module_bytes('module.ko.zst')

    def test_configured_gzip_and_xz_decode_original_cpio(self):
        import gzip
        import lzma
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.cpio"
            source.write_bytes(b"070701" + b"cpio payload\0" * 200)
            for kind, decode in [("gzip", gzip.decompress), ("xz", lzma.decompress)]:
                for level in [1, 9]:
                    destination = Path(directory) / (kind + str(level))
                    compress_initrd(source, destination, {"compression": kind, "compression_level": level})
                    self.assertEqual(decode(destination.read_bytes()), source.read_bytes())


if __name__ == '__main__':
    unittest.main()
