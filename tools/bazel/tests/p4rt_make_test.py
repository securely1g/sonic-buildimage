"""Check source download policy on the P4Runtime package's actual Make recipe."""

from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[3] / "src/sonic-p4rt"
POLICY = SOURCE / "source_mirrors.cfg"
FEDORA = (
    "src.fedoraproject.org/lookaside/extras/xz/xz-5.4.6.tar.gz/sha512/"
    "b08a61d8d478d3b4675cb1ddacdbbd98dc6941a55bcdd81a28679e54e9367d3a"
    "595fa123ac97874a17da571c1b712e2a3e901c2737099a9d268616a1ba3de497/xz-5.4.6.tar.gz"
)


class P4rtMakeTest(unittest.TestCase):
    def test_mirror_policy_is_passed_to_both_nested_bazel_commands(self):
        with tempfile.TemporaryDirectory() as dest:
            result = subprocess.check_output([
                "make", "--no-print-directory", "--dry-run", "DEST=" + dest,
                "SONIC_P4RT=p4rt.deb", "SONIC_P4RT_DBG=p4rt-dbg.deb",
                "BAZEL_CACHE=" + dest + "/absent", dest + "/p4rt.deb",
            ], cwd=SOURCE, text=True)
        build, test = [shlex.split(line) for line in result.splitlines() if line.startswith("bazel ")]
        for command in (build, test):
            self.assertIn("--experimental_downloader_config=" + str(POLICY), command)
        self.assertEqual(build[1], "build")
        self.assertEqual(build[-2:], ["//p4rt_app:p4rt_deb", "//p4rt_app:p4rt_dbg_deb"])
        self.assertEqual(test[1], "test")
        self.assertEqual(test[-1], "//p4rt_app/...")

    def test_only_the_pinned_xz_archive_adds_an_official_mirror(self):
        rewrites = [line.split()[1:] for line in POLICY.read_text().splitlines() if line.startswith("rewrite ")]
        self.assertEqual([url for pattern, url in rewrites if re.fullmatch(pattern, FEDORA)], [
            "https://github.com/tukaani-project/xz/releases/download/v5.4.6/xz-5.4.6.tar.gz", "https://$1",
        ])
        for pattern, _ in rewrites:
            self.assertEqual(re.fullmatch(pattern, FEDORA).group(1), FEDORA)
            self.assertIsNone(re.fullmatch(pattern, FEDORA.replace("sha512/b08", "sha512/000")))
            self.assertIsNone(re.fullmatch(pattern, FEDORA.replace("5.4.6", "5.4.5")))


if __name__ == "__main__":
    unittest.main()
