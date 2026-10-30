"""Execute the separate VS build and validation steps with controlled producers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.tests.source_workflow_test import jobs, shell, steps


class VsBuildWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vs-build-workflow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scripts = self.root / "tools/bazel/ci"
        self.scripts.mkdir(parents=True)
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.revision = self.root / "revision"
        self.revision.write_text("a" * 40 + "\n")
        for name in ("build_vs.sh", "validate_vs.sh"):
            shutil.copyfile(ROOT / "tools/bazel/ci" / name, self.scripts / name)
        recorder = "#!" + sys.executable + "\n" + r'''
import json, os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
action = "make" if name == "make" else args[0]
with pathlib.Path("calls.jsonl").open("a") as output:
    output.write(json.dumps({"action": action, "args": args,
        "bazel_args": os.environ.get("BAZEL_CONTAINER_ARGS"),
        "build_revision": pathlib.Path("artifacts/vs/build-revision.txt").read_text().strip()}) + "\n")
print("controlled " + action)
if os.environ.get("CHANGE_REVISION_ACTION") in (action, args[0]):
    pathlib.Path("revision").write_text("b" * 40 + "\n")
if os.environ.get("FAIL_ACTION") in (action, args[0]):
    sys.exit(17)
'''
        (self.commands / "make").write_text(recorder)
        (self.commands / "make").chmod(0o755)
        (self.scripts / "vs_archive_reuse.py").write_text(recorder)
        # The build records Git identity before Make; Python checks and timing
        # belong exclusively to the separate validation helper.
        gate = "#!" + sys.executable + "\n" + r'''
import os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
if name != "git" and os.environ.get("FORBID_VALIDATION") == "1":
    sys.exit("The build step must not call " + name)
if name == "git":
    assert sys.argv[1:] == ["rev-parse", "HEAD"]
    print(pathlib.Path("revision").read_text().strip())
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''
        for name in ("git", "python3"):
            path = self.commands / name
            path.write_text(gate)
            path.chmod(0o755)
        self.environment = dict(
            os.environ, PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            SONIC_CI_CONTAINER_LABEL="vs-fixture-label", BAZEL="fixture-bazel",
            BAZEL_CONTAINER_ARGS="--distdir='/source downloads' --jobs=3",
        )
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL", "FAIL_ACTION",
                     "FORBID_VALIDATION", "CHANGE_REVISION_ACTION"):
            self.environment.pop(name, None)

    def run_script(self, name, *args, **environment):
        env = dict(self.environment)
        for key, value in environment.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(["bash", str(self.scripts / name), *args], cwd=self.root,
                              env=env, text=True, capture_output=True, check=False)

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def prepare_build(self):
        result = self.run_script("build_vs.sh", FORBID_VALIDATION="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.root / "calls.jsonl").unlink()

    def assert_profile(self, call, target):
        self.assertEqual(call["args"], [
            target, "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "INCLUDE_VS_DASH_SAI=y", "INCLUDE_FIPS=y",
            "ENABLE_ASAN=n", "ENABLE_SYNCD_RPC=n",
            "SONIC_BUILDER_EXTRA_CMDLINE=--label vs-fixture-label -e BAZEL=fixture-bazel -e BAZEL_CONTAINER_ARGS",
        ])
        self.assertEqual(call["bazel_args"], self.environment["BAZEL_CONTAINER_ARGS"])

    def test_build_records_revision_before_one_make_without_validation_receipts(self):
        result = self.run_script("build_vs.sh", FORBID_VALIDATION="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call["action"] for call in self.calls()], ["make"])
        self.assert_profile(self.calls()[0], "target/sonic-vs.img.gz")
        self.assertEqual(self.calls()[0]["build_revision"], "a" * 40)
        self.assertEqual([path.name for path in (self.root / "artifacts/vs").iterdir()], ["build-revision.txt"])

    def test_build_failure_propagates_without_a_followup(self):
        result = self.run_script("build_vs.sh", FAIL_ACTION="make", FORBID_VALIDATION="1")
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertEqual([call["action"] for call in self.calls()], ["make"])
        self.assertEqual([path.name for path in (self.root / "artifacts/vs").iterdir()], ["build-revision.txt"])

    def test_extra_arguments_and_old_calibration_are_rejected_before_make(self):
        for name in ("build_vs.sh", "validate_vs.sh"):
            for argument in ("--measure-redundant-request", "target/docker-orchagent.gz"):
                with self.subTest(name=name, argument=argument):
                    result = self.run_script(name, argument)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(self.calls(), [])

    def test_build_requires_the_existing_container_label_and_bazel_setting(self):
        for name in ("SONIC_CI_CONTAINER_LABEL", "BAZEL"):
            with self.subTest(name=name):
                result = self.run_script("build_vs.sh", **{name: None})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_validation_captures_side_outputs_before_followup_then_verifies_them(self):
        self.prepare_build()
        result = self.run_script("validate_vs.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual([call["action"] for call in calls], ["capture-pairs", "make", "verify-pairs"])
        self.assert_profile(calls[1], "bazel-vs-oci-verify")
        self.assertEqual(calls[0]["args"][1:], calls[2]["args"][1:])
        self.assertIn("--receipt", calls[0]["args"])
        self.assertIn("--orchagent-receipt", calls[0]["args"])
        timing = json.loads((self.root / "artifacts/vs/runtime-debug-pairs-timing.json").read_text())
        self.assertEqual(timing["schema"], 2)
        self.assertEqual(timing["revision"], "a" * 40)
        self.assertFalse(timing["profile"]["install_debug_tools"])
        self.assertGreaterEqual(timing["p4_validation_seconds"], 0)
        self.assertGreaterEqual(timing["pair_validation_seconds"], 0)
        self.assertAlmostEqual(timing["validation_seconds"],
                               timing["p4_validation_seconds"] + timing["pair_validation_seconds"])
        self.assertEqual(set(timing), {"schema", "revision", "profile", "p4_validation_seconds",
                                       "pair_validation_seconds", "validation_seconds", "scope"})
        self.assertFalse((self.root / "artifacts/vs/orchagent-reuse-timing.json").exists())

    def test_validation_failures_stop_before_later_checks_or_success_timing(self):
        self.prepare_build()
        actions = ["capture-pairs", "make", "verify-pairs"]
        for index, action in enumerate(actions):
            with self.subTest(action=action):
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                result = self.run_script("validate_vs.sh", FAIL_ACTION=action)
                self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
                self.assertEqual([call["action"] for call in self.calls()], actions[:index + 1])
                self.assertFalse((self.root / "artifacts/vs/runtime-debug-pairs-timing.json").exists())

    def test_missing_build_revision_stops_before_any_validation_or_make(self):
        result = self.run_script("validate_vs.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing VS build revision record", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_revision_change_during_build_or_between_steps_stops_validation(self):
        for when in ("during-build", "between-steps"):
            with self.subTest(when=when):
                self.revision.write_text("a" * 40 + "\n")
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                result = self.run_script("build_vs.sh", CHANGE_REVISION_ACTION=(
                    "target/sonic-vs.img.gz" if when == "during-build" else ""))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls()[0]["build_revision"], "a" * 40)
                if when == "between-steps":
                    self.revision.write_text("b" * 40 + "\n")
                result = self.run_script("validate_vs.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Source revision changed since the VS build started", result.stderr)
                self.assertEqual([call["action"] for call in self.calls()], ["make"])
                self.assertFalse((self.root / "artifacts/vs/runtime-debug-pairs-timing.json").exists())

    def test_revision_change_during_validation_cannot_produce_success_timing(self):
        self.prepare_build()
        result = self.run_script("validate_vs.sh", CHANGE_REVISION_ACTION="verify-pairs")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Source revision changed during VS validation", result.stderr)
        self.assertEqual([call["action"] for call in self.calls()], ["capture-pairs", "make", "verify-pairs"])
        self.assertFalse((self.root / "artifacts/vs/runtime-debug-pairs-timing.json").exists())

    def workflow_steps(self):
        job = next(job for job in jobs().values() if "tools/bazel/ci/build_vs.sh" in job)
        selected = steps(job)
        build = next(name for name, body in selected.items() if "tools/bazel/ci/build_vs.sh" in body)
        validation = next(name for name, body in selected.items() if "tools/bazel/ci/validate_vs.sh" in body)
        return selected, build, validation

    def test_workflow_exposes_build_and_validation_as_separate_ordered_steps(self):
        selected, build, validation = self.workflow_steps()
        self.assertEqual(build, "Build the VS image")
        self.assertEqual(validation, "Validate VS container pairs and P4 debug symbols")
        self.assertLess(list(selected).index(build), list(selected).index(validation))
        (self.root / "artifacts/vs").mkdir(parents=True)
        for name in (build, validation):
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell(selected[name])],
                                    cwd=self.root, env=self.environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call["action"] for call in self.calls()],
                         ["make", "capture-pairs", "make", "verify-pairs"])
        build_log = (self.root / "artifacts/vs/build.log").read_text()
        self.assertIn("controlled make", build_log)
        self.assertNotIn("capture-pairs", build_log)
        validation_log = (self.root / "artifacts/vs/validation.log").read_text()
        self.assertIn("controlled capture-pairs", validation_log)
        self.assertIn("controlled verify-pairs", validation_log)

    def test_workflow_tee_does_not_hide_build_or_validation_failure(self):
        selected, build, validation = self.workflow_steps()
        (self.root / "artifacts/vs").mkdir(parents=True)
        for name, failure in ((build, "make"), (validation, "verify-pairs")):
            with self.subTest(step=name):
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell(selected[name])],
                                        cwd=self.root, env=dict(self.environment, FAIL_ACTION=failure),
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 17, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
