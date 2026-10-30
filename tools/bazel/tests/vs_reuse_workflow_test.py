"""Execute VS orchestration with controlled Make and archive-verification processes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/bazel/ci/build_vs_with_reused_orchagent.sh"


class VsReuseWorkflowTest(unittest.TestCase):
    def run_build(self, *, measure=False, fail=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        scripts = root / "tools/bazel/ci"
        scripts.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-q", "--allow-empty", "-m", "Fixture revision"], cwd=root, check=True)
        shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
        executable = "#!" + sys.executable + "\n" + '''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
kind = "make" if name == "make" else sys.argv[1]
with pathlib.Path("calls.jsonl").open("a") as stream:
    stream.write(json.dumps([kind, *sys.argv[1:]]) + "\\n")
if os.environ.get("FAIL") == kind or os.environ.get("FAIL") == sys.argv[1]:
    sys.exit(17)
'''
        (root / "make").write_text(executable)
        (root / "make").chmod(0o755)
        (scripts / "vs_archive_reuse.py").write_text(executable)
        result = subprocess.run(
            ["bash", str(scripts / SCRIPT.name), *(["--measure-redundant-request"] if measure else [])],
            cwd=root, text=True, capture_output=True,
            env=dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                     SONIC_CI_CONTAINER_LABEL="test-label", BAZEL="bazel", FAIL=fail),
        )
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        return result, calls, root

    def test_reuses_runtime_and_preserves_remaining_outputs(self):
        result, calls, root = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls], ["make", "capture-pairs", "make", "verify-pairs"])
        make = [call[1:] for call in calls if call[0] == "make"]
        self.assertEqual(make[0][0], "target/sonic-vs.img.gz")
        self.assertFalse(any("target/docker-orchagent.gz" in call for call in make))
        self.assertEqual(make[1][0], "bazel-vs-oci-verify")
        self.assertEqual(make[0][1:], make[1][1:])
        report = json.loads((root / "artifacts/vs/orchagent-reuse-timing.json").read_text())
        self.assertIsNone(report["redundant_request_seconds"])
        self.assertIsNone(report["net_avoided_seconds"])
        timing = json.loads((root / "artifacts/vs/runtime-debug-pairs-timing.json").read_text())
        self.assertEqual(timing["revision"], subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip())
        self.assertFalse(timing["profile"]["install_debug_tools"])
        self.assertAlmostEqual(timing["total_seconds_excluding_calibration"],
                               timing["vs_build_seconds"] + timing["followup_seconds"] + timing["pair_validation_seconds"])

    def test_calibration_uses_identical_flags_and_checks_unchanged_archive(self):
        result, calls, root = self.run_build(measure=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in calls],
                         ["make", "capture-pairs", "make", "verify-runtime", "make", "verify-pairs"])
        self.assertEqual(calls[2][1], "target/docker-orchagent.gz")
        self.assertEqual(calls[0][2:], calls[2][2:])
        report = json.loads((root / "artifacts/vs/orchagent-reuse-timing.json").read_text())
        self.assertGreater(report["redundant_request_seconds"], 0)
        self.assertAlmostEqual(report["net_avoided_seconds"],
                               report["redundant_request_seconds"] - report["validation_seconds"])

    def test_failed_build_or_validation_stops_without_claiming_savings(self):
        for failure in ("target/sonic-vs.img.gz", "capture-pairs", "target/docker-orchagent.gz",
                        "verify-runtime", "bazel-vs-oci-verify", "verify-pairs"):
            with self.subTest(failure=failure):
                result, calls, root = self.run_build(measure=True, fail=failure)
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertFalse((root / "artifacts/vs/orchagent-reuse-timing.json").exists())
                self.assertTrue(failure == calls[-1][0] or failure == calls[-1][1])


if __name__ == "__main__":
    unittest.main()
