#!/usr/bin/env python3
"""Exercise worker RC selection and retained hosted-cache helper compatibility."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
HELPER = "tools/bazel/ci/preserve_setup_bazel_cache.py"
OWNER = "frr" if (ROOT / "tools/bazel/ci/frr_draft_bazel.sh").is_file() else "libteam"
WRAPPER = "tools/bazel/ci/" + OWNER + "_draft_bazel.sh"
JOBS = [("bazel-base-images.yml", "native"), ("bazel-eventd.yml", "native"),
        ("bazel-" + OWNER + ".yml", "native"), ("bazel-oci.yml", "archive"),
        ("bazel-oci.yml", "checks"), ("bazel-supervisor-rust.yml", "native"),
        ("bazel-yang-models.yml", "package")]


class SetupBazelCacheTest(unittest.TestCase):
    def prepare(self, root, home_text=None, user=None):
        home = root / "home"
        home.mkdir()
        if home_text is not None:
            (home / ".bazelrc").write_text(home_text)
        if user is not None:
            (root / ".bazelrc.user").write_text(user)
        for name in (HELPER, WRAPPER, "tools/bazel/common.bazelrc"):
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        binary = root / "bin"
        binary.mkdir()
        recorder = binary / "bazel"
        recorder.write_text("""#!/usr/bin/python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert '--nohome_rc' in args
rcs = [Path(x.split('=', 1)[1]) for x in args if x.startswith('--bazelrc=')]
texts = [rc.read_text() for rc in rcs if rc.is_file()]
user = Path('.bazelrc.user')
Path(os.environ['RECORD']).write_text(json.dumps({'argv': args, 'explicit_rc': texts, 'user': user.read_text() if user.exists() else ''}))
""")
        recorder.chmod(0o755)
        environment = dict(os.environ, HOME=str(home), PATH=str(binary) + ":/usr/bin:/bin",
                    RECORD=str(root / "record.json"), BASH_ENV="/dev/null",
                    RUNNER_TEMP=str(root), CI_CPU="x86_64", CI_DISTRIBUTION="trixie")
        environment.pop("SONIC_CI_EXECUTION_MODE", None)
        return environment

    def test_worker_workflows_preserve_managed_rc_and_explicit_cache_overrides(self):
        for filename, job_name in JOBS:
            for mode in ("native", "qemu-aarch64"):
                with self.subTest(workflow=filename, job=job_name, mode=mode), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    document = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
                    job = document["jobs"][job_name]
                    steps = job["steps"]
                    self.assertIn("debian-trixie-container", job["runs-on"])
                    self.assertEqual(job["defaults"]["run"]["shell"], "sonic-ci-shell {0}")
                    self.assertIn("SONIC_CI_ARCH", job["env"])
                    self.assertFalse(any("setup-bazel@" in step.get("uses", "") for step in steps))
                    self.assertFalse(any(HELPER in step.get("run", "") for step in steps))
                    # These are the operator-managed worker defaults, independent
                    # of the checkout and the selected source registry.
                    home = ("common --disk_cache=/retained/disk\n"
                            "common --repository_cache=/retained/repository\n")
                    prior = "common --experimental_downloader_config=/verified/transport.cfg\n"
                    env = self.prepare(root, home, prior)
                    env["SONIC_CI_EXECUTION_MODE"] = mode
                    if filename == "bazel-" + OWNER + ".yml":
                        build = next(s for s in steps if "run_bazel()" in s.get("run", ""))
                        script = build["run"]
                        options = re.search(r"(?ms)^options=\(\n.*?^\)", script).group()
                        function = re.search(r"(?ms)^run_bazel\(\) \{\n.*?^\}", script).group()
                        env["SONIC_BAZEL_REGISTRY"] = build["env"]["SONIC_BAZEL_REGISTRY"]
                        command = options + "\n" + function + "\nrun_bazel info repository_cache"
                    else:
                        self.assertIn(WRAPPER, str(job))
                        command = "bash " + WRAPPER + " info repository_cache"
                    subprocess.run(["bash", "-eu", "-c", command], cwd=root, env=env, check=True)
                    observed = json.loads((root / "record.json").read_text())
                    self.assertIn("--bazelrc=/etc/bazel.bazelrc", observed["argv"])
                    self.assertIn("--bazelrc=" + str(root / "home/.bazelrc"), observed["argv"])
                    self.assertIn(home, observed["explicit_rc"])
                    self.assertEqual(observed["user"], prior)
                    # Fresh-root verifiers must still override the worker defaults.
                    override = ["bash", WRAPPER, "build", "--repository_cache=/private/repository",
                                "--disk_cache=/private/disk", "//tools/bazel/tests:archive-fixture.gz"]
                    subprocess.run(override, cwd=root, env=env, check=True)
                    self.assertEqual(json.loads((root / "record.json").read_text())["argv"][-3:], override[-3:])

    def test_nonworker_wrapper_still_isolates_unrelated_home_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = ("build --disk_cache=/retained/disk\n"
                    "common --repository_cache=/retained/repository\n"
                    "common --registry=https://unrelated.invalid\n"
                    "startup --output_base=/unrelated/output\n")
            env = self.prepare(root, home)
            subprocess.run(["python3", HELPER], cwd=root, env=env, check=True)
            subprocess.run(["bash", WRAPPER, "info", "repository_cache"], cwd=root, env=env, check=True)
            observed = json.loads((root / "record.json").read_text())
            self.assertEqual(sum(arg.startswith("--bazelrc=") for arg in observed["argv"]), 1)
            self.assertIn("build --disk_cache=/retained/disk", observed["user"])
            self.assertIn("common --repository_cache=/retained/repository", observed["user"])
            self.assertNotIn("unrelated", observed["user"])
            self.assertNotIn(home, observed["explicit_rc"])

    def test_inspector_heap_and_shutdown_use_the_same_worker_startup_settings(self):
        for mode in ("native", "qemu-aarch64"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = self.prepare(root, "common --repository_cache=/retained/repository\n")
                env["SONIC_CI_EXECUTION_MODE"] = mode
                subprocess.run(["bash", WRAPPER, "--host_jvm_args=-Xmx4096m", "mod", "graph",
                                "--lockfile_mode=off"], cwd=root, env=env, check=True)
                command = json.loads((root / "record.json").read_text())["argv"]
                self.assertLess(command.index("--host_jvm_args=-Xmx4096m"), command.index("mod"))
                self.assertEqual(command[-1], "--lockfile_mode=off")
                subprocess.run(["bash", WRAPPER, "shutdown"], cwd=root, env=env, check=True)
                shutdown = json.loads((root / "record.json").read_text())["argv"]
                self.assertEqual(shutdown[-1], "shutdown")
                self.assertIn("--bazelrc=/etc/bazel.bazelrc", shutdown)
                self.assertFalse(any(arg.startswith(("--registry=", "--lockfile_mode=")) for arg in shutdown))
                self.assertFalse(any(arg.endswith("/common.bazelrc") for arg in shutdown))

    def test_missing_duplicate_and_invalid_settings_leave_user_file_unchanged(self):
        cases = [None, "", "build --disk_cache=/disk\n",
                 "common --repository_cache=/repo\n",
                 "common --repository_cache=/one\ncommon --repository_cache=/two\nbuild --disk_cache=/disk\n",
                 "common --repository_cache=/repo\nbuild --disk_cache=/one\nbuild --disk_cache=/two\n",
                 "common --repository_cache=\nbuild --disk_cache=/disk\n",
                 "common --repository_cache=/repo --registry=https://unrelated.invalid\nbuild --disk_cache=/disk\n"]
        for settings in cases:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prior = "common --experimental_downloader_config=/verified/config\n"
                env = self.prepare(root, settings, prior)
                result = subprocess.run(["python3", HELPER], cwd=root, env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / ".bazelrc.user").read_text(), prior)
                self.assertFalse((root / "record.json").exists())

    def test_repository_only_and_newline_boundaries_preserve_policy(self):
        for prior in (None, "", "common --distdir=/retained/downloads", "common --distdir=/retained/downloads\n"):
            with self.subTest(prior=prior), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = self.prepare(root, "common --repository_cache=/repo\nbuild --disk_cache=/ignored\n", prior)
                subprocess.run(["python3", HELPER, "--repository-only"], cwd=root, env=env, check=True)
                prefix = (prior or "") + ("\n" if prior and not prior.endswith("\n") else "")
                self.assertEqual((root / ".bazelrc.user").read_text(), prefix + "common --repository_cache=/repo\n")

    def test_normal_regression_jobs_discover_the_cache_tests_without_branch_skips(self):
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        for job_name in ("archive", "checks"):
            with self.subTest(job=job_name):
                job = document["jobs"][job_name]
                self.assertNotIn("SOURCE_ONLY_NO_DEBS", str(job))
                steps = job["steps"]
                checks = [s for s in steps if "python3 -B -m unittest discover -s tools/bazel/tests -p '*_test.py'" in s.get("run", "")]
                self.assertEqual(len(checks), 1)
                index = steps.index(checks[0])
                self.assertTrue(any("python3-yaml" in s.get("run", "") for s in steps[:index]))


if __name__ == "__main__":
    unittest.main()
