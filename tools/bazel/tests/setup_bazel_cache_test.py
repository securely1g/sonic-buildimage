#!/usr/bin/env python3
"""Exercise the real hosted cache handoff without invoking Bazel."""

import json
import os
from pathlib import Path
import re
import shlex
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
rc = Path(next(x.split('=', 1)[1] for x in args if x.startswith('--bazelrc=')))
assert 'try-import %workspace%/.bazelrc.user' in rc.read_text()
Path(os.environ['RECORD']).write_text(json.dumps({'argv': args, 'user': Path('.bazelrc.user').read_text()}))
""")
        recorder.chmod(0o755)
        return dict(os.environ, HOME=str(home), PATH=str(binary) + ":/usr/bin:/bin",
                    RECORD=str(root / "record.json"), BASH_ENV="/dev/null",
                    RUNNER_TEMP=str(root), CI_CPU="x86_64", CI_DISTRIBUTION="trixie")

    def test_all_seven_workflow_calls_reach_isolated_wrapper_and_direct_native(self):
        for filename, job_name in JOBS:
            with self.subTest(workflow=filename, job=job_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                document = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
                job = document["jobs"][job_name]
                steps = job["steps"]
                setup = next(s for s in steps if "setup-bazel@" in s.get("uses", ""))
                handoff = [s for s in steps if HELPER in s.get("run", "")]
                self.assertEqual(len(handoff), 1)
                handoff = handoff[0]
                self.assertEqual(steps.index(handoff), steps.index(setup) + 1)
                repository_only = setup["with"].get("disk-cache") is False
                self.assertEqual(handoff["run"].strip(), "python3 " + HELPER +
                                 (" --repository-only" if repository_only else ""))
                self.assertTrue(setup["with"]["repository-cache"])
                if filename not in ("bazel-base-images.yml", "bazel-oci.yml"):
                    events = document.get("on", document.get(True))
                    for event in ("pull_request", "push"):
                        if event in events:
                            self.assertIn(HELPER, events[event]["paths"])
                home = ("build --disk_cache=/retained/disk\n"
                        "common --repository_cache=/retained/repository\n"
                        "common --registry=https://unrelated.invalid\n"
                        "common --experimental_downloader_config=/unrelated/home.cfg\n"
                        "startup --output_base=/unrelated/output\n")
                prior = "common --experimental_downloader_config=/verified/transport.cfg"
                env = self.prepare(root, home, prior)
                subprocess.run(["bash", "-eu", "-c", handoff["run"]], cwd=root, env=env, check=True)
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
                user = observed["user"]
                self.assertTrue(user.startswith(prior + "\n"))
                self.assertIn("common --repository_cache=/retained/repository\n", user)
                self.assertEqual("build --disk_cache=/retained/disk\n" in user, not repository_only)
                self.assertNotIn("unrelated", user)
                # Existing fresh-root verifiers can still override these defaults.
                override = ["bash", WRAPPER, "build", "--repository_cache=/private/repository",
                            "--disk_cache=/private/disk", "//tools/bazel/tests:archive-fixture.gz"]
                subprocess.run(override, cwd=root, env=env, check=True)
                self.assertEqual(json.loads((root / "record.json").read_text())["argv"][-3:], override[-3:])

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

    def test_normal_ci_discovery_runs_this_regression(self):
        """The normal workflow discovers cache tests alongside other helper tests."""
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        steps = document["jobs"]["checks"]["steps"]
        commands = [(index, step, line) for index, step in enumerate(steps)
                    for line in step.get("run", "").splitlines()
                    if "-m unittest discover -s tools/bazel/tests" in line]
        self.assertEqual(len(commands), 1)
        index, step, command = commands[0]
        self.assertNotIn("if", step)
        self.assertTrue(any("python3-yaml" in s.get("run", "") for s in steps[:index]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "tools/bazel/tests"
            tests.mkdir(parents=True)
            names = ("setup_bazel_cache_test", "kernel_test")
            for name in names:
                (tests / (name + ".py")).write_text(
                    "import unittest\nfrom pathlib import Path\n"
                    "class DiscoveryProbe(unittest.TestCase):\n"
                    "    def test_selected(self):\n"
                    "        Path(" + repr(name + ".selected") + ").touch()\n")
            subprocess.run(shlex.split(command), cwd=root, check=True, capture_output=True, text=True)
            self.assertEqual(sorted(p.stem for p in root.glob("*.selected")), sorted(names))


if __name__ == "__main__":
    unittest.main()
