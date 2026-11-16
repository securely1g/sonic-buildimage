#!/usr/bin/env python3
"""Exercise the maintained owner runner's explicit short-test-directory option."""

from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


def load_owner():
    source = Path(os.environ.get("CONTAINER_OWNER_SOURCE", Path(__file__).resolve().parents[1] / "ci/container.py"))
    stubs = {}
    for name in (
        "tools", "tools.bazel", "tools.bazel.ci", "tools.bazel.gzip",
        "tools.bazel.ci.build", "tools.bazel.ci.cache_reuse", "tools.bazel.ci.command_log",
        "tools.bazel.ci.public_artifacts", "tools.bazel.ci.resolution",
        "tools.bazel.ci.artifact_validation", "tools.bazel.ci.bazel_commands",
        "tools.bazel.gzip.source_archive",
    ):
        stubs[name] = types.ModuleType(name)
        stubs[name].__path__ = []
    for name, module in stubs.items():
        if "." in name:
            parent, leaf = name.rsplit(".", 1)
            setattr(stubs[parent], leaf, module)

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    stubs["tools.bazel.ci.artifact_validation"].require = require
    stubs["tools.bazel.ci.artifact_validation"].sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    stubs["tools.bazel.ci.public_artifacts"].label = lambda value: value
    stubs["tools.bazel.ci.bazel_commands"].inspect_actions = lambda path: {"deb_outputs": [], "packaging_wrappers": []}
    stubs["tools.bazel.gzip.source_archive"].check_versions = lambda path: None
    spec = importlib.util.spec_from_file_location("container_owner_under_test", source)
    owner = importlib.util.module_from_spec(spec)
    saved_path = sys.path[:]
    try:
        with mock.patch.dict(sys.modules, stubs):
            spec.loader.exec_module(owner)
    finally:
        sys.path[:] = saved_path
    return owner


class ContainerShortTestTemporaryTests(unittest.TestCase):
    def setUp(self):
        self.owner = load_owner()
        self.temporary = tempfile.TemporaryDirectory(prefix="cst-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".bazelversion").write_text("8.5.1\n")
        (self.workspace / "MODULE.bazel").write_text("module(name='fixture')\n")
        self.artifacts = self.root / "artifacts"
        self.parent = self.root / "job"
        self.parent.mkdir(mode=0o700)
        self.sentinel = self.parent / "preserve"
        self.sentinel.write_text("other job-owned state")
        self.commands = []
        self.observed_bases = []
        self.original_home = os.environ.get("HOME")
        self.selected_tests = ["//fixture:one", "//fixture:two"]

    def exercise(self, *, enabled, fail=False):
        owner = self.owner
        expected_suite = "//dockers/docker-orchagent:ci_tests"

        def execute(argv, artifacts, receipt, log, *, cwd, output_path=None):
            self.assertEqual(cwd, self.workspace)
            self.commands.append((log, list(argv)))
            receipt["commands"].append({"argv": list(argv), "log": log})
            flags = [arg for arg in argv if arg.startswith("--test_tmpdir=")]
            if log != "tests":
                self.assertEqual(flags, [])
            if log == "revision":
                return "0" * 40 + "\n"
            if log == "bazel-version":
                return "bazel 8.5.1\n"
            if log == "actions":
                output_path.write_text("{}\n")
                return ""
            self.assertEqual(log, "tests")
            self.assertEqual(argv[:2], ["bazel-owner", "test"])
            self.assertEqual(argv[-1], expected_suite)
            self.assertIn("--nocache_test_results", argv)
            self.assertFalse(any(arg.startswith("--test_filter") for arg in argv))
            if enabled:
                self.assertEqual(len(flags), 1)
                base = Path(flags[0].split("=", 1)[1])
                self.assertEqual(base.parent, self.parent)
                self.assertTrue(base.is_dir())
                self.assertEqual(base.stat().st_mode & 0o777, 0o700)
                self.assertLessEqual(len(str(base).encode()), 50)
                self.observed_bases.append(base)
                self.assertFalse(receipt["test_temporary_directory"]["cleanup_complete"])
            else:
                self.assertEqual(flags, [])
                self.assertNotIn("test_temporary_directory", receipt)
            if fail:
                raise subprocess.CalledProcessError(1, argv)
            return ""

        def collect_artifacts(workspace, artifacts, receipt, target, *, bazel, options, **kwargs):
            self.assertFalse(any(arg.startswith("--test_tmpdir=") for arg in options))
            self.assertEqual(target, "//dockers/docker-orchagent:ci_artifacts")
            (workspace / "MODULE.bazel.lock").write_text("{}\n")
            receipt["artifacts"] = {"fixture.tar": {"sha256": "a" * 64}}
            return {"fixture.tar": "fixture.tar"}

        owner.command_log.execute = execute
        owner.build.collect_artifacts = collect_artifacts
        owner.resolution.collect = lambda *args, **kwargs: {"fixture": True}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {}, clear=False))
            if enabled:
                os.environ["RUNNER_TEMP"] = str(self.parent)
            else:
                os.environ.pop("RUNNER_TEMP", None)
            stack.enter_context(mock.patch.object(owner.platform, "machine", return_value="x86_64"))
            stack.enter_context(mock.patch.object(owner.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "trixie"}))
            query = stack.enter_context(mock.patch.object(owner, "query_tests", return_value=self.selected_tests))
            stack.enter_context(mock.patch.object(owner, "declared_sources", return_value=["MODULE.bazel"]))
            stack.enter_context(mock.patch.object(owner, "configured_test_labels", return_value=dict.fromkeys(self.selected_tests, "fixture")))
            outputs = stack.enter_context(mock.patch.object(owner, "collect_test_outputs", return_value=([], {})))
            try:
                owner.run("//dockers/docker-orchagent", workspace=self.workspace, artifacts=self.artifacts,
                          bazel="bazel-owner", options=["--jobs=1"], short_test_temp=enabled)
            finally:
                self.assertEqual(query.call_args.args[0], expected_suite)
                self.assertEqual(os.environ.get("HOME"), self.original_home)
                self.assertTrue(self.sentinel.is_file())
                self.assertTrue(all(not base.exists() for base in self.observed_bases))
                self.assertEqual(outputs.call_count, 0 if fail else 1)

    def test_default_keeps_local_calls_without_runner_temp_unchanged(self):
        self.exercise(enabled=False)
        receipt = json.loads((self.artifacts / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertNotIn("test_temporary_directory", receipt)
        self.assertEqual(set(receipt["tests"]), set(self.selected_tests))
        self.assertEqual(list(self.parent.iterdir()), [self.sentinel])

    def test_opt_in_only_changes_test_arguments_and_cleans_up(self):
        self.exercise(enabled=True)
        receipt = json.loads((self.artifacts / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["test_temporary_directory"]["cleanup_complete"])
        self.assertEqual(receipt["test_temporary_directory"]["per_test_isolation"], "bazel-managed")
        self.assertEqual(set(receipt["tests"]), set(self.selected_tests))
        self.assertEqual(len(self.observed_bases), 1)
        self.assertEqual(list(self.parent.iterdir()), [self.sentinel])

    def test_test_failure_propagates_and_removes_only_owned_base(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.exercise(enabled=True, fail=True)
        receipt = json.loads((self.artifacts / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["test_temporary_directory"]["cleanup_complete"])
        self.assertEqual(len(self.observed_bases), 1)
        self.assertEqual(list(self.parent.iterdir()), [self.sentinel])

    def test_cli_option_is_explicit_and_default_is_false(self):
        argv = ["--container", "//dockers/docker-orchagent", "--artifacts", str(self.artifacts)]
        with mock.patch.object(self.owner, "run") as run:
            self.owner.main(argv)
            self.assertIs(run.call_args.kwargs["short_test_temp"], False)
            self.owner.main([*argv, "--short-test-temp"])
            self.assertIs(run.call_args.kwargs["short_test_temp"], True)
            self.assertEqual(run.call_args.args, ("//dockers/docker-orchagent",))
        self.assertEqual(os.environ.get("HOME"), self.original_home)


if __name__ == "__main__":
    unittest.main()
