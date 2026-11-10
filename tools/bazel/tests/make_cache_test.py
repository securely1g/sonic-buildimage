"""Reject false cache-hit evidence without substituting Make or Bazel."""

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import make_cache


def action(name="common", *, hit=True):
    return {"mnemonic": "CppCompile", "target": "@@sonic-swss-common+//:" + name,
            "outputs": ["bazel-out/native/" + name + ".o"], "digest": "a" * 64,
            "runner": "remote cache hit" if hit else "linux-sandbox", "cache_hit": hit}


class MakeCacheTest(unittest.TestCase):
    def test_self_hosted_builder_probes_succeed_without_host_policy_and_fail_on_denial(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / make_cache.PRIVATE).mkdir(parents=True)
            stack.enter_context(patch.multiple(make_cache, ROOT=root, INSTALL_BASE=root))
            stack.enter_context(patch.object(make_cache, "require_native_builder"))
            stack.enter_context(patch.object(make_cache.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(make_cache, "command"))
            stack.enter_context(patch.object(make_cache, "common_helpers"))
            stack.enter_context(patch.object(make_cache.make_cache_sandbox, "restriction_value", return_value="1"))
            child = {"uid": 1000, "profile": "unconfined", "sandbox_profile": "unconfined",
                     "namespaces": {"user": "new-user", "mnt": "new-mount"}}
            sandbox = root / "linux-sandbox"
            sandbox.write_text("#!/bin/sh\n[ ! -f deny ] || exit 23\necho '" + json.dumps(child) + "'\n")
            sandbox.chmod(0o755)

            for phase in ("before", "after"):
                with self.subTest(phase=phase):
                    make_cache.sandbox_probe("arm64", phase)
                    path = root / make_cache.PUBLIC / ("sandbox-probe-" + phase + ".json")
                    receipt = json.loads(path.read_text())
                    self.assertTrue(receipt["passed"])
                    self.assertFalse(receipt["host_profile_installed"])
                    self.assertEqual(receipt["restriction_before"], "1")
                    self.assertEqual(receipt["restriction_after"], "1")
            (root / "deny").touch()
            for phase in ("before", "after"):
                with self.subTest(denied=phase), self.assertRaisesRegex(ValueError, "Linux sandbox"):
                    make_cache.sandbox_probe("arm64", phase)
                path = root / make_cache.PUBLIC / ("sandbox-probe-" + phase + ".json")
                receipt = json.loads(path.read_text())
                self.assertFalse(receipt["passed"])
                self.assertEqual(receipt["returncode"], 23)

    def test_self_hosted_sandbox_baseline_requires_the_same_native_probe(self):
        receipt = {"architecture": "arm64", "restriction_before": "1", "sandbox_sha256": "a" * 64}
        baseline = {**receipt, "passed": True}
        make_cache.verify_sandbox_baseline(baseline, receipt)
        for field, value in (("passed", False), ("architecture", "amd64"),
                             ("restriction_before", "0"), ("sandbox_sha256", "b" * 64)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_cache.verify_sandbox_baseline({**baseline, field: value}, receipt)

    def test_seed_may_have_extra_actions(self):
        make_cache.compare_image_actions(
            {"common_actions": [action(hit=False), action("extra", hit=False)]},
            {"common_actions": [action()]})

    def test_rejects_missing_changed_duplicate_or_local_image_actions(self):
        seed = {"common_actions": [action(hit=False)]}
        changed = action()
        changed["digest"] = "b" * 64
        for rows in ([], [action("absent")], [changed], [action(), action()], [action(hit=False)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                make_cache.compare_image_actions(seed, {"common_actions": rows})

    def test_target_and_output_are_part_of_identity(self):
        for field, value in (("target", "@@sonic-swss-common+//:other"),
                             ("outputs", ["bazel-out/exec/common.o"])):
            changed = copy.deepcopy(action())
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_cache.compare_image_actions({"common_actions": [action(hit=False)]},
                                                 {"common_actions": [changed]})

    def test_rejects_existing_output_lock_image_and_broken_bazel_symlink(self):
        for name in ("artifacts/private/read-output", "artifacts/private/bazel-install",
                     "MODULE.bazel.lock", "target", ".platform"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                make_cache.require_fresh_workspace(root)
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                with self.assertRaises(ValueError):
                    make_cache.require_fresh_workspace(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bazel-bin").symlink_to(root / "missing")
            with self.assertRaises(ValueError):
                make_cache.require_fresh_workspace(root)

    def test_native_architecture_does_not_accept_emulation(self):
        for architecture, machine in (("amd64", "x86_64"), ("arm64", "aarch64")):
            make_cache.native_architecture(architecture, machine)
        with self.assertRaises(ValueError):
            make_cache.native_architecture("arm64", "x86_64")

    def test_query_cannot_overwrite_image_execution_log(self):
        build = make_cache.bazel_invocation(["build", "//image"])
        query = make_cache.bazel_invocation(["cquery", "--output=files", "//image"])
        self.assertEqual(sum(arg.startswith("--execution_log_json_file=") for arg in build), 1)
        self.assertFalse(any(arg.startswith("--execution_log_json_file=") for arg in query))
        for argv in (build, query):
            self.assertIn("--batch", argv)
            self.assertIn("--nosystem_rc", argv)
            self.assertIn("--nohome_rc", argv)
            self.assertIn("--output_base=" + str(make_cache.OUTPUT_BASE), argv)
            self.assertIn("--install_base=" + str(make_cache.INSTALL_BASE), argv)

    def test_native_manifest_ignores_attestations_and_other_architecture(self):
        manifest = {"manifests": [
            {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:" + "3" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
        ]}
        self.assertEqual(make_cache.native_manifest(manifest, "arm64"), "sha256:" + "2" * 64)
        manifest["manifests"].append(manifest["manifests"][1])
        with self.assertRaises(ValueError):
            make_cache.native_manifest(manifest, "arm64")


if __name__ == "__main__":
    unittest.main()
