"""Exercise isolated benchmark selection, lifecycle and immutable cache inputs."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from tools.bazel.ci import vs_cleanup_benchmark as benchmark
from tools.bazel.ci import vs_host_base_snapshot as host
from tools.bazel.ci import vs_kernel_cache_snapshot as kernel


ROOT = Path(__file__).resolve().parents[3]


class CleanupBenchmarkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cleanup-benchmark-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "output"
        event = self.directory / "event.json"
        event.write_text("{}")
        work = self.directory / "work"
        (work / "sonic-buildimage").mkdir(parents=True)
        self.settings = self.directory / ".runner"
        self.settings.write_text(json.dumps({
            "agentName": "benchmark-runner", "ephemeral": False, "workFolder": str(work),
        }))
        self.env = {
            "VS_CLEANUP_BENCHMARK_ARM": "A", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "VS_BENCHMARK_CANARY": "true", "VS_BENCHMARK_SKIP_VS": "false",
            "VS_BENCHMARK_SOURCE_IMAGE": "none", "VS_BENCHMARK_ENROLLED": "",
            "VS_BENCHMARK_HOST_BASE_ARTIFACT": "101", "VS_BENCHMARK_KERNEL_ARTIFACT": "102",
            "GITHUB_REPOSITORY": "example/sonic-buildimage", "GITHUB_RUN_NUMBER": "100",
            "GITHUB_EVENT_PATH": str(event), "GITHUB_OUTPUT": str(self.output),
            "RUNNER_NAME": "benchmark-runner", "RUNNER_WORKSPACE": str(work / "sonic-buildimage"),
        }
        self.artifact = {
            "id": 101, "name": host.MASTER, "expired": False,
            "expires_at": "2100-01-01T00:00:00Z", "workflow_run": {"id": 12},
        }
        self.workflow = {"id": 42, "path": benchmark.WORKFLOW}
        self.run = {
            "id": 12, "run_number": 12, "workflow_id": 42, "path": benchmark.WORKFLOW,
            "event": "push", "head_branch": "master",
            "repository": {"full_name": self.env["GITHUB_REPOSITORY"]},
            "head_repository": {"full_name": self.env["GITHUB_REPOSITORY"]},
        }
        self.api = Mock(side_effect=self.response)

    def response(self, endpoint):
        if endpoint == "artifacts/101":
            return self.artifact
        if endpoint == "artifacts/102":
            return dict(self.artifact, id=102, name=kernel.ARTIFACT)
        return {"workflows/bazel-oci.yml": self.workflow, "runs/12": self.run}[endpoint]

    def test_default_mode_has_no_host_dependencies(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sys, "argv", ["benchmark", "lifecycle"]), \
                patch.object(benchmark.subprocess, "run") as command:
            benchmark.main()
        command.assert_not_called()

    def test_benchmark_rejects_noncanary_dispatch_and_implicit_cache_inputs(self):
        for changes in (
                {"GITHUB_EVENT_NAME": "push"}, {"VS_BENCHMARK_CANARY": "false"},
                {"VS_BENCHMARK_SKIP_VS": "true"}, {"VS_BENCHMARK_SOURCE_IMAGE": "dhcp-relay"},
                {"VS_CLEANUP_BENCHMARK_ARM": "unknown"},
                {"VS_BENCHMARK_HOST_BASE_ARTIFACT": ""},
                {"VS_BENCHMARK_KERNEL_ARTIFACT": "00"},
                {"VS_CLEANUP_BENCHMARK_ARM": "none"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                benchmark.configuration(dict(self.env, **changes))

    def test_lifecycle_emits_auto_removal_only_for_matching_arm_a(self):
        validate = benchmark.validate_lifecycle
        for arm in ("A", "B", "C"):
            with self.subTest(arm=arm):
                self.output.unlink(missing_ok=True)
                settings = json.loads(self.settings.read_text())
                settings["ephemeral"] = arm == "C"
                self.settings.write_text(json.dumps(settings))
                env = dict(self.env, VS_CLEANUP_BENCHMARK_ARM=arm,
                           VS_BENCHMARK_ENROLLED="true" if arm == "C" else "")
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(sys, "argv", ["benchmark", "lifecycle"]), \
                        patch.object(benchmark.subprocess, "run",
                                     return_value=Mock(stdout="active" if arm == "C" else "inactive")), \
                        patch.object(benchmark, "validate_lifecycle",
                                     side_effect=lambda e, s: validate(
                                         e, s, self.settings, self.directory / "work")):
                    benchmark.main()
                self.assertEqual(self.output.read_text(),
                                 "builder_cleanup=" + ("n\n" if arm == "A" else "y\n"))

    def test_wrong_listener_deferral_registration_and_work_root_fail_closed(self):
        for status, env_changes, settings_changes in (
                ("active", {}, {}), ("failed", {}, {}),
                ("inactive", {"VS_BENCHMARK_ENROLLED": "true"}, {}),
                ("inactive", {}, {"ephemeral": True}),
                ("inactive", {}, {"agentName": "other-runner"}),
                ("inactive", {}, {"workFolder": str(self.directory)}),
                ("inactive", {"RUNNER_WORKSPACE": str(self.directory / "sonic-buildimage")},
                 {"workFolder": str(self.directory)}),
                ("active", {"VS_CLEANUP_BENCHMARK_ARM": "C",
                            "VS_BENCHMARK_ENROLLED": "true"}, {"ephemeral": False})):
            with self.subTest(status=status, env=env_changes, settings=settings_changes):
                original = json.loads(self.settings.read_text())
                self.settings.write_text(json.dumps(dict(original, **settings_changes)))
                with self.assertRaises(ValueError):
                    benchmark.validate_lifecycle(
                        dict(self.env, **env_changes), status, self.settings,
                        self.directory / "work")
                self.settings.write_text(json.dumps(original))

    def test_pinned_lookup_never_searches_latest_or_publishes(self):
        for module, expected in (
                (host, "read_artifact_id=101\nread_run_id=12\n"),
                (kernel, "run_id=12\nartifact_id=102\n")):
            with self.subTest(module=module.__name__):
                self.output.unlink(missing_ok=True)
                self.api.reset_mock()
                with patch.dict(os.environ, self.env, clear=True), \
                        patch.object(module, "github_api", self.api), \
                        patch.object(module, "select", side_effect=AssertionError("latest lookup")):
                    module.main()
                self.assertEqual(self.output.read_text(), expected)
                self.assertEqual(len(self.api.call_args_list), 3)

    def test_explicit_zero_avoids_api_and_pinned_errors_never_fall_back(self):
        for module in (host, kernel):
            with self.subTest(module=module.__name__):
                env = dict(self.env, **{key: "0" for key in benchmark.PINS})
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(module, "github_api") as api, \
                        patch.object(module, "select", side_effect=AssertionError("latest lookup")):
                    module.main()
                api.assert_not_called()
                with patch.dict(os.environ, self.env, clear=True), \
                        patch.object(module, "github_api", side_effect=TimeoutError("unavailable")), \
                        self.assertRaises(TimeoutError):
                    module.main()

    def test_untrusted_expired_or_wrong_artifact_and_producer_are_rejected(self):
        for artifact_changes, run_changes, workflow_changes in (
                ({"name": "other"}, {}, {}), ({"id": 999}, {}, {}),
                ({"expired": True}, {}, {}),
                ({"expires_at": "2000-01-01T00:00:00Z"}, {}, {}),
                ({"workflow_run": None}, {}, {}),
                ({}, {"event": "pull_request"}, {}),
                ({}, {"head_branch": "other"}, {}),
                ({}, {"head_repository": {"full_name": "fork/buildimage"}}, {}),
                ({}, {"repository": {"full_name": "fork/buildimage"}}, {}),
                ({}, {"workflow_id": 43}, {}), ({}, {"run_number": 100}, {}),
                ({}, {"path": ".github/workflows/other.yml"}, {}),
                ({}, {}, {"path": ".github/workflows/other.yml"})):
            with self.subTest(artifact=artifact_changes, run=run_changes, workflow=workflow_changes):
                artifact, run, workflow = self.artifact, self.run, self.workflow
                self.artifact = dict(artifact, **artifact_changes)
                self.run = dict(run, **run_changes)
                self.workflow = dict(workflow, **workflow_changes)
                with self.assertRaises(ValueError):
                    benchmark.pinned_artifact(
                        self.api, self.env, host.MASTER, benchmark.PINS[0])
                self.artifact, self.run, self.workflow = artifact, run, workflow

    def test_benchmark_preparation_runs_for_all_arms_without_changing_normal_deferral(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        steps = workflow["jobs"]["vs_container"]["steps"]
        preparation = [step for step in steps if step["name"] in (
            "Reclaim unused images and build cache", "Reclaim local build caches")]
        for arm in (None, "none", "A", "B", "C"):
            for enrolled in ("false", "true"):
                context = {
                    "inputs": SimpleNamespace(vs_cleanup_benchmark_arm=arm),
                    "steps": SimpleNamespace(vs_cleanup_handoff=SimpleNamespace(
                        outputs=SimpleNamespace(enrolled=enrolled))),
                }
                for step in preparation:
                    with self.subTest(arm=arm, enrolled=enrolled, step=step["name"]):
                        expression = step["if"].replace("||", " or ").replace("&&", " and ")
                        enabled = bool(eval(expression, {"__builtins__": {}}, context))
                        self.assertEqual(enabled, enrolled != "true" or arm in ("A", "B", "C"))


if __name__ == "__main__":
    unittest.main()
