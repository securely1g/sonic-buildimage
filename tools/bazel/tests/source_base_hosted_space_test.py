#!/usr/bin/env python3
"""Exercise hosted space allocation through recorders, never host cleanup."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/source-base-hosted-space"
MOUNTS = ["/usr/local/lib/android:/runner-unused/android",
          "/usr/share/dotnet:/runner-unused/dotnet", "/opt/ghc:/runner-unused/ghc"]
SDK_PATHS = [mount.split(":")[1] for mount in MOUNTS]


def condition(expression, **changes):
    context = {"github.event_name": "pull_request", "github.head_ref": BRANCH,
               "github.event.pull_request.number": 10000,
               "github.event.pull_request.head.repo.full_name": "securely1g/sonic-buildimage",
               "github.repository": "securely1g/sonic-buildimage",
               "github.ref": "refs/pull/10000/merge", "inputs.skip_vs": True,
               "inputs.source_image": "none", "matrix.architecture": "amd64"}
    context.update(changes)
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    for key in sorted(context, key=len, reverse=True):
        expression = expression.replace(key, repr(context[key]))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    expression = re.sub(r"\btrue\b", "True", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return eval(" ".join(expression.split()), {"__builtins__": {}}, {})


class HostedSourceBaseSpaceTest(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-base-images.yml").read_text())
        self.native = self.workflow["jobs"]["native"]
        steps = self.native["steps"]
        self.step = next(s for s in steps if s.get("name") == "Allocate source-build space on this disposable runner")
        self.assertIs(self.step, steps[0])

    def run_step(self, runner_environment):
        # Every external command in the accepted shell is intercepted. In
        # particular, find records its -exec arguments and never executes rm.
        with tempfile.TemporaryDirectory(prefix="source-base-space-") as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            record = root / "commands.jsonl"
            command = '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
with open(os.environ["SPACE_TEST_RECORD"], "a") as output:
    output.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
'''
            for name in ("df", "du", "find", "rm"):
                path = tools / name
                path.write_text(command)
                path.chmod(0o755)
            protected = [root / name for name in ("workspace/source", "workspace/artifacts/archive.gz",
                         "cache/repository_cache/source", "cache/disk_cache/action", "execution-tools/compiler")]
            for path in protected:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"preserved")
            env = {**os.environ, "PATH": str(tools), "GITHUB_WORKSPACE": str(root / "workspace"),
                   "SPACE_TEST_RECORD": str(record)}
            env.pop("RUNNER_ENVIRONMENT", None)
            if runner_environment is not None:
                env["RUNNER_ENVIRONMENT"] = runner_environment
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", self.step["run"]],
                                    env=env, text=True, capture_output=True, timeout=10)
            calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
            for path in protected:
                self.assertEqual(path.read_bytes(), b"preserved")
            return result, calls, str(root / "workspace")

    def test_only_three_unused_host_sdks_are_mounted(self):
        self.assertEqual(self.native["container"]["volumes"], MOUNTS)
        self.assertEqual(self.native["container"]["image"],
                         "debian:trixie-20260918@sha256:9cc080028c43b27d2074d63a5f9caf7166d731494965616c1a6d2827a004585c")
        self.assertTrue(condition(self.step["if"]))
        self.assertFalse(condition(self.step["if"], **{"matrix.architecture": "arm64"}))

    def test_hosted_cleanup_is_confined_and_preserves_build_state(self):
        result, calls, workspace = self.run_step("github-hosted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            ["df", "-h", "/", workspace],
            ["du", "-sh", *SDK_PATHS],
            ["find", *SDK_PATHS, "-mindepth", "1", "-maxdepth", "1", "-exec", "rm", "-rf", "--", "{}", "+"],
            ["df", "-h", "/", workspace],
        ])
        self.assertNotIn("rm", [call[0] for call in calls])

    def test_self_hosted_and_missing_identity_fail_before_commands(self):
        for identity in ("self-hosted", None, "github-hosted-other", ""):
            with self.subTest(identity=identity):
                result, calls, _ = self.run_step(identity)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_native_architectures_gnoi_and_cache_are_preserved(self):
        self.assertEqual(self.native["strategy"]["matrix"]["include"], [
            {"architecture": "amd64", "runner": "ubuntu-24.04"},
            {"architecture": "arm64", "runner": "ubuntu-24.04-arm"}])
        steps = self.native["steps"]
        gnoi = next(i for i,s in enumerate(steps) if s.get("name") == "Initialize Sysmgr's pinned gNOI source")
        bazel = next(i for i,s in enumerate(steps) if s.get("uses", "").startswith("bazel-contrib/setup-bazel@"))
        self.assertLess(gnoi, bazel)
        self.assertEqual(steps[bazel]["with"]["repository-cache"], True)
        self.assertEqual(steps[bazel]["with"]["disk-cache"], "base-images-${{ matrix.architecture }}")
        self.assertIn("runtime", self.workflow["jobs"])

    def test_own_pr_excludes_only_deb_fixtures_and_full_vs(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        guarded = [step for job in workflow["jobs"].values() for step in job.get("steps", [])
                   if "SOURCE_ONLY_NO_DEBS" in step.get("env", {})]
        self.assertEqual(len(guarded), 2)
        for step in guarded:
            guard = step["env"]["SOURCE_ONLY_NO_DEBS"]
            self.assertTrue(condition(guard))
            for changes in ({"github.head_ref": BRANCH + "-suffix"}, {"github.head_ref": "unrelated"},
                            {"github.event.pull_request.head.repo.full_name": "other/fork"},
                            {"github.event_name": "push"}, {"github.event_name": "workflow_dispatch"}):
                with self.subTest(changes=changes):
                    self.assertFalse(condition(guard, **changes))
            self.assertIn("for test in tools/bazel/tests/*_test.py", step["run"])
            self.assertIn('python3 -B "$test"', step["run"])
        self.assertFalse(condition(workflow["jobs"]["vs_container"]["if"]))
        self.assertTrue(condition(workflow["jobs"]["vs_container"]["if"], **{"github.head_ref": BRANCH + "-suffix"}))
        self.assertFalse(condition(workflow["jobs"]["vs"]["if"]))
        for name in ("archive", "checks"):
            self.assertTrue(condition(workflow["jobs"][name].get("if", "true")))
        kernel = yaml.safe_load((ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text())
        self.assertFalse(condition(kernel["jobs"]["kernel-bundle"]["if"]))
        self.assertTrue(condition(kernel["jobs"]["kernel-bundle"]["if"], **{"github.event.pull_request.number": 6}))


if __name__ == "__main__":
    unittest.main()
