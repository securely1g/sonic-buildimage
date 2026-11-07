"""Exercise host-rendered APT configuration before rootfs Python/CA installation."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/native_rootfs_apt.py"
SPEC = importlib.util.spec_from_file_location("native_rootfs_apt", HELPER)
rootfs_apt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rootfs_apt)
STAMPS = {"debian": "20260727T143429Z", "debian-security": "20260726T121236Z"}


class NativeRootfsAptTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-rootfs-apt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.apt = self.root / "etc/apt"
        (self.apt / "apt.conf.d").mkdir(parents=True)
        (self.apt / "sources.list.d").mkdir()
        (self.root / "var/lib").mkdir(parents=True)
        (self.root / "usr/share").mkdir(parents=True)
        environment = mock.patch.dict(os.environ, http_proxy="http://192.168.240.1:12345", https_proxy="")
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.apt / "sources.list"
        self.source.write_text(
            "deb [signed-by=/keys/debian.gpg] http://deb.debian.org/debian trixie main contrib non-free-firmware\n"
            "deb-src http://deb.debian.org/debian trixie-updates main contrib non-free-firmware\n"
            "deb http://deb.debian.org/debian trixie-backports main contrib non-free-firmware\n"
            "deb-src http://deb.debian.org/debian-security trixie-security main contrib non-free-firmware\n")
        self.source.chmod(0o600)
        self.original = self.source.read_bytes()

    def test_real_config_keeps_original_and_later_sourceparts_and_cleans_every_stage(self):
        for stage in ("first", "last"):
            with self.subTest(stage=stage):
                os.environ["http_proxy"] = "http://192.168.240.1:" + ("12345" if stage == "first" else "54321")
                previous_umask = os.umask(0o077)
                try:
                    rootfs_apt.setup(self.root, STAMPS)
                finally:
                    os.umask(previous_umask)
                view = self.root / rootfs_apt.VIEW
                content = (view / "sources.list").read_text()
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertEqual(view.stat().st_mode & 0o777, 0o700)
                self.assertEqual((view / "sources.list").stat().st_mode & 0o777, 0o600)
                self.assertEqual((self.root / rootfs_apt.TRUST).stat().st_mode & 0o777, 0o755)
                self.assertIn("signed-by=/keys/debian.gpg", content)
                self.assertIn("http://deb.debian.org/debian trixie main", content)
                self.assertIn("20260727T143429Z/ trixie-backports main contrib non-free-firmware", content)
                self.assertIn("20260726T121236Z/ trixie-security contrib non-free-firmware", content)
                self.assertNotIn("trusted=", content)
                self.assertNotIn("check-valid-until=", content)
                vendor = self.apt / "sources.list.d/docker.list"
                vendor.write_text("deb https://download.docker.com/linux/debian trixie stable\n")
                result = subprocess.run([
                    "apt-config", "-o", "Dir::Etc::sourceparts=" + str(vendor.parent),
                    "-c", str(self.root / rootfs_apt.CONFIG), "shell",
                    "source", "Dir::Etc::sourcelist", "parts", "Dir::Etc::sourceparts",
                    "trust", "Acquire::https::snapshot.debian.org::CaInfo",
                    "global_trust", "Acquire::https::CaInfo",
                    "proxy", "Acquire::http::Proxy", "https_proxy", "Acquire::https::Proxy",
                ], text=True, capture_output=True, check=True)
                self.assertIn("source='/var/lib/sonic-native-apt/sources.list'", result.stdout)
                self.assertIn("parts='" + str(vendor.parent) + "'", result.stdout)
                self.assertIn("trust='/usr/share/sonic-native-apt/ca-certificates.crt'", result.stdout)
                self.assertIn("proxy='" + os.environ["http_proxy"] + "'", result.stdout)
                self.assertIn("https_proxy='DIRECT'", result.stdout)
                self.assertNotIn("global_trust=", result.stdout)
                rootfs_apt.cleanup(self.root)
                rootfs_apt.cleanup(self.root)
                self.assertFalse(view.exists())
                self.assertFalse((self.root / rootfs_apt.TRUST).exists())
                self.assertFalse((self.root / rootfs_apt.CONFIG).exists())
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertIn("download.docker.com", vendor.read_text())

    def test_trust_is_only_enabled_distro_certificates(self):
        reads = []
        certificate = next(Path("/usr/share/ca-certificates/mozilla").glob("*.crt")).read_text()
        def read(path, *args, **kwargs):
            reads.append(str(path))
            if path == Path("/etc/ca-certificates.conf"):
                return "# comment\n!mozilla/Disabled.crt\nmozilla/Enabled.crt\nlocal/Private.crt\nmozilla/../Escape.crt\n"
            self.assertEqual(path, Path("/usr/share/ca-certificates/mozilla/Enabled.crt"))
            return certificate
        with mock.patch.object(Path, "read_text", read), mock.patch.object(Path, "is_symlink", return_value=False), \
                mock.patch.object(Path, "is_file", return_value=True):
            bundle = rootfs_apt.distro_ca_bundle()
        self.assertEqual(bundle, certificate.strip() + "\n")
        self.assertEqual(len(reads), 2)

    def test_missing_dates_and_existing_config_fail_before_changes(self):
        with self.assertRaises(ValueError):
            rootfs_apt.setup(self.root, {"debian": STAMPS["debian"]})
        self.assertFalse((self.root / rootfs_apt.VIEW).exists())
        config = self.root / rootfs_apt.CONFIG
        config.write_text("pre-existing configuration\n")
        with self.assertRaises(ValueError):
            rootfs_apt.setup(self.root, STAMPS)
        with self.assertRaises(ValueError):
            rootfs_apt.cleanup(self.root)
        self.assertEqual(config.read_text(), "pre-existing configuration\n")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_missing_current_proxy_fails_before_changes(self):
        for proxy in ("", "http://192.168.240.2:12345", "http://user:password@192.168.240.1:12345",
                      "http://192.168.240.1:12345/path", "http://192.168.240.1:12345?query"):
            with self.subTest(proxy=proxy), mock.patch.dict(os.environ, http_proxy=proxy), self.assertRaises(ValueError):
                rootfs_apt.setup(self.root, STAMPS)
            self.assertFalse((self.root / rootfs_apt.VIEW).exists())

    def test_cleanup_refuses_unowned_directories(self):
        for relative in (rootfs_apt.VIEW, rootfs_apt.TRUST):
            with self.subTest(relative=relative):
                directory = self.root / relative
                directory.mkdir()
                retained = directory / "unrelated"
                retained.write_text("keep\n")
                with self.assertRaises(ValueError):
                    rootfs_apt.cleanup(self.root)
                self.assertEqual(retained.read_text(), "keep\n")
                retained.unlink()
                directory.rmdir()

    def test_actual_shell_functions_are_opt_in_and_cleanup_on_failure(self):
        source = (ROOT / "build_debian.sh").read_text()
        functions = source.split("native_rootfs_apt_setup()", 1)[1].split("## Read ONIE", 1)[0]
        functions = "native_rootfs_apt_setup()" + functions
        script = ("set -e\nsudo() { if [[ $1 == --preserve-env=* ]]; then shift; fi; \"$@\"; }\n"
                  "trap_push() { trap \"$1\" EXIT; }\n" + functions +
                  '\nnative_rootfs_apt_setup\ntest -f "$FILESYSTEM_ROOT/etc/apt/apt.conf.d/99sonic-native-apt"\nexit 23\n')
        env = dict(os.environ, FILESYSTEM_ROOT=str(self.root), SONIC_NATIVE_PRIVATE_APT="1",
                   SONIC_NATIVE_DEBIAN_SNAPSHOT=STAMPS["debian"],
                   SONIC_NATIVE_DEBIAN_SECURITY_SNAPSHOT=STAMPS["debian-security"])
        result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertFalse((self.root / rootfs_apt.VIEW).exists())
        self.assertFalse((self.root / rootfs_apt.CONFIG).exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        result = subprocess.run(["bash", "-c", functions + "\nnative_rootfs_apt_setup\nnative_rootfs_apt_cleanup\n"],
                                cwd=ROOT, env=dict(env, SONIC_NATIVE_PRIVATE_APT=""), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / rootfs_apt.VIEW).exists())


if __name__ == "__main__":
    unittest.main()
