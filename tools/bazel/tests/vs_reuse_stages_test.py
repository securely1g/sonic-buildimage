#!/usr/bin/env python3
"""Pure stage/custody fixtures. No planner, Make, daemon, namespace or network call."""
from contextlib import ExitStack
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import vs_reuse_stages as stages


class StageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "assembly"
        self.records = self.workspace / "artifacts/vs"
        self.records.mkdir(parents=True)
        self.context = dict(controller=self.root / "controller", workspace=self.workspace,
                            manifest=self.root / "inventory.json", manifest_sha256="a" * 64,
                            merge="b" * 40, source_head="d" * 40, source_tree="e" * 40, producer=self.root / "producer",
                            registry=self.root / "registry", receipt=self.root / "capture.json",
                            receipt_sha256="c" * 64, python="/usr/bin/python3",
                            make_inputs=self.workspace / "target/sonic-vs.bin.make-inputs.tsv",
                            make_inputs_sha256="e" * 64)
        self.before = b'{"schema":1,"stage":"before-daemon","status":"prepared"}\n'
        self.storage = b'{"schema":1,"status":"passed","working_root":"/actions/_work/private"}\n'
        (self.records / "slave-preparation.json").write_bytes(self.before)
        (self.records / "k3s-storage.json").write_bytes(self.storage)
        # Only metadata is mocked for platform portability; reads, no-follow opens,
        # exclusive writes and exact original byte custody are exercised for real.
        self.original_fstat = os.fstat
        self.original_lstat = Path.lstat
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                  "st_size", "st_mtime_ns", "st_ctime_ns")
        def worker(row):
            value = {name: getattr(row, name) for name in fields}
            value["st_uid"] = 1000
            return SimpleNamespace(**value)
        self.worker = worker
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(stages.os, "getuid", return_value=1000))
        stack.enter_context(mock.patch.object(stages.os, "geteuid", return_value=1000))
        stack.enter_context(mock.patch.object(stages.os, "fstat", side_effect=lambda fd: worker(self.original_fstat(fd))))
        original_lstat = self.original_lstat
        stack.enter_context(mock.patch.object(Path, "lstat", new=lambda path: worker(original_lstat(path))))

    def test_each_planner_mode_uses_new_isolated_cli(self):
        for mode in ("inputs", "admission", "environment", "metadata", "plan", "verify"):
            argv = stages.planner_command(mode, **self.context)
            self.assertEqual(argv[:4], ["/usr/bin/python3", "-I", "-S", "-B"])
            self.assertEqual(argv[5], mode)
            self.assertEqual("--require-environment" in argv, mode in {"metadata", "plan", "verify"})
            self.assertNotIn("-c", argv)

    def test_original_kernel_workspaces_cannot_be_omitted(self):
        for mode in ("metadata", "plan"):
            for field in ("producer", "registry"):
                with self.subTest(mode=mode, field=field), self.assertRaises(ValueError):
                    stages.planner_command(mode, **dict(self.context, **{field: None}))

    def test_capture_identity_is_required_for_verify(self):
        for changes in ({"receipt": None}, {"receipt_sha256": None}, {"receipt_sha256": "c" * 63}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                stages.planner_command("verify", **dict(self.context, **changes))

    def test_frontend_argv_carries_no_shell_argument_or_second_daemon(self):
        before = stages.frontend_phase(self.context["controller"], "before-daemon")
        self.assertEqual(before["argv"][:3], ["/bin/bash", "--noprofile", "--norc"])
        self.assertEqual(len(before["argv"]), 4)
        self.assertTrue(before["worker_user"])
        self.assertEqual(before["environment"]["SONIC_VS_REUSE_ONLY"], "y")
        with self.assertRaises(ValueError):
            stages.frontend_phase(self.context["controller"], "after-daemon")

    def test_original_bytes_survive_finish_overwrite(self):
        destination = self.root / "first-phase"
        receipt = stages.retain_preparation(self.workspace, destination)
        (self.records / "slave-preparation.json").write_text('{"stage":"complete","status":"passed"}\n')
        self.assertEqual((destination / "slave-preparation.json").read_bytes(), self.before)
        self.assertEqual((destination / "k3s-storage.json").read_bytes(), self.storage)
        self.assertEqual(receipt["environment"]["SONIC_VS_PREPARATION_SHA256"], hashlib.sha256(self.before).hexdigest())
        self.assertFalse(receipt["assembly_or_native_admission"])
        after = stages.frontend_phase(self.context["controller"], "after-daemon", receipt)
        self.assertEqual(after["environment"]["SONIC_VS_STORAGE_SHA256"], hashlib.sha256(self.storage).hexdigest())

    def test_existing_custody_directory_is_preserved(self):
        destination = self.root / "first-phase"
        destination.mkdir()
        sentinel = destination / "keep"
        sentinel.write_bytes(b"original")
        with self.assertRaises(ValueError):
            stages.retain_preparation(self.workspace, destination)
        self.assertEqual(sentinel.read_bytes(), b"original")

    def test_failed_or_wrong_phase_cannot_start_next_phase(self):
        for change in ({"stage": "before-daemon", "status": "failed"},
                       {"stage": "complete", "status": "passed"}):
            (self.records / "slave-preparation.json").write_text(json.dumps(change))
            with self.assertRaises(ValueError):
                stages.retain_preparation(self.workspace, self.root / "no-output")
            self.assertFalse((self.root / "no-output").exists())

    def test_failed_storage_cannot_be_retained_as_ready(self):
        (self.records / "k3s-storage.json").write_text('{"status":"failed"}')
        with self.assertRaises(ValueError):
            stages.retain_preparation(self.workspace, self.root / "no-output")
        self.assertFalse((self.root / "no-output").exists())

    def test_symlink_and_oversized_preparation_refused(self):
        source = self.records / "slave-preparation.json"
        source.unlink()
        source.symlink_to(self.records / "k3s-storage.json")
        with self.assertRaises(ValueError):
            stages.stable_record(source)
        source.unlink()
        source.write_bytes(b"x" * (stages.CAP + 1))
        with self.assertRaises(ValueError):
            stages.stable_record(source)

    def test_changed_file_metadata_refuses_original_bytes(self):
        source = self.records / "slave-preparation.json"
        before = self.worker(self.original_lstat(source))
        after = copy.copy(before)
        after.st_mtime_ns += 1
        with mock.patch.object(stages.os, "fstat", side_effect=[before, after]), self.assertRaises(ValueError):
            stages.stable_record(source)

    def test_strict_record_json_and_tampered_resume_hash_refused(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                stages.decode(body)
        receipt = stages.retain_preparation(self.workspace, self.root / "first-phase")
        receipt["environment"]["SONIC_VS_PREPARATION_SHA256"] = "0" * 64
        with self.assertRaises(ValueError):
            stages.frontend_phase(self.context["controller"], "after-daemon", receipt)

    def test_postchecks_are_existing_nongenerating_interfaces(self):
        commands = stages.postbuild_commands(self.context, tested_merge="b" * 40,
                                              expected_short_commit="b" * 7, profile_paths=stages.POLICIES)
        self.assertEqual([x["label"] for x in commands], ["post-archive-verify", "installer-version", "installed-images"])
        self.assertEqual(commands[0]["argv"], stages.planner_command("verify", **self.context))
        self.assertEqual(commands[1]["argv"][:3], ["/usr/bin/python3", "-I", "-B"])
        self.assertIn("--installer", commands[1]["argv"])
        self.assertEqual(commands[2]["argv"].count("--images-policy"), 2)
        for row in commands:
            self.assertNotIn("make", row["argv"])
            self.assertNotIn("build", row["argv"])
            self.assertFalse(any("verify_vs_oci.sh" in x for x in row["argv"]))

    def test_unbound_policies_or_source_commit_refused(self):
        for kwargs in (dict(tested_merge="b" * 40, expected_short_commit="e" * 7, profile_paths=stages.POLICIES),
                       dict(tested_merge="b" * 40, expected_short_commit="b" * 7, profile_paths=stages.POLICIES[:1])):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                stages.postbuild_commands(self.context, **kwargs)

    def test_actual_frontend_finish_status_is_accepted_only_on_success(self):
        shell = (Path(__file__).resolve().parents[3] / "tools/bazel/ci/prepare_vs_slave.sh").read_text()
        marker = '    python3 - "$artifacts/slave-preparation.json" "$stage" "$status" ' + "<<'PY'\n"
        fragment = shell.split(marker, 1)[1].split("\nPY\n", 1)[0]
        program = compile(ast.parse(fragment), "<actual-frontend-finish-fixture>", "exec")
        filename = self.records / "slave-preparation.json"
        for status in (0, 3):
            filename.write_bytes(self.before)
            with self.subTest(status=status), mock.patch.object(sys, "argv", ["fixture", str(filename), "before-daemon", str(status)]):
                exec(program, {"__name__": "__fixture__"})
            actual = filename.read_bytes()
            self.assertEqual(json.loads(actual)["status"], "prepared" if status == 0 else "failed")
            destination = self.root / ("actual-first-phase-" + str(status))
            if status == 0:
                receipt = stages.retain_preparation(self.workspace, destination)
                self.assertEqual((destination / filename.name).read_bytes(), actual)
                self.assertEqual(receipt["files"][filename.name]["sha256"], hashlib.sha256(actual).hexdigest())
                stages.frontend_phase(self.context["controller"], "after-daemon", receipt)
            else:
                with self.assertRaises(ValueError):
                    stages.retain_preparation(self.workspace, destination)
                self.assertFalse(destination.exists())

    def test_old_synthetic_passed_status_is_not_frontend_preparation(self):
        filename = self.records / "slave-preparation.json"
        filename.write_text('{"stage":"before-daemon","status":"passed"}\n')
        destination = self.root / "wrong-status"
        with self.assertRaises(ValueError):
            stages.retain_preparation(self.workspace, destination)
        self.assertFalse(destination.exists())

    def test_dangling_or_existing_postcheck_output_is_preserved(self):
        output = self.records / "sonic-version.json"
        output.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            stages.postbuild_commands(self.context, tested_merge="b" * 40,
                                     expected_short_commit="b" * 7, profile_paths=stages.POLICIES)
        self.assertTrue(output.is_symlink())


class AssemblyCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.revision, self.tree = "a" * 40, "b" * 40
        self.native = SimpleNamespace(source_check=mock.Mock(), run=mock.Mock())
        self.observe()

    def observe(self, *, tree=None, submodules=b"", untracked=b""):
        self.native.run.side_effect = [
            SimpleNamespace(stdout=((tree or self.tree) + "\n").encode()),
            SimpleNamespace(stdout=submodules), SimpleNamespace(stdout=untracked),
        ]

    def verify(self, **kwargs):
        return stages.verify_assembly_checkout(self.native, self.root,
            revision=self.revision, tree=self.tree, **kwargs)

    def test_actual_tree_and_recursive_submodules_with_exact_artifacts(self):
        self.observe(submodules=b" " + b"c" * 40 + b" src/library (v1)\n",
                     untracked=b"target/a.deb\0artifacts/vs/input.json\0")
        result = self.verify(allowed_untracked=["target/a.deb", "artifacts/vs/input.json"])
        self.assertEqual(result["status"], "CHECKOUT_VERIFIED_ONLY")
        self.assertFalse(result["native_acceptance_or_assembly"])
        self.assertEqual(result["untracked_inputs"], 2)
        self.assertEqual(self.native.source_check.call_args_list,
                         [mock.call(self.root, self.revision), mock.call(self.root, self.revision)])

    def test_core_dirty_or_wrong_head_failure_stops_before_other_observations(self):
        self.native.source_check.side_effect = RuntimeError("dirty or wrong HEAD")
        with self.assertRaisesRegex(RuntimeError, "dirty"):
            self.verify()
        self.native.run.assert_not_called()

    def test_declared_tree_does_not_replace_actual_checkout_tree(self):
        self.observe(tree="c" * 40)
        with self.assertRaisesRegex(ValueError, "tree differs"):
            self.verify()

    def test_uninitialized_changed_and_conflicted_submodules_are_refused(self):
        for prefix in (b"-", b"+", b"U"):
            self.observe(submodules=prefix + b"c" * 40 + b" src/library\n")
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "submodule"):
                self.verify()

    def test_ignored_python_bytecode_is_not_an_admitted_import(self):
        self.observe(untracked=b"tools/bazel/ci/__pycache__/worker.cpython-313.pyc\0")
        with self.assertRaisesRegex(ValueError, "Unadmitted"):
            self.verify()

    def test_unexpected_make_dependency_fragment_is_not_an_admitted_input(self):
        self.observe(untracked=b"target/debs/trixie/a.dep\0")
        with self.assertRaisesRegex(ValueError, "Unadmitted"):
            self.verify()

    def test_manifest_cannot_allow_untracked_code_or_make_cache_fragments(self):
        for name in ("tools/bazel/ci/worker.py", "files/build_templates/organization_extensions.sh",
                     "rules/config.user", "target/debs/trixie/a.dep", "target/../rules/config.user"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "artifact data"):
                self.verify(allowed_untracked=[name])
        self.native.source_check.assert_not_called()

    def test_untracked_output_must_be_complete_and_unique(self):
        for body in (b"target/a.deb", b"target/a.deb\0target/a.deb\0"):
            self.observe(untracked=body)
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.verify(allowed_untracked=["target/a.deb"])

    def test_changed_checkout_at_end_is_not_reported_as_verified(self):
        self.native.source_check.side_effect = [None, RuntimeError("checkout changed")]
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.verify()

    def test_symlink_checkout_is_refused_before_git(self):
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            stages.verify_assembly_checkout(self.native, link, revision=self.revision, tree=self.tree)
        self.native.source_check.assert_not_called()

    def test_absent_source_tree_identity_is_refused(self):
        with self.assertRaisesRegex(ValueError, "commit or tree"):
            stages.verify_assembly_checkout(self.native, self.root, revision=self.revision, tree=None)
        self.native.source_check.assert_not_called()

    def test_observation_is_bounded_and_does_not_accept_decoded_text(self):
        for body in (b"x" * (stages.CAP + 1), self.tree + "\n"):
            self.native.run.side_effect = [SimpleNamespace(stdout=body)]
            with self.subTest(body_type=type(body).__name__), self.assertRaisesRegex(ValueError, "observation"):
                self.verify()



class FullVsWorkloadTests(unittest.TestCase):
    def fixture(self, *, fail_label=None, plan_status="CONFIGURED_INPUTS_VERIFIED_COMMANDS_ONLY",
                tested_merge="b" * 40, tested_tree="d" * 40, tested_base="1" * 40):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "assembly"
        workspace.mkdir()
        context = dict(workspace=str(workspace), manifest=str(root / "inputs.json"),
                       manifest_sha256="a" * 64, merge=tested_merge,
                       producer=str(root / "producer"), registry=str(root / "registry"),
                       native_controller=str(root / "native"), source_head="c" * 40,
                       source_tree=tested_tree, expected_short_commit=tested_merge[:7],
                       manager_revision="e" * 40,
                       profile_paths=list(stages.POLICIES))
        paths = {"state": root / "state",
                 "identity": {"worker": "fixture-k3s", "run": "123", "attempt": "1"}}
        paths["state"].mkdir()
        endpoint = {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONFIG": str(root / "client")}
        policy = dict(BAZEL="/usr/bin/false", BAZEL_REAL="/usr/bin/false",
                      SONIC_DPKG_CACHE_METHOD="none", SONIC_DPKG_CACHE_METHOD_OVERRIDE="none",
                      SONIC_BAZEL_CACHE_POLICY="remote-only", SONIC_DEB_BUILD_POLICY="reuse-only")
        retained = {"status": "FIRST_PHASE_BYTES_RETAINED_ONLY",
                    "files": {name: {"bytes": 1, "sha256": digest} for name, digest in
                              (("slave-preparation.json", "e" * 64), ("k3s-storage.json", "f" * 64))},
                    "environment": {"SONIC_VS_PREPARATION_SHA256": "e" * 64,
                                    "SONIC_VS_STORAGE_SHA256": "f" * 64}}
        checkout = {"revision": tested_merge, "tree": tested_tree}
        assembly = {"commit": tested_merge, "tree": tested_tree}
        native_source = {"head": "c" * 40, "base": tested_base, "merge": tested_merge, "tree": tested_tree}
        plan = {"status": plan_status, "assembly": assembly,
                "kernel_verify_argv": ["make", "bazel-kernel-ensure", "SONIC_BAZEL_KERNEL_PREPARE=verify"],
                "build_argv": ["make", "target/sonic-vs.bin"],
                "original_archive_capture": {"sha256": "1" * 64},
                "configured_packages": {"fixture": True}, "configured_sources": {"fixture": True}}
        observations = []
        def bounded(argv, state, label, seconds, environment=None, **kwargs):
            observations.append(dict(label=label, argv=argv, environment=dict(environment or {}), **kwargs))
            if label == fail_label:
                raise ValueError("demonstrated fixture failure")
            outputs = {
                "full-vs-environment": dict(policy, SONIC_IMAGE_VERSION="bazel-ci"),
                "full-vs-retain-preparation": retained,
                "full-vs-metadata-command": {"status": "METADATA_COMMAND_ONLY",
                                             "argv": ["make", "vs-reuse-selection"]},
                "full-vs-plan": plan,
            }
            if label == "full-vs-assembly":
                return {"stdout": {"fixture": True}, "stderr": {"fixture": True}}
            return json.dumps(outputs[label]).encode() if label in outputs else b""
        runtime = SimpleNamespace(ROOT=root / "controller", PATH="/usr/bin:/bin",
                                  FULL_VS_BODY_SECONDS=7200, bounded=bounded)
        def stable(filename, *, json_record=True):
            if filename.name == "k3s-storage.json":
                return b"storage", {"assembly_checkout": checkout, "native_admission": {"assembly": assembly, "source": native_source}}
            if filename.name == "sonic-vs-slave.env.json":
                return b"environment", {"schema": 1, "unset": ["PLATFORM"],
                                        "environment": dict(policy, **endpoint)}
            self.assertFalse(json_record)
            self.assertEqual(filename, workspace / "target/sonic-vs.bin.make-inputs.tsv")
            return b"actual fixture capture bytes\n", None
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(stages, "stable_record", side_effect=stable))
        stack.enter_context(mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir="/home/runner")))
        return stages.full_vs_workload(runtime, context), paths, endpoint, observations

    def test_capture_and_kernel_verify_precede_assembly_and_postchecks(self):
        workload, paths, endpoint, calls = self.fixture()
        self.assertEqual(workload("before-daemon", "/usr/bin/docker", None, paths)["status"], "passed")
        result = workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        self.assertEqual(result["status"], "passed")
        labels = [call["label"] for call in calls]
        self.assertEqual(labels, ["full-vs-environment", "full-vs-before-daemon",
            "full-vs-retain-preparation", "full-vs-after-daemon", "full-vs-metadata-command",
            "full-vs-metadata", "full-vs-plan", "full-vs-kernel-verify", "full-vs-assembly",
            "full-vs-post-archive-verify", "full-vs-installer-version", "full-vs-installed-images"])
        command = next(call["argv"] for call in calls if call["label"] == "full-vs-plan")
        self.assertEqual(command[command.index("--make-inputs-sha256") + 1],
                         hashlib.sha256(b"actual fixture capture bytes\n").hexdigest())
        assembly = next(call for call in calls if call["label"] == "full-vs-assembly")
        self.assertTrue(assembly["private_log_records"])
        self.assertTrue(all(call["worker_user"] for call in calls))
        self.assertTrue(all(call["working_directory"] == calls[0]["working_directory"] for call in calls))
        self.assertEqual(assembly["environment"]["BAZEL"], "/usr/bin/false")
        self.assertTrue(all(call["environment"]["EXPECTED_MANAGER_REVISION"] == "e" * 40 for call in calls))


    def test_advanced_base_caller_keeps_head_merge_tree_and_installer_version_distinct(self):
        workload, paths, endpoint, calls = self.fixture(
            tested_merge="f" * 40, tested_tree="9" * 40, tested_base="8" * 40)
        first = workload("before-daemon", "/usr/bin/docker", None, paths)
        self.assertEqual(first["source_checkout"], {"revision": "f" * 40, "tree": "9" * 40})
        self.assertEqual(first["native_admission"]["source"],
                         {"head": "c" * 40, "base": "8" * 40, "merge": "f" * 40, "tree": "9" * 40})
        workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        for call in calls:
            self.assertEqual(call["environment"]["VS_SOURCE_HEAD"], "c" * 40)
            self.assertEqual(call["environment"]["VS_SOURCE_MERGE"], "f" * 40)
            self.assertEqual(call["environment"]["VS_SOURCE_TREE"], "9" * 40)
        check = next(call["argv"] for call in calls if call["label"] == "full-vs-installer-version")
        self.assertEqual(check[check.index("--expected-commit-id") + 1], "f" * 7)
        check = next(call["argv"] for call in calls if call["label"] == "full-vs-post-archive-verify")
        self.assertEqual(check[check.index("--source-head") + 1], "c" * 40)
        self.assertEqual(check[check.index("--merge") + 1], "f" * 40)

    def test_configured_graph_failure_stops_before_kernel_or_build(self):
        workload, paths, endpoint, calls = self.fixture(fail_label="full-vs-plan")
        workload("before-daemon", "/usr/bin/docker", None, paths)
        with self.assertRaises(ValueError):
            workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        self.assertFalse(any(call["label"] in {"full-vs-kernel-verify", "full-vs-assembly"} for call in calls))

    def test_kernel_failure_stops_before_build(self):
        workload, paths, endpoint, calls = self.fixture(fail_label="full-vs-kernel-verify")
        workload("before-daemon", "/usr/bin/docker", None, paths)
        with self.assertRaises(ValueError):
            workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        self.assertNotIn("full-vs-assembly", [call["label"] for call in calls])

    def test_postcheck_failure_is_not_a_successful_full_build(self):
        workload, paths, endpoint, calls = self.fixture(fail_label="full-vs-installed-images")
        workload("before-daemon", "/usr/bin/docker", None, paths)
        with self.assertRaises(ValueError):
            workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        rows = workload.stage_timings
        self.assertTrue(all(row["duration_seconds"] >= 0 for row in rows))
        self.assertEqual(len([row for row in rows if row["phase"] == "build"]), 1)
        self.assertEqual(rows[-1]["phase"], "validation")
        self.assertEqual(rows[-1]["outcome"], "failed")
        self.assertEqual(rows[-1]["stage"], "full-vs-installed-images")


    def test_old_archive_only_status_cannot_execute_build(self):
        workload, paths, endpoint, calls = self.fixture(plan_status="ARCHIVES_VERIFIED_COMMANDS_ONLY")
        workload("before-daemon", "/usr/bin/docker", None, paths)
        with self.assertRaises(ValueError):
            workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        self.assertNotIn("full-vs-assembly", [call["label"] for call in calls])

    def test_phase_order_and_repeat_are_refused(self):
        workload, paths, endpoint, calls = self.fixture()
        with self.assertRaises(ValueError):
            workload("after-daemon", "/usr/bin/docker", endpoint, paths)
        workload("before-daemon", "/usr/bin/docker", None, paths)
        with self.assertRaises(ValueError):
            workload("before-daemon", "/usr/bin/docker", None, paths)


if __name__ == "__main__":
    unittest.main()
