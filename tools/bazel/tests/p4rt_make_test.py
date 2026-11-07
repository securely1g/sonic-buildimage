"""Exercise the P4RT Make recipe's build/test policy and failure propagation."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "src/sonic-p4rt"


class P4rtMakeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p4rt-make-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("Makefile", "source_mirrors.cfg"):
            shutil.copyfile(SOURCE / name, self.source / name)
        (self.source / "sonic-pins").mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.runtime = "sonic-p4rt_0.0.1_amd64.deb"
        self.debug = "sonic-p4rt-dbgsym_0.0.1_amd64.deb"
        bazel = self.bin / "bazel"
        bazel.write_text("#!" + sys.executable + "\n" + r'''
import json,os,pathlib,sys
args=sys.argv[1:]
command=next(arg for arg in args if arg in ("build","test","shutdown"))
with open(os.environ["CALLS"],"a") as stream:
    stream.write(json.dumps(args)+"\n")
if command==os.environ.get("FAIL_COMMAND"):
    sys.exit(23)
if command=="build":
    output=pathlib.Path("bazel-bin/p4rt_app")
    output.mkdir(parents=True)
    for name in (os.environ["SONIC_P4RT"],os.environ["SONIC_P4RT_DBG"]):
        (output/name).write_text("recipe fixture, not a Debian package\n")
''')
        bazel.chmod(0o755)

    def run_recipe(self, failure=""):
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                           CALLS=str(self.log), FAIL_COMMAND=failure,
                           SONIC_P4RT=self.runtime, SONIC_P4RT_DBG=self.debug,
                           BAZEL_CACHE=str(self.root / "absent-cache"),
                           BAZEL_OPTS="--host_jvm_args=-Dfixture=1",
                           BAZEL_BUILD_OPTS="--define=fixture=1")
        for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL"):
            environment.pop(key, None)
        return subprocess.run(["make", "--no-print-directory", "DEST=" + str(self.output),
                               str(self.output / self.runtime)], cwd=self.source, env=environment,
                              capture_output=True, text=True, timeout=30)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_build_and_test_share_exact_policy_without_changing_targets_or_options(self):
        result = self.run_recipe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        build, test, shutdown = self.calls()
        policy = "--experimental_downloader_config=" + str(self.source / "source_mirrors.cfg")
        for arguments in (build, test):
            self.assertIn(policy, arguments)
            for option in ("--host_jvm_args=-Dfixture=1", "--define=fixture=1",
                           "--fission=opt", "--copt=-g", "--copt=-gdwarf-4",
                           "--override_repository=sonic_swss_common=/sonic/src/sonic-swss-common"):
                self.assertIn(option, arguments)
            self.assertIn(["-c", "opt"], [arguments[i:i+2] for i in range(len(arguments)-1)])
        self.assertEqual(build[-2:], ["//p4rt_app:p4rt_deb", "//p4rt_app:p4rt_dbg_deb"])
        self.assertEqual(test[-1], "//p4rt_app/...")
        self.assertEqual(shutdown, ["--host_jvm_args=-Dfixture=1", "shutdown"])
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), sorted((self.runtime, self.debug)))
        self.assertFalse((self.source / "sonic-pins/bazel-bin").exists())

    def test_failed_build_or_tests_cannot_publish_packages(self):
        for failure in ("build", "test"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result = self.run_recipe(failure)
                self.assertNotEqual(result.returncode, 0)
                commands = [next(arg for arg in call if arg in ("build", "test", "shutdown"))
                            for call in self.calls()]
                self.assertEqual(commands, ["build", "shutdown"] if failure == "build" else
                                 ["build", "test", "shutdown"])
                self.assertFalse(list(self.output.iterdir()))

    def test_mirror_policy_retains_original_and_does_not_match_other_archives(self):
        rules = [line.split(maxsplit=2)[1:] for line in (SOURCE / "source_mirrors.cfg").read_text().splitlines()
                 if line.startswith("rewrite ")]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0][0], rules[1][0])
        original = ("src.fedoraproject.org/lookaside/extras/xz/xz-5.4.6.tar.gz/sha512/"
                    "b08a61d8d478d3b4675cb1ddacdbbd98dc6941a55bcdd81a28679e54e9367d3a595fa123ac97874a17da571c1b712e2a3e901c2737099a9d268616a1ba3de497/xz-5.4.6.tar.gz")
        self.assertEqual(re.fullmatch(rules[0][0], original)[1], original)
        self.assertEqual(rules[0][1], "https://github.com/tukaani-project/xz/releases/download/v5.4.6/xz-5.4.6.tar.gz")
        self.assertEqual(rules[1][1], "https://$1")
        for other in (original.replace("5.4.6", "5.4.5"), original.replace(".tar.gz", ".tar.xz"),
                      "example.invalid/" + original):
            self.assertIsNone(re.fullmatch(rules[0][0], other))


if __name__ == "__main__":
    unittest.main()
