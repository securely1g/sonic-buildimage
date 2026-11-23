"""Exercise native validation selection without allocating a build runner."""

from pathlib import Path
from unittest import TestCase, mock

import yaml

from tools.bazel.ci import native_validation as native


ROOT = Path(__file__).resolve().parents[3]
EVENTD = "bazel-eventd.yml"
RUST = "bazel-supervisor-rust.yml"


def inputs(workflow):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    return document["jobs"]["plan"]["with"]["paths"].split()


class NativeValidationTest(TestCase):
    def test_skip_requires_a_successful_matrix_on_both_architectures(self):
        """A green selector or a lone AMD64 check cannot attest to ARM64."""
        candidates = [
            {"id": n, "head_sha": str(n) * 40} for n in [3, 2, 1]
        ]
        def api(endpoint):
            run = int(endpoint.split("/")[1])
            if run == 3:
                jobs = [{"name": "Check native validation inputs", "conclusion": "success"}]
            else:
                jobs = [{"name": "Native event services (amd64)", "conclusion": "success"},
                        {"name": "Native event services (arm64)", "conclusion": "skipped" if run == 2 else "success"}]
            return {"jobs": jobs}

        with mock.patch.object(native.validation_changes, "successful_master_runs", return_value=candidates):
            self.assertEqual(native.validated_revision(api, EVENTD, "org/repo", 4, lambda _: True), "1" * 40)

    def test_affected_native_owners_and_unavailable_or_shared_inputs(self):
        """An unrelated owner can skip, while changed tests and unclassified inputs run."""
        for filename in native.NATIVE_JOBS:
            paths = inputs(filename)
            self.assertFalse(native.affected(filename, ["doc/unrelated.md"], paths))
            self.assertTrue(native.affected(filename, [".github/workflows/" + filename], paths))
            self.assertTrue(native.affected(filename, ["tools/bazel/unclassified_toolchain.bzl"], paths))
        changed = ["src/sonic-eventd/tests/example.cpp"]
        self.assertTrue(native.affected(EVENTD, changed, inputs(EVENTD)))
        self.assertFalse(native.affected(RUST, changed, inputs(RUST)))
        self.assertTrue(native.affected("runtime-image-validation.yml", ["dockers/docker-orchagent/bazel/runtime_probe.py"],
                                        inputs("runtime-image-validation.yml")))

    def test_missing_baseline_runs_and_dispatch_always_runs(self):
        """Missing evidence selects the suite. Explicit source and cache tests always execute."""
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/master", "GITHUB_REPOSITORY": "up/repo",
               "GITHUB_RUN_NUMBER": "10", "GITHUB_SHA": "a" * 40}
        with mock.patch.object(native, "validated_revision", side_effect=OSError("unavailable")):
            self.assertTrue(native.select_workflow(EVENTD, inputs(EVENTD), env, {})[0])
        env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.assertTrue(native.select_workflow(EVENTD, inputs(EVENTD), env, {})[0])

    def test_fork_uses_its_existing_paths_only(self):
        """Fork runners retain their old source-only scope even if no VS baseline exists."""
        env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "up/repo", "GITHUB_SHA": "c" * 40}
        event = {"pull_request": {"head": {"repo": {"full_name": "fork/repo"}},
                                  "base": {"sha": "b" * 40}}}
        for path, expected in [("unrelated-build-tool.bzl", False), ("src/sonic-eventd/tests/check.cpp", True)]:
            with mock.patch.object(native.validation_changes, "changes", return_value=[path]) as changes:
                self.assertEqual(native.select_workflow(EVENTD, inputs(EVENTD), env, event)[0], expected)
                changes.assert_called_once_with("b" * 40, "c" * 40)
