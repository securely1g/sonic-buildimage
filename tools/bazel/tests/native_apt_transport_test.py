"""Native APT transport preserves sources, authentication, and command behavior."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "src/sonic-build-hooks/scripts/native_apt_transport.py"
SPEC = importlib.util.spec_from_file_location("native_apt_transport", HELPER)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
STAMP = "20260727T143429Z"
SECURITY_STAMP = "20260726T121236Z"
STAMPS = {"debian": STAMP, "debian-security": SECURITY_STAMP}
SNAPSHOT = "https://snapshot.debian.org/archive/debian/" + STAMP + "/"


class NativeAptTransportTest(unittest.TestCase):
    def test_list_keeps_suites_keys_and_unrelated_https(self):
        original = (
            "# keep original runtime sources\n"
            "deb [arch=amd64 signed-by=/keys/debian.gpg] https://deb.debian.org/debian trixie main\n"
            "deb-src https://deb.debian.org/debian trixie-updates main\n"
            "deb [arch=amd64] https://deb.debian.org/debian trixie-backports main contrib\n"
            "deb https://deb.debian.org/debian-security trixie-security main non-free-firmware\n"
            "deb https://vendor.example.invalid/repo trixie main\n")
        view = native.source_view(original, ".list", STAMPS)
        self.assertIn("signed-by=/keys/debian.gpg] http://deb.debian.org/debian trixie", view)
        self.assertIn("deb-src http://deb.debian.org/debian trixie-updates", view)
        self.assertIn("deb [arch=amd64] " + SNAPSHOT + " trixie-backports main contrib", view)
        self.assertIn("http://deb.debian.org/debian-security trixie-security", view)
        self.assertIn("https://snapshot.debian.org/archive/debian-security/" + SECURITY_STAMP
                      + "/ trixie-security non-free-firmware", view)
        self.assertIn("https://vendor.example.invalid/repo", view)
        self.assertNotIn("trusted=", view)
        self.assertNotIn("check-valid-until=", view)

    def test_deb822_splits_backports_without_losing_signed_by_or_other_suites(self):
        original = ("Types: deb deb-src\nURIs: https://deb.debian.org/debian\n"
                    "Suites: trixie trixie-updates\n trixie-backports\nComponents: main contrib\n"
                    "Signed-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n signed-key\n"
                    " -----END PGP PUBLIC KEY BLOCK-----\n")
        view = native.source_view(original, ".sources", STAMPS)
        self.assertIn("URIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main", view)
        self.assertIn("URIs: http://deb.debian.org/debian\nSuites: trixie-updates\nComponents: main", view)
        self.assertIn("URIs: " + SNAPSHOT + "\nSuites: trixie-backports", view)
        self.assertEqual(view.count("Types: deb deb-src"), 5)
        self.assertEqual(view.count(" signed-key\n"), 5)

    @unittest.skipUnless(shutil.which("apt-config"), "requires apt-config, no network")
    def test_real_config_subprocess_fresh_views_cleanup_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parts = root / "sources.list.d"
            parts.mkdir()
            sources = root / "sources.list"
            sources.write_text("deb https://deb.debian.org/debian trixie main\n")
            config = root / "apt.conf"
            config.write_text(f'Dir::Etc::sourcelist "{sources}";\nDir::Etc::sourceparts "{parts}";\n')
            receipt = root / "receipt.json"
            command = root / "apt-get"
            command.write_text("#!" + sys.executable + "\n" +
                "import json, os, pathlib, sys\n"
                "args=sys.argv[1:]; options={}\n"
                "for i, arg in enumerate(args):\n"
                " if arg == '-o':\n"
                "  key, value=args[i+1].split('=',1); options[key]=value\n"
                "source=pathlib.Path(options['Dir::Etc::sourcelist'])\n"
                "parts=pathlib.Path(options['Dir::Etc::sourceparts'])\n"
                "data={'args':args, 'source':source.read_text(), 'view':str(source.parent),\n"
                " 'parts':{p.name:p.read_text() for p in parts.iterdir()},\n"
                " 'https_proxy':os.environ['https_proxy']}\n"
                "pathlib.Path(os.environ['RECEIPT']).write_text(json.dumps(data))\n"
                "sys.exit(23)\n")
            command.chmod(0o755)
            environment = dict(os.environ, APT_CONFIG=str(config), RECEIPT=str(receipt),
                               SONIC_NATIVE_DEBIAN_SNAPSHOT=STAMP,
                               SONIC_NATIVE_DEBIAN_SECURITY_SNAPSHOT=SECURITY_STAMP,
                               https_proxy="", HTTPS_PROXY="")
            original = sources.read_bytes()
            for addition in (False, True):
                if addition:
                    (parts / "later.list").write_text("deb https://vendor.example.invalid/repo trixie main\n")
                completed = subprocess.run([sys.executable, str(HELPER), str(command),
                                            "install", "--", "package-name"], env=environment)
                self.assertEqual(completed.returncode, 23)
                result = json.loads(receipt.read_text())
                self.assertEqual(sources.read_bytes(), original)
                self.assertFalse(Path(result["view"]).exists())
                self.assertIn("http://deb.debian.org/debian trixie", result["source"])
                self.assertEqual(result["args"][-3:], ["install", "--", "package-name"])
                self.assertIn("Acquire::https::Proxy=DIRECT", result["args"])
                self.assertEqual("later.list" in result["parts"], addition)

    def test_missing_pin_and_source_overrides_fail_before_execution(self):
        for stamp, arguments in (("", ["update"]), (STAMP, ["-c", "/tmp/other", "update"]),
                                 (STAMP, ["-o", "Dir::Etc::sourcelist=/tmp/other", "update"])):
            with self.subTest(stamp=stamp, arguments=arguments), mock.patch.dict(
                    os.environ, SONIC_NATIVE_DEBIAN_SNAPSHOT=stamp,
                    SONIC_NATIVE_DEBIAN_SECURITY_SNAPSHOT=SECURITY_STAMP), self.assertRaises(ValueError):
                native.run("never-executed", arguments)

    def test_copy_preserves_restricted_source_reader_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "private.list"
            destination = Path(temporary) / "copy.list"
            source.write_text("deb https://vendor.example.invalid/repo trixie main\n")
            source.chmod(0o600)
            native.copy_source(source, destination, STAMPS)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
