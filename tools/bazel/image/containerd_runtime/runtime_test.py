#!/usr/bin/env python3
"""Check Docker/containerd settings and lossless store extraction wiring."""

import json
from pathlib import Path
import tempfile
import unittest

import overlay


class OverlayTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.unit = self.root / "etc/systemd/system/docker.service.d/docker.service.conf"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Service]\nEnvironment=GODEBUG=netdns=cgo\nExecStart=\n"
                             "ExecStart=/usr/bin/dockerd -H fd:// --storage-driver=overlay2 "
                             "--bip=240.127.1.1/24 --iptables=false --ipv6=true\n")
        self.config = self.root / "etc/docker/daemon.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"log-driver": "json-file", "log-opts": {"max-size": "10m"},
                                          "features": {"buildkit": False}}))
        self.initramfs = self.root / "etc/initramfs-tools/scripts/init-bottom/union-mount"
        self.initramfs.parent.mkdir(parents=True)
        self.initramfs.write_text('''#!/bin/sh -e
extract_dockerfs()
{
    pzstd -d -q ${rootmnt}/host/$image_dir/dockerfs.tar.gz -c | tar x --numeric-owner -C ${rootmnt}/var/lib/docker
    tar xz --numeric-owner -f "${rootmnt}/host/$image_dir/dockerfs.tar.gz" -C "${rootmnt}/var/lib/docker"
    unzip -qp "$swi_path" dockerfs.tar.gz | tar xz --numeric-owner -C ${rootmnt}/var/lib/docker
}
''')

    def test_preserves_settings_and_orders_containerd_before_docker(self):
        original = self.unit.read_bytes()
        config = self.root / "etc/containerd/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("# unrelated original configuration\n")
        overlay.install(self.root, "amd64")
        self.assertEqual(self.unit.read_bytes(), original)
        self.assertEqual(config.read_text(), "# unrelated original configuration\n")
        docker = json.loads(self.config.read_text())
        self.assertEqual(docker["features"], {"buildkit": False, "containerd-snapshotter": True})
        self.assertEqual(docker["log-opts"], {"max-size": "10m"})
        self.assertEqual(docker["storage-driver"], "overlayfs")
        self.assertEqual(docker["containerd-namespace"], "moby")
        unit = (self.unit.parent / "zz-sonic-bazel-containerd.conf").read_text()
        self.assertIn("Requires=containerd.service\n", unit)
        self.assertIn("After=containerd.service\n", unit)
        self.assertIn("-H fd:// --bip=240.127.1.1/24 --iptables=false --ipv6=true", unit)
        self.assertNotIn("overlay2", unit)
        containerd = (self.root / "etc/containerd/sonic-bazel.toml").read_text()
        self.assertIn('root = "/var/lib/docker/containerd-store"', containerd)
        self.assertFalse((self.root / "etc/systemd/system/sonic-oci-import.service").exists())
        self.assertFalse((self.root / "usr/lib/sonic/sonic-oci-import.py").exists())
        script = self.initramfs.read_text()
        self.assertIn('chroot "${rootmnt}" /usr/bin/tar --extract', script)
        self.assertIn("--xattrs --xattrs-include='*' --acls", script)
        self.assertIn("--use-compress-program=/usr/bin/zstd", script)
        self.assertEqual(script.count('sonic_bazel_extract_containerd --file "/host/$image_dir/dockerfs.tar.gz"'), 2)
        self.assertIn("sonic_bazel_extract_containerd --file=-", script)
        self.assertNotIn("--gzip", script)
        self.assertNotIn("pzstd -d", script)
        self.assertNotIn("| tar ", script)

    def test_arm64_and_repeat_application_are_stable(self):
        paths = overlay.install(self.root, "arm64")
        first = {path: (self.root / path).read_bytes() for path in paths}
        overlay.install(self.root, "arm64")
        self.assertEqual(first, {path: (self.root / path).read_bytes() for path in paths})
        self.assertEqual(len(first), 5)

    def test_changed_initramfs_contract_is_rejected_before_writes(self):
        self.initramfs.write_text("#!/bin/sh\ntar xzf /different-store.tar.gz\n")
        with self.assertRaisesRegex(ValueError, "initramfs dockerfs extraction contract"):
            overlay.install(self.root, "amd64")
        self.assertFalse((self.root / "etc/containerd/sonic-bazel.toml").exists())

    def test_unsupported_docker_modes_fail_without_writes(self):
        for value in ({"userns-remap": "default"}, {"storage-opts": ["overlay2.size=10G"]}):
            with self.subTest(value=value):
                self.config.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    overlay.install(self.root, "amd64")
                self.assertEqual(json.loads(self.config.read_text()), value)
                self.assertFalse((self.root / "etc/containerd/sonic-bazel.toml").exists())

    def test_missing_sonic_execstart_is_not_guessed(self):
        self.unit.write_text("[Service]\nExecStart=\n")
        with self.assertRaisesRegex(ValueError, "one /usr/bin/dockerd"):
            overlay.install(self.root, "amd64")

    def test_split_options_and_socket_activation_are_preserved(self):
        command = overlay.docker_command("[Service]\nExecStart=/usr/bin/dockerd -H fd:// "
            "--storage-driver overlay2 --containerd=/old/socket --containerd-namespace old --iptables=false\n")
        self.assertEqual(command, "/usr/bin/dockerd -H fd:// --iptables=false")

    def test_output_symlink_cannot_escape_restored_root(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "etc/containerd").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes"):
                overlay.install(self.root, "amd64")
            self.assertFalse((Path(outside) / "sonic-bazel.toml").exists())


if __name__ == "__main__":
    unittest.main()
