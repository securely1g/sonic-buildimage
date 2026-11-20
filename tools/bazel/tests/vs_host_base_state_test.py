#!/usr/bin/env python3
"""Cover the native Make entry and the cold-base personalization boundary."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/vs_host_base_state.sh"
TARGET = "sonic-vs.bin__vs__rfs.squashfs"
ABI = "6.12.41+deb13-sonic-amd64"


class HostBaseStateTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.work = Path(workspace.name)
        buildinfo = self.work / "src/sonic-build-hooks/buildinfo"
        (buildinfo / "config").mkdir(parents=True)
        (buildinfo / "config/buildinfo.config").write_text("export DISTRO=trixie\n")
        (buildinfo / "sonic-build-hooks_1.0_all.deb").touch()
        self.env = dict(
            PATH=os.defpath, USERNAME="admin", TARGET_MACHINE="vs", IMAGE_TYPE="onie",
            CONFIGURED_PLATFORM="vs", CONFIGURED_ARCH="amd64", IMAGE_DISTRO="trixie",
            TARGET_BOOTLOADER="grub", RFS_SPLIT_FIRST_STAGE="y", RFS_SPLIT_LAST_STAGE="n",
            RFS_SQUASHFS_NAME=TARGET, MIRROR_SNAPSHOT="y",
            BUILD_SNAPSHOT_URL="http://packages.trafficmanager.net/snapshot",
            BUILD_PUBLIC_URL="https://packages.trafficmanager.net/public",
            BUILD_PACKAGES_URL="https://packages.trafficmanager.net/packages",
            TRUSTED_GPG_URLS="https://packages.microsoft.com/keys/microsoft.asc",
        )

    def run_state(self, *args, **settings):
        return subprocess.run([SCRIPT, *args], cwd=self.work, env=self.env | settings,
                              capture_output=True, text=True)

    def test_stock_inputs_and_rejection_of_private_settings(self):
        self.assertEqual(self.run_state("check-inputs").returncode, 0)
        for name, value in [("PASSWORD", "fixture-secret"), ("USERNAME", "siteadmin"),
                            ("BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD", "fixture-secret"),
                            ("IMAGE_TYPE", "kvm"), ("MIRROR_URLS", "https://site.example")]:
            with self.subTest(name=name):
                result = self.run_state("check-inputs", **{name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("fixture-secret", result.stderr)
        hook = self.work / "files/build_templates/build_debian.organization.sh"
        hook.parent.mkdir(parents=True)
        hook.touch()
        self.assertNotEqual(self.run_state("check-inputs").returncode, 0)

    def test_native_make_recipe_launches_with_declared_inputs_only(self):
        slave = (ROOT / "slave.mk").read_text()
        start = slave.index("\t\t$(if $(vs_host_base_trial),env -u PASSWORD")
        end = slave.index("./build_debian.sh $(LOG)", start) + len("./build_debian.sh $(LOG)")
        recipe = slave[start:end]
        makefile = self.work / "Makefile"
        makefile.write_text("SHELL := /bin/bash\n.ONESHELL:\n"
                            "USERNAME := admin\nCHANGE_DEFAULT_PASSWORD := y\n"
                            "BMC_NOS_ACCOUNT_USERNAME := fixture-bmc\n%: FORCE\n" + recipe
                            + "\nFORCE:\n")
        driver = self.work / "tools/bazel/ci/vs_host_base.py"
        driver.parent.mkdir(parents=True)
        driver.write_text("import os, sys\nassert sys.argv[1:] == ['--','./build_debian.sh']\n"
                          "assert os.environ['SONIC_VS_UNPERSONALIZED_BASE'] == 'y'\n"
                          "for key in ('PASSWORD','CHANGE_DEFAULT_PASSWORD','BMC_NOS_ACCOUNT_USERNAME',"
                          "'BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD','MAKEFLAGS'):\n"
                          "    assert key not in os.environ\n")
        native = self.work / "build_debian.sh"
        native.write_text("#!/bin/sh\n"
                          'test "$PASSWORD:$BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD:$CHANGE_DEFAULT_PASSWORD:$BMC_NOS_ACCOUNT_USERNAME"'
                          ' = fixture-user:fixture-bmc-password:y:fixture-bmc\n')
        native.chmod(0o755)
        for select in ("", "y"):
            with self.subTest(select=select):
                result = subprocess.run(["make", "-s", "-f", str(makefile), TARGET,
                                         "vs_host_base_trial=" + select], cwd=self.work,
                                        env=self.env | dict(PASSWORD="fixture-user",
                                                            BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD="fixture-bmc-password"),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_seal_rejects_missing_kernel_and_nonlocked_accounts(self):
        fs = self.work / "fsroot-vs"

        def write(path, data):
            target = fs / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data)

        fs.mkdir()
        self.assertIn("incomplete kernel", self.run_state("seal", fs, ABI).stderr)
        for name in ["boot/vmlinuz-" + ABI, "boot/initrd.img-" + ABI, "usr/lib/modules/" + ABI + "/modules.dep"]:
            write(name, "fixture\n")
        write("etc/shadow", "root:*:::::::\nadmin:$fixture:::::::\n")
        (fs / "etc/sonic").mkdir()
        binary = self.work / "bin"
        binary.mkdir()
        for name, content in [
            ("chroot", 'test "$2" = modinfo'),
            ("install", 'if [ "$1" = "-o" ]; then shift 4; fi\nexec /usr/bin/install "$@"'),
        ]:
            command = binary / name
            command.write_text("#!/bin/sh\n" + content + "\n")
            command.chmod(0o755)
        selected = dict(PATH=str(binary) + ":" + os.defpath)
        denied = self.run_state("seal", fs, ABI, **selected)
        self.assertIn("password is set", denied.stderr)
        self.assertNotIn("$fixture", denied.stderr)
        (fs / "etc/sonic/.vs-host-base-v1").unlink()
        write("etc/shadow", "root:*:::::::\nadmin:!:::::::\n")
        write("etc/resolv.conf", "private worker resolver")
        valid = self.run_state("seal", fs, ABI, **selected)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertFalse((fs / "etc/resolv.conf").exists())
        restored = self.run_state("restore", fs, ABI, **selected)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((fs / "etc/resolv.conf").read_bytes(), Path("/etc/resolv.conf").read_bytes())
        self.assertFalse((fs / "etc/sonic/.vs-host-base-v1").exists())


if __name__ == "__main__":
    unittest.main()
