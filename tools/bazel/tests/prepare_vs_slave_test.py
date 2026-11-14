#!/usr/bin/env python3
"""Pure working-scope and no-Bazel routing fixtures; no native preparation or build."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SHELL = ROOT / "tools/bazel/ci/prepare_vs_slave.sh"
WORKER = "ks-e6fb1976-715102e6996d-0-" + "a" * 32


def namespace():
    shell = SHELL.read_text()
    text = shell.split("<<'PY_VS_WORKING'\n", 1)[1].split("\nPY_VS_WORKING\n", 1)[0]
    parsed = ast.parse(text)
    body = [node for node in parsed.body if isinstance(node, ast.FunctionDef) or
            (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in
             ("NATIVE_REVISION", "NATIVE_FILES") for target in node.targets))]
    scope = {name: globals()[name] for name in
             ("hashlib", "json", "os", "Path", "re", "stat", "subprocess", "sys", "tempfile")}
    # Pool matching is an existing native API; these fixtures check its frontend boundary.
    scope["worker_matches_pool"] = lambda name, pool: name == WORKER and pool == "sonic-slave-container-k3s"
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SHELL), "exec"), scope)
    return scope


class WorkingScopeTest(unittest.TestCase):
    def setUp(self):
        self.ns = namespace()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.parent = self.work / "runner-temp"
        self.parent.mkdir(mode=0o700)
        self.owner = (os.getuid(), os.getgid())

    def fresh(self, **changes):
        args = dict(parent=self.parent, run="123", attempt="2", worker=WORKER,
                    work_root=self.work, owner=self.owner)
        args.update(changes)
        return self.ns["fresh_scope"](**args)

    def test_fresh_scope_has_only_transient_directories(self):
        root = self.fresh()
        self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob("*")),
                         ["bazel", "bazel/bazelisk", "bazel/output-base", "bazel/repository_cache",
                          "packages", "packages/vcache", "tmp"])
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in [root, *root.rglob("*")]))
        self.assertFalse((root / "bazel/disk_cache").exists())

    def test_existing_scope_is_preserved_and_refused(self):
        root = self.fresh()
        sentinel = root / "sentinel"
        sentinel.write_bytes(b"unchanged")
        with self.assertRaises(FileExistsError):
            self.fresh()
        self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_existing_cache_sentinel_is_untouched(self):
        cache = self.work / ".source-image-cache"
        cache.mkdir()
        (cache / "sentinel").write_bytes(b"persistent")
        self.fresh()
        self.assertEqual((cache / "sentinel").read_bytes(), b"persistent")

    def test_parent_symlink_is_refused(self):
        link = self.work / "linked"
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.fresh(parent=link)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_outside_parent_is_refused(self):
        other = self.work / "different"
        other.mkdir()
        with self.assertRaises(ValueError):
            self.fresh(work_root=other)

    def test_group_writable_parent_is_refused(self):
        self.parent.chmod(0o770)
        with self.assertRaises(ValueError):
            self.fresh()

    def test_other_owner_is_refused(self):
        with self.assertRaises(ValueError):
            self.fresh(owner=(self.owner[0] + 1, self.owner[1]))

    def test_invalid_run_and_attempt_are_refused(self):
        for value in ("0", "01", "-1", "../1", "1\n", "9" * 21, 1, True):
            for key in ("run", "attempt"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.fresh(**{key: value})

    def test_worker_mismatch_is_refused(self):
        with self.assertRaises(ValueError):
            self.fresh(worker="../worker")

    def test_working_probe_closes_without_named_output(self):
        root = self.fresh()
        before = sorted(root.iterdir())
        self.ns["writable_scope"](root)
        self.assertEqual(sorted(root.iterdir()), before)

    def policy(self):
        return {"WORKER_POOL": "sonic-slave-container-k3s", "SONIC_VS_REUSE_ONLY": "y",
                "SONIC_DEB_BUILD_POLICY": "reuse-only", "SONIC_BAZEL_KERNEL_PREPARE": "verify",
                "SONIC_BAZEL_VS_ARCHIVE_PROFILE": "final23", "SONIC_DPKG_CACHE_METHOD": "none",
                "SONIC_DPKG_CACHE_METHOD_OVERRIDE": "none", "SONIC_BAZEL_CACHE_POLICY": "remote-only"}

    def test_metadata_policy_needs_no_auth_value(self):
        policy = self.policy()
        self.assertNotIn("BAZEL_REMOTE_AUTH", policy)
        self.ns["validate_policy"](policy)

    def test_each_early_policy_is_required(self):
        for key in self.policy():
            policy = self.policy()
            policy.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.ns["validate_policy"](policy)

    def test_rwcache_override_is_refused(self):
        for key in ("SONIC_DPKG_CACHE_METHOD", "SONIC_DPKG_CACHE_METHOD_OVERRIDE"):
            policy = self.policy()
            policy[key] = "rwcache"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.ns["validate_policy"](policy)







    def test_native_root_outside_job_paths_is_refused_before_git(self):
        with mock.patch.object(subprocess, "check_output") as command:
            with self.assertRaises(ValueError):
                self.ns["load_native"](self.work, self.parent, self.parent)
        command.assert_not_called()

    def test_wrong_native_revision_is_refused_before_import(self):
        native = self.parent / "native"
        native.mkdir()
        with mock.patch.object(subprocess, "check_output", return_value=b"wrong\n"), \
             mock.patch.object(subprocess, "run") as command:
            with self.assertRaises(ValueError):
                self.ns["load_native"](native, self.work, self.parent)
        command.assert_not_called()

    def configuration(self, reuse, **changes):
        shell = SHELL.read_text()
        fragment = shell.split('export SONIC_DPKG_CACHE_SOURCE="$vs_dpkg_cache"', 1)[1].split(
            'export SONIC_BAZEL_CACHE_SOURCE=', 1)[0]
        fragment = 'export SONIC_DPKG_CACHE_SOURCE="$vs_dpkg_cache"' + fragment
        environment = {"PATH": "/usr/bin:/bin", "reuse_only": reuse,
                       "vs_dpkg_cache": str(self.parent / "packages"), "vs_bazel_cache": str(self.parent / "bazel"),
                       "SONIC_DPKG_CACHE_METHOD": "none", "SONIC_DPKG_CACHE_METHOD_OVERRIDE": "none",
                       "SONIC_BAZEL_CACHE_POLICY": "remote-only"}
        environment.update(changes)
        tail = '\nprintf "%s\\n" "$SONIC_DPKG_CACHE_METHOD" "${BAZEL-}" "${BAZEL_REAL-}" "${BAZEL_CONTAINER_CACHE_DIR-}" "${BAZEL_SWSS_CACHE_DIR-}"'
        return subprocess.run(["/bin/bash", "-euc", 'die() { exit 91; };\n' + fragment + tail],
                              env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def test_reuse_shell_refuses_bazel_and_retains_transient_interfaces(self):
        result = self.configuration("y")
        self.assertEqual(result.returncode, 0, result.stderr)
        cache = str(self.parent / "bazel")
        self.assertEqual(result.stdout.splitlines(), ["none", "/usr/bin/false", "/usr/bin/false", cache, cache])

    def test_reuse_export_refuses_an_unexpected_routed_bazel_command(self):
        shell = SHELL.read_text()
        fragment = shell.split('export SONIC_DPKG_CACHE_SOURCE="$vs_dpkg_cache"', 1)[1].split(
            'export SONIC_BAZEL_CACHE_SOURCE=', 1)[0]
        sentinel = self.work / "unexpected-bazel"
        fake = self.work / "caller-bazel"
        fake.write_text('#!/bin/sh\ntouch "$SENTINEL"\n')
        fake.chmod(0o700)
        environment = {"PATH": "/usr/bin:/bin", "reuse_only": "y", "vs_bazel_cache": str(self.parent / "bazel"),
                       "SONIC_DPKG_CACHE_METHOD": "none", "SONIC_DPKG_CACHE_METHOD_OVERRIDE": "none",
                       "SONIC_BAZEL_CACHE_POLICY": "remote-only", "BAZEL": str(fake), "BAZEL_REAL": str(fake),
                       "SENTINEL": str(sentinel)}
        result = subprocess.run(["/bin/bash", "-euc", 'die() { exit 91; };\n' + fragment + '\n"$BAZEL" build //unexpected:action'],
                                env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(sentinel.exists())

    def test_legacy_shell_preserves_caller_bazel_and_rwcache(self):
        result = self.configuration("n", BAZEL="legacy-bazel", BAZEL_REAL="legacy-real",
                                    BAZEL_CONTAINER_CACHE_DIR="legacy-container", BAZEL_SWSS_CACHE_DIR="legacy-swss")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["rwcache", "legacy-bazel", "legacy-real", "legacy-container", "legacy-swss"])

    def test_shell_refuses_late_cache_policy_reset(self):
        result = self.configuration("y", SONIC_DPKG_CACHE_METHOD_OVERRIDE="rwcache")
        self.assertEqual(result.returncode, 91)
        self.assertEqual(result.stdout, "")

    def test_untracked_initializer_and_ignored_bytecode_refuse_import(self):
        native = self.parent / "native"
        native.mkdir()
        marker = self.work / "import-executed"
        for name in ("tools/__init__.py", "tools/bazel/ci/__pycache__/source_image_worker.cpython-313.pyc"):
            file = native / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"raise RuntimeError('untrusted module must not execute')\n")
            previous_path = list(sys.path)
            with self.subTest(name=name), \
                 mock.patch.object(subprocess, "check_output", side_effect=[
                     (self.ns["NATIVE_REVISION"] + "\n").encode(), (name + "\n").encode()]) as read, \
                 mock.patch.object(subprocess, "run", return_value=types.SimpleNamespace(returncode=0)):
                with self.assertRaisesRegex(ValueError, "Untracked native import inputs"):
                    self.ns["load_native"](native, self.work, self.parent)
            self.assertEqual(read.call_args.args[0], ["git", "--no-optional-locks", "-C", str(native),
                                                    "ls-files", "--others", "--", "tools"])
            self.assertEqual(sys.path, previous_path)
            self.assertFalse(marker.exists())
            file.unlink()



class ConnectedSourceAdmissionTest(unittest.TestCase):
    def execute_guard(self, *, reject=False):
        import runpy
        shell = SHELL.read_text()
        fragment = shell.split("import runpy\ncontroller = Path(sys.argv[5])", 1)[1]
        fragment = "import runpy\ncontroller = Path(sys.argv[5])" + fragment.split(
            "\nworker_matches_pool = resources.worker_matches_pool", 1)[0]
        program = compile(ast.parse(fragment), "<actual-frontend-source-admission>", "exec")
        events = []
        assembly = {"commit": "d" * 40, "tree": "b" * 40}
        bootstrap = "src/sonic-build-hooks/buildinfo/sonic-build-hooks_1.0_all.deb"
        bootstrap_record = {"bytes": 12148, "sha256": "e" * 64}
        inventory = {"files": {"target/original-input": {}, "artifacts/original.json": {},
                                "files/tracked-source": {}, bootstrap: bootstrap_record}}
        def guard(native, workspace, **kwargs):
            events.append("source")
            if reject:
                raise ValueError("fixture dirty source")
            self.assertEqual(kwargs["revision"], assembly["commit"])
            self.assertEqual(kwargs["tree"], assembly["tree"])
            self.assertEqual(kwargs["allowed_untracked"], ["artifacts/original.json", bootstrap, "target/original-input"])
            self.assertEqual(kwargs["bootstrap_input"], bootstrap_record)
            return {"status": "CHECKOUT_VERIFIED_ONLY"}
        def process(argv, **kwargs):
            events.append("canonical-native-admission")
            self.assertEqual(argv, ["python", "admission"])
            self.assertEqual(kwargs["stderr"], sys.stderr)
            return types.SimpleNamespace(stdout=json.dumps({
                "status": "NATIVE_INPUTS_VERIFIED_BEFORE_PREPARATION_ONLY",
                "assembly": assembly}).encode())
        planner = {"data": lambda *args: inventory, "assembly_domain": lambda value, **context: assembly,
                   "strict_json": json.loads}
        stages = {"BOOTSTRAP_INPUT": bootstrap, "verify_assembly_checkout": guard,
                  "planner_command": lambda *args, **kwargs: ["python", "admission"]}
        scope = {"Path": Path, "sys": sys, "os": os, "subprocess": subprocess,
                 "workspace": Path("/fixture/workspace"), "native": object(),
                 "require": lambda value, reason: None if value else (_ for _ in ()).throw(ValueError(reason))}
        with mock.patch.object(runpy, "run_path", side_effect=[planner, stages]), \
             mock.patch.object(sys, "argv", ["fixture", "", "", "", "", "/fixture/controller"]), \
             mock.patch.dict(os.environ, {"VS_REUSE_INPUTS": "/fixture/inputs.json",
                                          "VS_REUSE_INPUTS_SHA256": "c" * 64,
                                          "VS_SOURCE_MERGE": "d" * 40,
                                          "VS_SOURCE_HEAD": "a" * 40,
                                          "VS_SOURCE_TREE": assembly["tree"]}), \
             mock.patch.object(subprocess, "run", side_effect=process) as run:
            if reject:
                with self.assertRaisesRegex(ValueError, "dirty source"):
                    exec(program, scope)
                run.assert_not_called()
            else:
                exec(program, scope)
        return events

    def test_clean_source_guard_precedes_any_assembly_module_admission(self):
        self.assertEqual(self.execute_guard(), ["source", "canonical-native-admission"])

    def test_dirty_source_never_enters_canonical_admission(self):
        self.assertEqual(self.execute_guard(reject=True), ["source"])


    def test_early_shell_call_passes_pr_head_and_tested_merge_to_real_planner_cli(self):
        # Execute only the read-only shell prefix. The real planner parser runs
        # with payload reads mocked, before any UID, daemon or host preparation.
        prefix = SHELL.read_text().split('[[ $(id -u):$(id -g)', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "bin"
            binary.mkdir()
            program = binary / "python3"
            planner = ROOT / "tools/bazel/ci/vs_reuse_plan.py"
            program.write_text(
                "#!" + sys.executable + "\n"
                "import importlib.util, sys\nfrom pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('fixture_planner', " + repr(str(planner)) + ")\n"
                "plan = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(plan)\n"
                "plan.inputs = lambda args: (Path(args.workspace), "
                "{'source': {'head': args.source_head, 'merge': args.merge, 'tree': args.source_tree}, "
                "'assembly': {'commit': args.merge, 'tree': args.source_tree}})\n"
                "sys.argv = ['vs_reuse_plan.py'] + sys.argv[sys.argv.index('inputs'):]\n"
                "plan.main()\n")
            program.chmod(0o700)
            script = root / "prefix.sh"
            script.write_text(prefix)
            environment = dict(os.environ, PATH=str(binary) + ":/usr/bin:/bin",
                               SONIC_VS_REUSE_ONLY="y", SONIC_VS_SUPERVISED_PHASE="before-daemon",
                               WORKER_POOL="sonic-slave-container-k3s",
                               SONIC_DEB_BUILD_POLICY="reuse-only", SONIC_BAZEL_KERNEL_PREPARE="verify",
                               SONIC_BAZEL_VS_ARCHIVE_PROFILE="final23", SONIC_DPKG_CACHE_METHOD="none",
                               SONIC_DPKG_CACHE_METHOD_OVERRIDE="none", SONIC_BAZEL_CACHE_POLICY="remote-only",
                               GITHUB_WORKSPACE=str(root), VS_REUSE_INPUTS=str(root / "unused-fixture.json"),
                               VS_REUSE_INPUTS_SHA256="f" * 64, VS_SOURCE_HEAD="a" * 40,
                               VS_SOURCE_MERGE="b" * 40, VS_SOURCE_TREE="c" * 40)
            result = subprocess.run(["/bin/bash", str(script)], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["source"]["head"], "a" * 40)
            self.assertEqual(value["assembly"], {"commit": "b" * 40, "tree": "c" * 40})

    def test_generated_json_environment_preserves_values_as_data_exclusively(self):
        shell = SHELL.read_text()
        fragment = shell.split("<<'PY_ENVIRONMENT'\n", 1)[1].split("\nPY_ENVIRONMENT\n", 1)[0]
        program = compile(ast.parse(fragment), "<actual-frontend-environment>", "exec")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "environment.json"
            environment = {"BUILD_HOSTNAME": "literal ' $(never-executed)", "SONIC_IMAGE_VERSION": "bazel-ci"}
            argv = ["fixture", str(target), *environment]
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, environment):
                exec(program, {"__name__": "__fixture__"})
                original = target.read_bytes()
                self.assertEqual(json.loads(original), {"schema": 1, "unset": ["PLATFORM"],
                                                       "environment": environment})
                self.assertTrue(original.endswith(b"\n"))
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                with self.assertRaises(FileExistsError):
                    exec(program, {"__name__": "__fixture__"})
                self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
