"""Build the real hook package and check its Docker-cache input is reproducible."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "src/sonic-build-hooks"


@unittest.skipUnless(shutil.which("dpkg-deb") and shutil.which("make"),
                     "requires native dpkg-deb and make")
class BuildHooksPackageTest(unittest.TestCase):
    def test_checkout_times_and_parent_epoch_do_not_change_package_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            packages = []
            for number, epoch in enumerate((1600000000, 1700000000)):
                checkout = workspace / str(number)
                checkout.mkdir()
                shutil.copyfile(SOURCE / "Makefile", checkout / "Makefile")
                for directory in ("scripts", "hooks", "debian"):
                    shutil.copytree(SOURCE / directory, checkout / directory)
                for path in checkout.rglob("*"):
                    os.utime(path, (epoch - 100, epoch - 100), follow_symlinks=False)
                # The parent Makefile exports the image commit timestamp. Test
                # both environment and command-line inheritance into this make.
                subprocess.run(["make", "all", "TRUSTED_GPG_URLS=",
                                f"SOURCE_DATE_EPOCH={epoch}"], cwd=checkout,
                               env=dict(os.environ, SOURCE_DATE_EPOCH=str(epoch)),
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                package = checkout / "buildinfo/sonic-build-hooks_1.0_all.deb"
                packages.append(package.read_bytes())
                for option in ("--ctrl-tarfile", "--fsys-tarfile"):
                    archive = subprocess.check_output(["dpkg-deb", option, str(package)])
                    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
                        members = stream.getmembers()
                    self.assertTrue(members)
                    self.assertEqual({member.mtime for member in members}, {0})
            self.assertEqual(packages[0], packages[1])

            # Rebuilding after a real hook edit must change the cached input.
            script = checkout / "scripts/pre_run_buildinfo"
            script.write_bytes(script.read_bytes() + b"\n# changed hook payload\n")
            package.unlink()
            subprocess.run(["make", "all", "TRUSTED_GPG_URLS="], cwd=checkout,
                           env=dict(os.environ, SOURCE_DATE_EPOCH="1800000000"),
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(package.read_bytes(), packages[-1])
            archive = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(package)])
            with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
                installed = stream.extractfile("./usr/local/share/buildinfo/scripts/pre_run_buildinfo")
                self.assertTrue(installed.read().endswith(b"# changed hook payload\n"))


if __name__ == "__main__":
    unittest.main()
