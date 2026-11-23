"""Exercise successful-baseline selection, transitive inputs and immutable VS profiles."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.ci import validation_changes as ci
from tools.bazel.ci import validation_profile as profile


class SourceSelectionTest(unittest.TestCase):
    def test_documentation_needs_no_product_validations(self):
        plan = ci.select(["doc/debian-source-selection.md", "README.buildsystem.md"])
        self.assertFalse(plan["full"])
        self.assertFalse(any(plan["groups"].values()))
        self.assertEqual((plan["images"], plan["components"]), ([], []))

    def test_shared_source_selects_owner_once_and_both_images(self):
        plan = ci.select(["src/libteam/BUILD.bazel", "src/libteam/configure.ac"])
        self.assertEqual(plan["images"], ["docker-orchagent", "docker-teamd"])
        self.assertEqual(plan["components"], ["libteam"])
        self.assertTrue(plan["groups"]["package_amd64"])
        self.assertFalse(plan["groups"]["kernel_arm64"])

    def test_component_checker_only_reruns_that_owner(self):
        plan = ci.select(["tools/bazel/ci/components/libteam.py"])
        self.assertEqual(plan["components"], ["libteam"])
        self.assertEqual(plan["images"], [])
        self.assertFalse(any(plan["groups"].values()))

    def test_all_consumers_of_changed_base_are_covered(self):
        plan = ci.select(["dockers/docker-config-engine-trixie/BUILD.bazel"])
        for image in ["docker-teamd", "docker-swss-layer-trixie", "docker-nat",
                      "docker-sonic-bmp", "docker-orchagent", "docker-config-engine-trixie"]:
            self.assertIn(image, plan["images"])
        self.assertNotIn("docker-base-trixie", plan["images"])

    def test_single_runtime_probe_change_is_scoped(self):
        plan = ci.select(["dockers/docker-dhcp-relay/bazel/runtime_probe.py"])
        self.assertEqual(plan["images"], ["docker-dhcp-relay"])
        self.assertFalse(any(plan["groups"].values()))

    def test_unknown_input_configuration_and_shared_workflow_force_full(self):
        for path in ["rules/config", "MODULE.bazel", "foo/new-input.yml", ".github/workflows/bazel-oci.yml"]:
            with self.subTest(path=path):
                plan = ci.select(["README.md", path])
                self.assertTrue(plan["full"])
                self.assertTrue(all(plan["groups"].values()))

    def test_diff_accounts_for_renames_and_deletions(self):
        with tempfile.TemporaryDirectory() as scratch:
            def git(*args):
                return subprocess.check_output(["git", "-C", scratch, *args])
            git("init", "-q")
            git("config", "user.email", "test@example.org")
            git("config", "user.name", "test")
            for name in ["old", "removed"]:
                Path(scratch, name).write_text(name)
            git("add", ".")
            git("commit", "-qm", "prior successful build")
            Path(scratch, "old").rename(Path(scratch, "new"))
            Path(scratch, "removed").unlink()
            git("add", "-A")
            git("commit", "-qm", "move and delete")
            previous = os.getcwd()
            try:
                os.chdir(scratch)
                self.assertEqual(sorted(ci.changes("HEAD^", "HEAD")), ["new", "old", "removed"])
            finally:
                os.chdir(previous)

    def test_filter_preserves_immutable_component_artifact_identity(self):
        matrix = {"include": [{"component": "libteam", "artifact_id": "42", "debug": "n"},
                              {"component": "fib", "artifact_id": "99", "debug": "n"}]}
        plan = ci.select(["src/libteam/verify_runtime.py"])
        self.assertEqual(ci.filter_matrix(plan, matrix, "component"), {"include": [matrix["include"][0]]})
        self.assertEqual(ci.filter_matrix(ci.full("no baseline"), matrix, "component"), matrix)


class MasterBaselineTest(unittest.TestCase):
    def setUp(self):
        self.repo = "example/buildimage"
        self.sha = "a" * 40
        self.run = {"workflow_id": 11, "id": 22, "run_number": 7, "run_attempt": 2,
                    "head_sha": self.sha, "head_branch": "master", "path": ci.WORKFLOW,
                    "status": "completed", "conclusion": "success", "event": "push",
                    "repository": {"full_name": self.repo}, "head_repository": {"full_name": self.repo}}
        self.jobs = [{"name": name, "conclusion": "success"} for name in
                     ["VS installer in SONiC slave (AMD64)", "Container archive (AMD64)"]]
        self.artifact = {"id": 41, "name": "validation-profile-v1-2", "expired": False, "size_in_bytes": 578}

    def find(self, ancestor=True):
        def api(endpoint):
            if endpoint.startswith("workflows/bazel-oci.yml"):
                return {"id": 11, "path": ci.WORKFLOW}
            if endpoint.startswith("workflows/11/runs?"):
                return {"workflow_runs": [self.run]}
            if endpoint.startswith("runs/22/jobs?"):
                return {"jobs": self.jobs}
            if endpoint.startswith("runs/22/artifacts?name=validation-profile-v1-2"):
                return {"artifacts": [self.artifact]}
            self.fail(endpoint)
        return ci.baseline(api, self.repo, 8, lambda commit: ancestor and commit == self.sha)

    def test_accepts_successful_prior_master_run_with_profile_and_gate(self):
        self.assertEqual(self.find(), (self.sha, 41))
        self.assertIsNone(self.find(ancestor=False))

    def test_rejects_superseded_attempt_profiles_and_skipped_acceptance(self):
        self.artifact["name"] = "validation-profile-v1-1"
        self.assertIsNone(self.find())
        self.artifact["name"] = "validation-profile-v1-2"
        self.jobs[1]["conclusion"] = "skipped"
        self.assertIsNone(self.find())

    def test_rejects_untrusted_or_failed_runs(self):
        for field, value in [("event", "pull_request"), ("head_branch", "feature"), ("workflow_id", 9),
                             ("head_repository", {"full_name": "fork/buildimage"}), ("conclusion", "failure")]:
            with self.subTest(field=field), patch.dict(self.run, {field: value}):
                self.assertIsNone(self.find())

    def test_api_unavailability_schedules_everything(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_NUMBER": "8",
               "GH_TOKEN": "unused", "GITHUB_REPOSITORY": self.repo}
        with patch.dict(os.environ, env), patch.object(ci, "baseline", side_effect=OSError):
            self.assertTrue(ci.plan_run()["full"])


class ProfileComparisonTest(unittest.TestCase):
    def test_stamp_changes_do_not_hide_a_startup_or_layer_change(self):
        def signed(version, cmd):
            return {"rootfs": {"diff_ids": ["abc"]}, "config": {"Labels": {"Tag": version},
                    "Env": ["IMAGE_VERSION=" + version, "X=" + cmd], "Cmd": [cmd]}}
        first = profile.image_config(signed("v1", "test"), "v1")
        self.assertEqual(first, profile.image_config(signed("v2", "test"), "v2"))
        self.assertNotEqual(first, profile.image_config(signed("v2", "different"), "v2"))
        with self.assertRaisesRegex(ValueError, "Tag"):
            profile.image_config(signed("wrong", "test"), "v2")

    def test_complete_producer_profile_tracks_wheel_kernel_fixture_and_optional_features(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            containers = root / "containers"
            image = containers / "docker-teamd"
            owner = containers / "components" / "libteam"
            wheels = root / "python-packages"
            kernel = root / "kernel-amd64"
            for directory in [image, owner, wheels, kernel]:
                directory.mkdir(parents=True)
            sha = "a" * 40
            version = "test-0.1"
            def write(path, data):
                path.write_text(json.dumps(data))
            selection = {"revision": sha, "build_version": version, "architecture": "amd64",
                         "debug_enabled": False, "installer_archives": ["docker-teamd.gz"],
                         "auxiliary_parents": [], "components": ["libteam"],
                         "registered_debug": ["docker-teamd-dbg.gz"], "validation_only_debug": []}
            write(containers / "selection.json", selection)
            archive = image / "docker-teamd.gz"
            archive.write_bytes(b"compressed")
            make_env = image / "runtime.make.image.env"
            make_env.write_text("IMAGE_VERSION=" + version + "\n")
            (image / "runtime.oci.json.gz").write_bytes(b"inventory")
            (image / "probe").write_bytes(b"original")
            bundle = {"revision": sha, "image": "docker-teamd", "build_version": version,
                      "architecture": "amd64", "debug_enabled": False, "supports_debug": True,
                      "archives": {"runtime": {"validation_config": "config", "layers": ["layer"],
                                               "file": archive.name, "size": archive.stat().st_size}},
                      "fixtures": {name: profile.sha(image / name) for name in
                                   ("runtime.oci.json.gz", make_env.name, "probe")}}
            write(image / "bundle.json", bundle)
            write(owner / "manifest.json", {"component": "libteam", "revision": sha, "architecture": "amd64",
                  "debug": False, "images": ["docker-teamd"],
                  "build_settings": {"//tools/bazel/oci:image_version": version}, "files": {"library": "sha"}})
            packages = dict(profile.wheels.package_targets(False))
            write(wheels / "manifest.json", {"revision": sha, "packages": packages, "pddf_enabled": False})
            write(kernel / "vs-kernel-handoff.json", {"vs_revision": sha, "summary": {"artifacts": ["kernel"]}})
            old = profile.create(containers)
            old_tree = packages[profile.wheels.YANG_TREE]
            packages[profile.wheels.YANG_TREE] = "different-model-tree"
            write(wheels / "manifest.json", {"revision": sha, "packages": packages, "pddf_enabled": False})
            tree_changed = profile.create(containers)
            self.assertEqual({k for k in old["groups"] if old["groups"][k] != tree_changed["groups"][k]},
                             {"archive", "package_amd64", "yang_models"})
            packages[profile.wheels.YANG_TREE] = old_tree

            # These producer files include the VS stamp. Check them against this build
            # before normalizing that stamp for comparison with the previous run.
            original_env = make_env.read_bytes()
            make_env.write_text("IMAGE_VERSION=new-stamp\n")
            bundle["fixtures"][make_env.name] = profile.sha(make_env)
            write(image / "bundle.json", {**bundle, "build_version": "new-stamp"})
            self.assertEqual(old["images"]["docker-teamd"], profile.image_inputs(
                image, {**selection, "build_version": "new-stamp"}, "docker-teamd"))
            make_env.write_bytes(original_env)
            bundle["fixtures"][make_env.name] = profile.sha(make_env)
            (image / "probe").write_bytes(b"changed")
            bundle["fixtures"]["probe"] = profile.sha(image / "probe")
            write(image / "bundle.json", bundle)
            packages[profile.wheels.SCAPY_WHEEL] = "different-wheel"
            write(wheels / "manifest.json", {"revision": sha, "packages": packages, "pddf_enabled": False})
            write(kernel / "vs-kernel-handoff.json", {"vs_revision": sha, "summary": {"artifacts": ["other-kernel"]}})
            changed = profile.create(containers)
            self.assertNotEqual(old["images"], changed["images"])
            self.assertEqual(old["components"], changed["components"])
            self.assertEqual({k for k in old["groups"] if old["groups"][k] != changed["groups"][k]},
                             {"package_amd64", "kernel_packages_amd64"})
            for field, value in (("image", "docker-other"), ("architecture", "arm64"), ("revision", "b" * 40),
                                 ("build_version", "old"), ("debug_enabled", True), ("supports_debug", False)):
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "VS (bundle|debug)"):
                    write(image / "bundle.json", {**bundle, field: value})
                    profile.create(containers)
            write(image / "bundle.json", {**bundle, "debug_enabled": True})
            with self.assertRaisesRegex(ValueError, "variants"):
                profile.image_inputs(image, {**selection, "debug_enabled": True}, "docker-teamd")
            write(image / "bundle.json", bundle)
            (image / "probe").write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "fixture checksum"):
                profile.create(containers)
            (image / "probe").write_bytes(b"changed")
            archive.write_bytes(b"truncated")
            with self.assertRaisesRegex(ValueError, "archive size"):
                profile.create(containers)
            archive.unlink()
            with self.assertRaisesRegex(ValueError, "Missing VS artifact file"):
                profile.create(containers)

    def test_profile_changes_select_outputs_even_when_git_inputs_are_unchanged(self):
        old = {"schema": 1, "architecture": "amd64", "debug": False, "revision": "a" * 40,
               "images": {"docker-sflow": "first"}, "components": {"libteam": "first"},
               "groups": {"archive": "first"}}
        new = {**old, "revision": "b" * 40, "components": {"libteam": "second"}, "groups": {"archive": "second"}}
        plan = {**ci.select(["README.md"]), "baseline": old["revision"]}
        with patch.dict(os.environ, GITHUB_SHA=new["revision"]):
            refined = ci.refine(plan, old, new)
            self.assertFalse(refined["full"])
            self.assertEqual(refined["components"], ["libteam"])
            self.assertEqual(refined["images"], ["docker-orchagent", "docker-teamd"])
            self.assertTrue(refined["groups"]["archive"])
            for varying in [{"debug": True}, {"images": {}}, {"architecture": "arm64"}]:
                self.assertTrue(ci.refine(plan, old, {**new, **varying})["full"])

    def test_standalone_dispatch_remains_full(self):
        with patch.dict(os.environ, GITHUB_EVENT_NAME="workflow_dispatch"):
            self.assertTrue(ci.plan_run()["full"])


if __name__ == "__main__":
    unittest.main()
