#!/usr/bin/env python3
"""Check retained-image transitions and the actual outer Make selection."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import build


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        worker_record = mock.patch.object(build, "WORKER_RECORD", self.root / "absent-worker.json")
        worker_record.start()
        self.addCleanup(worker_record.stop)
        (self.root / "target").mkdir()
        self.output = self.root / "target/sonic-vs.bin"
        self.receipt = self.root / build.RECEIPT
        selector = mock.patch.object(build.backend, "select_backend")
        self.select_backend = selector.start()
        self.addCleanup(selector.stop)

    def retained(self):
        self.output.write_bytes(b"previous-bazel-installer")
        self.receipt.write_text('{"schema":1,"producer":"bazel"}\n')

    def test_native_switch_forces_rebuild_and_removes_marker_after_success(self):
        self.retained()

        def execute(command, *, cwd, check):
            self.assertFalse(self.output.exists())
            self.assertIn("SONIC_BAZEL_NATIVE_PUBLISH=y", command)
            self.assertIn("BUILD_WITH_BAZEL_WHEN_AVAILABLE=n", command)
            self.assertIn("USE_CONTAINER_IMAGE_STORE_AS_BACKEND=n", command)
            self.output.write_bytes(b"new-native-installer")

        build.native_build(self.root, execute)
        self.assertEqual(self.output.read_bytes(), b"new-native-installer")
        self.assertFalse(self.receipt.exists())

    def test_failed_native_switch_restores_previous_installer_and_receipt(self):
        self.retained()
        previous = self.receipt.read_bytes()

        def execute(command, **kwargs):
            self.output.write_bytes(b"partial-output")
            self.receipt.unlink()
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaises(subprocess.CalledProcessError):
            build.native_build(self.root, execute)
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
        self.assertEqual(self.receipt.read_bytes(), previous)

    def test_native_noop_does_not_invalidate_native_output(self):
        self.output.write_bytes(b"native-installer")
        before = self.output.stat().st_mtime_ns

        def execute(command, **kwargs):
            self.assertEqual(self.output.read_bytes(), b"native-installer")

        build.native_build(self.root, execute)
        self.assertEqual(self.output.stat().st_mtime_ns, before)

    def test_native_empty_success_restores_prior_installer(self):
        self.retained()
        with self.assertRaisesRegex(ValueError, "nonempty"):
            build.native_build(self.root, lambda *args, **kwargs: self.output.touch())
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")

    def test_interrupted_transition_backup_is_preserved(self):
        backup = self.root / "target/.sonic-vs.bin.bazel-previous"
        backup.write_bytes(b"recoverable")
        with self.assertRaisesRegex(ValueError, "unfinished native transition"):
            build.native_build(self.root, lambda *args, **kwargs: self.fail("must not build"))
        self.assertEqual(backup.read_bytes(), b"recoverable")

    def test_cancellation_keeps_old_image_away_from_surviving_native_writer(self):
        self.retained()

        def execute(*args, **kwargs):
            self.output.write_bytes(b"native-output-in-progress")
            raise KeyboardInterrupt()

        with self.assertRaisesRegex(RuntimeError, "previous installer preserved"):
            build.native_build(self.root, execute)
        # A native worker can still write this path after its Make client exits.
        self.output.write_bytes(b"late-native-write")
        self.assertEqual((self.root / build.BACKUP).read_bytes(), b"previous-bazel-installer")
        with self.assertRaisesRegex(ValueError, "unfinished native transition"):
            build.check_transition(self.root)

    def test_publish_replaces_installer_and_records_exact_bytes(self):
        self.retained()
        source = self.root / "new-image"
        source.write_bytes(b"new-docker-installer")
        expected = {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "bytes": source.stat().st_size}
        build.publish(source, self.output, self.receipt, expected)
        self.assertEqual(self.output.read_bytes(), source.read_bytes())
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record["producer"], "bazel")
        self.assertEqual(record["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(record["bytes"], source.stat().st_size)
        self.assertTrue(os.access(self.output, os.X_OK))

    def test_publish_missing_source_preserves_previous_image(self):
        self.retained()
        previous = self.receipt.read_bytes()
        with self.assertRaises(FileNotFoundError):
            build.publish(self.root / "missing", self.output, self.receipt, {})
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
        self.assertEqual(self.receipt.read_bytes(), previous)

    def test_publish_rejects_output_replaced_after_bazel_completion(self):
        self.retained()
        previous = self.receipt.read_bytes()
        source = self.root / "reused-bazel-path"
        source.write_bytes(b"other-backend")
        expected = {"sha256": hashlib.sha256(b"first-backend").hexdigest(), "bytes": 13}
        with self.assertRaisesRegex(ValueError, "differ from this build's BEP"):
            build.publish(source, self.output, self.receipt, expected)
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
        self.assertEqual(self.receipt.read_bytes(), previous)

    def test_publish_failed_rename_restores_original_receipt(self):
        self.retained()
        previous = self.receipt.read_bytes()
        source = self.root / "new-image"
        source.write_bytes(b"new")
        replace = Path.replace

        def fail_destination(path, target):
            if target == self.output:
                raise OSError("cannot replace destination")
            return replace(path, target)

        with mock.patch.object(Path, "replace", fail_destination), self.assertRaises(OSError):
            build.publish(source, self.output, self.receipt,
                          {"sha256": hashlib.sha256(b"new").hexdigest(), "bytes": 3})
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
        self.assertEqual(self.receipt.read_bytes(), previous)

    def test_bep_identity_requires_digest_and_rejects_conflicting_records(self):
        source = self.root / "artifact"
        events = self.root / "events.jsonl"
        entry = {"uri": source.as_uri(), "digest": "a" * 64, "length": "3"}
        events.write_text(json.dumps({"namedSetOfFiles": {"files": [entry]}}) + "\n")
        self.assertEqual(build.output_identity(events, source), {"sha256": "a" * 64, "bytes": 3})
        for bad in ({**entry, "digest": ""}, {**entry, "digest": "b" * 64}):
            events.write_text(json.dumps({"namedSetOfFiles": {"files": [entry, bad]}}) + "\n")
            with self.assertRaises(ValueError):
                build.output_identity(events, source)

    def test_bazel_publication_uses_only_complete_current_build_events(self):
        self.retained()
        inputs = self.root / "target/bazel-image-inputs"
        inputs.mkdir()
        (inputs / "execution-environment.json").write_text("{}")
        (inputs / "prepared-host").mkdir()
        (inputs / "prepared-host/receipt.json").write_text("{}")
        state = self.root / "state"
        source = state / "execroot/project/bazel-out/bin/sonic-vs.bin"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"this-build-installer")
        args = SimpleNamespace(workspace=self.root, mount_root=self.root,
                               output_user_root=state, repository_cache=None,
                               persistent_worker=None, git_config=None, backend="n")
        events = [
            {"id": {"namedSet": {"id": "output"}}, "namedSetOfFiles": {"files": [{
                "uri": source.as_uri(), "digest": hashlib.sha256(source.read_bytes()).hexdigest(),
                "length": str(source.stat().st_size)}]}},
            {"id": {"targetCompleted": {"label": build.TARGET}}, "completed": {
                "success": True, "outputGroup": [{"name": "default", "fileSets": [{"id": "output"}]}]}},
            {"id": {"buildFinished": {}}, "finished": {"overallSuccess": True, "exitCode": {"name": "SUCCESS"}}},
        ]

        def execute(command, **kwargs):
            output = Path(next(item.split("=", 1)[1] for item in command
                               if item.startswith("--build_event_json_file=")))
            output.write_text("".join(json.dumps(event) + "\n" for event in events))

        for failure in ("incomplete", "failed", "outside-root"):
            previous = self.receipt.read_bytes()
            with self.subTest(failure=failure), mock.patch.object(build.subprocess, "run", execute):
                if failure == "incomplete":
                    saved = events.pop()
                elif failure == "failed":
                    events[-1]["finished"]["overallSuccess"] = False
                else:
                    args.output_user_root = self.root / "different-state"
                with self.assertRaises(ValueError):
                    build.bazel_build(args)
                self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
                self.assertEqual(self.receipt.read_bytes(), previous)
                if failure == "incomplete":
                    events.append(saved)
                elif failure == "failed":
                    events[-1]["finished"]["overallSuccess"] = True
                else:
                    args.output_user_root = state
        for selected in ("y", "n"):
            args.backend = selected
            with mock.patch.object(build.subprocess, "run", execute):
                build.bazel_build(args)
            self.select_backend.assert_called_with(self.root, build_with_bazel_when_available="y",
                                                   use_container_image_store_as_backend=selected)
            self.assertEqual(json.loads(self.receipt.read_text())["backend"],
                             "containerd" if selected == "y" else "docker")
        # The same public publisher executes Bazel directly inside a restricted
        # runner; no launcher or Docker client is used there.
        worker_record = self.root / 'worker.json'
        worker_record.write_text('{}')
        args.backend = 'y'
        with mock.patch.object(build, 'WORKER_RECORD', worker_record), \
                mock.patch.object(build.privileges, 'check') as guard, \
                mock.patch.object(build.subprocess, 'run', side_effect=execute) as invoked:
            build.bazel_build(args)
            guard.assert_called_once_with()
            argv = invoked.call_args.args[0]
            self.assertEqual(argv[0], '/usr/local/bin/bazel')
            self.assertIn('--spawn_strategy=processwrapper-sandbox', argv)
            self.assertNotIn('docker', argv)
        worker_record.write_text('{"worker_image":"wrong"}')
        with mock.patch.object(build, 'WORKER_RECORD', worker_record), \
                mock.patch.object(build.privileges, 'check'), \
                mock.patch.object(build.subprocess, 'run') as invoked:
            with self.assertRaisesRegex(ValueError, 'runner differs'):
                build.bazel_build(args)
            invoked.assert_not_called()
        self.assertEqual(self.output.read_bytes(), source.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o755)

    def test_public_entry_imports_local_helpers_with_python_safe_path(self):
        environment = dict(os.environ, PYTHONSAFEPATH="1", PYTHONPATH="")
        result = subprocess.run([sys.executable, str(Path(build.__file__).resolve()), "--help"],
                                cwd=self.root, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--backend", result.stdout)

    def test_containerd_without_host_handoff_cannot_fall_back_to_privileged_worker(self):
        inputs = self.root / 'target/bazel-image-inputs'
        inputs.mkdir()
        (inputs / 'execution-environment.json').write_text('{}')
        args = SimpleNamespace(workspace=self.root, backend='y')
        with mock.patch.object(build.subprocess, 'run') as execute:
            with self.assertRaisesRegex(ValueError, 'separate native job'):
                build.bazel_build(args)
            execute.assert_not_called()

    def make_workspace(self):
        source = Path(__file__).resolve().parents[3]
        shutil.copyfile(source / "Makefile", self.root / "Makefile")
        (self.root / "Makefile.work").write_text("%:\n\t@echo native-target:$@\n")
        (self.root / "rules").mkdir()
        (self.root / "rules/config").write_text(
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE ?= n\n"
            "USE_CONTAINER_IMAGE_STORE_AS_BACKEND ?= n\n"
            'SECURE_UPGRADE_MODE ?= "no_sign"\n')

    def make(self, *arguments):
        # Clear inherited jobserver/options from a caller's Make invocation.
        env = {key: value for key, value in os.environ.items()
               if key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}}
        return subprocess.run(["make", "-n", *arguments, "target/sonic-vs.bin"],
                              cwd=self.root, env=env, text=True, capture_output=True)

    def test_make_selects_bazel_despite_retained_output(self):
        self.make_workspace()
        self.retained()
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--build-with-bazel-when-available "y"', result.stdout)
        self.assertNotIn("Makefile.work", result.stdout)

    def test_make_backend_requires_bazel_and_passes_the_selected_mode(self):
        self.make_workspace()
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--backend "y"', result.stdout)
        for mode in ("", "true", "y n"):
            result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "USE_CONTAINER_IMAGE_STORE_AS_BACKEND=" + mode)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=n", "USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", result.stderr)

    def test_make_rejects_invalid_selector(self):
        self.make_workspace()
        for value in ("", "y n", "true"):
            result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=" + value)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_make_uses_persisted_config_and_command_line_override(self):
        self.make_workspace()
        (self.root / "rules/config.user").write_text("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y\n")
        result = self.make()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--build-with-bazel-when-available "y"', result.stdout)
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--build-with-bazel-when-available "n"', result.stdout)

    def test_component_kvm_and_native_preparation_goals_keep_forwarding(self):
        self.make_workspace()
        for goal in ("target/docker-orchagent.gz", "target/sonic-vs.img.gz", "bazel-vs-native-inputs"):
            with self.subTest(goal=goal):
                result = subprocess.run(["make", "-n", "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", goal],
                                        cwd=self.root, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Makefile.work", result.stdout)
                self.assertNotIn("build.py", result.stdout)

    def test_native_preparation_clears_backend_for_prerequisites_and_cleanup(self):
        self.make_workspace()
        (self.root / "scripts").mkdir()
        retry = self.root / "scripts/run_with_retry"
        retry.write_text('#!/bin/sh\nexec "$@"\n')
        retry.chmod(0o755)
        # Exercise the real outer forwarding recipe, recording the selectors
        # received by every native prerequisite and cleanup sub-make.
        (self.root / "Makefile.work").write_text(
            "include rules/config\n-include rules/config.user\n"
            ".PHONY: bookworm bazel-vs-native-inputs docker-cleanup\n"
            "bookworm bazel-vs-native-inputs docker-cleanup:\n"
            '\t@test "$(USE_CONTAINER_IMAGE_STORE_AS_BACKEND)" = n\n'
            '\t@echo "$@ $(USE_CONTAINER_IMAGE_STORE_AS_BACKEND)" >> handoff.log\n')
        env = {key: value for key, value in os.environ.items()
               if key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}}
        for saved in (False, True):
            with self.subTest(saved=saved):
                arguments = ["make", "--no-print-directory", "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y"]
                if saved:
                    (self.root / "rules/config.user").write_text("USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y\n")
                else:
                    arguments.append("USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y")
                result = subprocess.run(arguments + ["bazel-vs-native-inputs"], cwd=self.root,
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                record = self.root / "handoff.log"
                self.assertEqual(record.read_text().splitlines(),
                                 ["bookworm n", "bazel-vs-native-inputs n", "docker-cleanup n"])
                record.unlink()

    def test_inner_store_guards_preserve_setup_and_component_goals(self):
        self.make_workspace()
        self.retained()
        (self.root / "rules/config.user").write_text("USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y\n")
        source = Path(__file__).resolve().parents[3]
        work = (source / "Makefile.work").read_text()
        slave = (source / "slave.mk").read_text()
        start = "ifneq ($(words $(USE_CONTAINER_IMAGE_STORE_AS_BACKEND)),1)"
        work_guard = work[work.index(start):work.index("\nifneq ($(filter target/sonic-vs.bin,")]
        slave_guard = slave[slave.index(start):slave.index("\nifneq ($(strip $(SONIC_EXTRA_EXPORT_VARS)),)")]
        # Read the production deferred installer check; do not copy its logic.
        check_start = slave.index("# Defer this check until Make reaches an installer")
        slave_guard += slave[check_start:slave.index("\nifneq ($(filter $(TARGET_PATH)/sonic-vs.bin,", check_start)]
        env = {key: value for key, value in os.environ.items()
               if key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}}
        for inner, guard in (("work", work_guard), ("slave", slave_guard)):
            (self.root / "inner.mk").write_text(
                ".SECONDEXPANSION:\ninclude rules/config\ninclude rules/config.user\n"
                "TARGET_PATH := target\nSONIC_INSTALLERS := sonic-vs.bin sonic-vs.img.gz\n"
                + guard + "\nall bazel-vs-native-inputs: target/sonic-vs.bin\n"
                "init configure target/docker-orchagent.gz target/sonic-vs.bin target/sonic-vs.img.gz:\n"
                '\t@echo native-target:$@ component-mode:$(BUILD_WITH_BAZEL_WHEN_AVAILABLE)\n')
            def invoke(goal, mode="y", *options):
                return subprocess.run(["make", "-n", "-f", "inner.mk",
                                       "BUILD_WITH_BAZEL_WHEN_AVAILABLE=" + mode, *options, goal],
                                      cwd=self.root, env=env, text=True, capture_output=True)
            for mode in ("y", "n"):
                for goal in ("init", "configure", "target/docker-orchagent.gz"):
                    with self.subTest(inner=inner, goal=goal, mode=mode):
                        result = invoke(goal, mode)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("component-mode:" + mode, result.stdout)
            for goal in ("target/sonic-vs.bin", "target/sonic-vs.img.gz") + (("all",) if inner == "slave" else ()):
                with self.subTest(inner=inner, goal=goal):
                    result = invoke(goal)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("requires top-level Bazel VS assembly", result.stderr)
            if inner == "slave":
                result = invoke("bazel-vs-native-inputs", "y", "BAZEL_NATIVE_PREPARE=y")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_make_rejects_unsupported_bazel_image_configuration(self):
        self.make_workspace()
        for setting in ("PLATFORM_ARCH=arm64", "PLATFORM=mellanox", "BLDENV=bookworm"):
            result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", setting)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Bazel VS assembly requires", result.stderr)

    def test_make_rejects_features_missing_from_prepared_inputs(self):
        self.make_workspace()
        for option in ("ENABLE_SBOM", "ENABLE_ASAN", "SONIC_INSTALL_DEBUG_TOOLS",
                       "INSTALL_DEBUG_TOOLS", "BUILD_MULTIASIC_KVM", "MULTIARCH_QEMU_ENVIRON",
                       "CROSS_BUILD_ENVIRON", "BUILD_REDUCE_IMAGE_SIZE", "INCLUDE_KUBERNETES",
                       "INCLUDE_KUBERNETES_MASTER", "SONIC_ENABLE_IMAGE_SIGNATURE",
                       "ENABLE_IMAGE_SIGNATURE"):
            with self.subTest(option=option):
                result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", option + "=y")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires these options disabled: " + option, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_make_rejects_conflicting_saved_native_configuration(self):
        self.make_workspace()
        self.retained()
        for filename, value in ((".arch", "arm64"), (".platform", "mellanox"),
                                (".arch", ""), (".platform", "")):
            with self.subTest(filename=filename, value=value):
                saved = self.root / filename
                saved.write_text(value + "\n")
                result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y",
                                   "PLATFORM=vs", "PLATFORM_ARCH=amd64")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires saved " + filename, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
                saved.unlink()
        (self.root / ".arch").write_text("amd64\n")
        (self.root / ".platform").write_text("vs\n")
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--build-with-bazel-when-available "y"', result.stdout)

    def test_make_rejects_signing_modes_and_unstaged_package_hooks(self):
        self.make_workspace()
        for setting in ("SECURE_UPGRADE_MODE=dev", "SECURE_UPGRADE_MODE=prod",
                        "SECURE_UPGRADE_MODE=", "SECURE_UPGRADE_MODE=unknown",
                        "SONIC_PACKAGES=custom-package", "sonic-vs.bin_POST_BUILD_HOOK=custom-hook"):
            with self.subTest(setting=setting):
                result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", setting)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Bazel VS assembly", result.stderr)
                self.assertEqual(result.stdout, "")
        for value in ("no_sign", '"no_sign"'):
            result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "SECURE_UPGRADE_MODE=" + value)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_make_applies_feature_guards_to_saved_configuration_and_cli_overrides(self):
        self.make_workspace()
        (self.root / "rules/config.user").write_text(
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE=y\nENABLE_SBOM=y\nSONIC_ENABLE_IMAGE_SIGNATURE=y\n")
        result = self.make()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ENABLE_SBOM", result.stderr)
        self.assertIn("SONIC_ENABLE_IMAGE_SIGNATURE", result.stderr)
        self.assertEqual(result.stdout, "")
        result = self.make("ENABLE_SBOM=n", "SONIC_ENABLE_IMAGE_SIGNATURE=n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--build-with-bazel-when-available "y"', result.stdout)

    def test_make_native_selection_preserves_supported_native_features(self):
        self.make_workspace()
        (self.root / ".arch").write_text("arm64\n")
        (self.root / ".platform").write_text("mellanox\n")
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=n", "ENABLE_SBOM=y",
                           "SONIC_ENABLE_IMAGE_SIGNATURE=y", "SECURE_UPGRADE_MODE=prod",
                           "SONIC_INSTALL_DEBUG_TOOLS=y", "SONIC_PACKAGES=custom-package",
                           "sonic-vs.bin_POST_BUILD_HOOK=custom-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--build-with-bazel-when-available "n"', result.stdout)

    def test_make_rejects_bazel_aggregate_goal_instead_of_native_fallback(self):
        self.make_workspace()
        result = self.make("BUILD_WITH_BAZEL_WHEN_AVAILABLE=y", "all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only the explicit target/sonic-vs.bin goal", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_make_without_goals_preserves_original_default(self):
        self.make_workspace()
        result = subprocess.run(["make", "-n"], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Making jessie", result.stdout)
        self.assertNotIn("build.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
