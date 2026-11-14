"""Check source download policy on the P4Runtime package's actual Make recipe."""

import json
import os
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
    def test_nested_bazel_commands_and_package_export_are_timed_on_a_cache_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tools").symlink_to(SOURCE.parents[1] / "tools")
            source = root / "src/sonic-p4rt"
            (source / "sonic-pins").mkdir(parents=True)
            (source / "Makefile").symlink_to(SOURCE / "Makefile")
            dest = root / "debs"
            dest.mkdir()
            bazel = root / "bazel"
            bazel.write_text("""#!/bin/bash
set -eu
[[ $1 == --output_user_root=* ]]
echo "fixture Bazel $2"
if [[ $2 != shutdown ]]; then
    [[ $3 == --profile=* && $4 == --noslim_profile ]]
    touch "${3#--profile=}"
fi
if [[ $2 == build ]]; then
    mkdir -p bazel-bin/p4rt_app
    touch bazel-bin/p4rt_app/{p4rt,p4rt-dbg}.deb
fi
""")
            bazel.chmod(0o755)
            command = ["make", "--no-print-directory", f"DEST={dest}", f"BAZEL_CACHE={root}",
                       "SONIC_P4RT=p4rt.deb", "SONIC_P4RT_DBG=p4rt-dbg.deb", str(dest / "p4rt.deb")]
            env = dict(os.environ, SONIC_VS_IMAGE_TIMINGS="1", PATH=f"{root}:{os.environ['PATH']}")
            for _ in range(2):  # The second call reuses the packages and must leave no new rows.
                subprocess.run(command, env=env, cwd=source, check=True, capture_output=True)
            directory, = (root / "artifacts/vs/image-timings/sonic-p4rt").iterdir()
            self.assertEqual(json.loads((directory / "recipe.json").read_text()),
                             {"bazel": ["build", "test", "shutdown"], "profiles": ["build", "test"],
                              "exports": ["stage-debs"]})
            for phase in ("build", "test", "shutdown", "stage-debs"):
                self.assertEqual(json.loads((directory / f"{phase}.timing.json").read_text())["exit_code"], 0)
            self.assertCountEqual([p.name for p in directory.glob("*.profile.json.gz")],
                                  ["build.profile.json.gz", "test.profile.json.gz"])
            self.assertIn("fixture Bazel test", (directory / "make.log").read_text())
            self.assertTrue((dest / "p4rt-dbg.deb").is_file())

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
