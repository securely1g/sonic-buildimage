"""Reject false cache-hit evidence without substituting Make or Bazel."""

import copy
import json
import os
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
    def test_managed_seed_requires_the_same_cache_and_runtime(self):
        selection = {"cache_mode": "runner-managed-anonymous", "cache_identity_sha256": "a" * 64,
                     "cache_runtime_sha256": "b" * 64}
        seed = {"passed": True, "mode": "make_seed", "architecture": "amd64", "yang": True,
                "phases": [{"phase": "seed"}], "common_revision": "c" * 40,
                "buildimage_revision": "d" * 40, **selection,
                "module_resolution": {"matched": True, "seed_module_count": 1, "shared_graph_sha256": "e" * 64}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(make_cache, "ROOT", Path(directory)), \
                patch.object(make_cache.managed, "enabled", return_value=True), \
                patch.object(make_cache.managed, "selection", return_value=selection):
            path = Path(directory) / make_cache.PUBLIC / "seed/cross-repo.json"
            path.parent.mkdir(parents=True)
            for name in make_cache.SEED_INPUTS:
                source = Path(directory) / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(name)
            seed["buildimage_inputs"] = make_cache.seed_inputs(Path(directory))
            path.write_text(json.dumps(seed))
            self.assertEqual(make_cache.seed_receipt("amd64")[0], seed)
            for field in selection:
                changed = dict(seed)
                changed.pop(field)
                path.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "same|this runner-managed"):
                    make_cache.seed_receipt("amd64")
            changed = dict(seed)
            changed.pop("module_resolution")
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "module resolution evidence"):
                make_cache.seed_receipt("amd64")
            path.write_text(json.dumps(seed))
            (Path(directory) / "MODULE.bazel").write_text("changed dependency graph")
            with self.assertRaisesRegex(ValueError, "dependency and toolchain inputs differ"):
                make_cache.seed_receipt("amd64")

    def test_managed_make_reuses_launcher_assets_without_legacy_auth(self):
        paths = ("/runtime/config", "/runtime/launcher")
        with patch.object(make_cache.managed, "enabled", return_value=True), \
                patch.object(make_cache.managed, "runtime_paths", return_value=paths), \
                patch.object(make_cache.managed, "downloader_option", return_value="--experimental_downloader_config=/runtime/mirrors.cfg"), \
                patch.dict(os.environ, {"BAZEL_REMOTE_AUTH": "private-test-value"}):
            command = make_cache.make_command("amd64", {"local_image": "image", "local_tag": "tag"}, Path("/tmp/cache"))
            extra = next(value for value in command if value.startswith("SONIC_BUILDER_EXTRA_CMDLINE="))
            self.assertIn("-e SONIC_BAZEL_MANAGED_CACHE", extra)
            self.assertIn("-e SONIC_BAZEL_DOWNLOADER_CONFIG", extra)
            for path in paths:
                self.assertIn("-v " + path + ":" + path + ":ro", extra)
            self.assertNotIn("private-test-value", extra)
            self.assertNotIn("BAZEL_REMOTE_AUTH", make_cache.make_environment())
            self.assertEqual(make_cache.bazel_invocation(["build", "//image"])[0], make_cache.managed.BAZEL)
            with self.assertRaises(ValueError):
                make_cache.bazel_invocation(["build", "--remote_cache=https://other.example/", "//image"])

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
