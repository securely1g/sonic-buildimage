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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build


class BuildTest(unittest.TestCase):
    def setUp(self):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("BAZEL_REQUESTED_")}
        environment_patch = mock.patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "target").mkdir()
        self.output = self.root / "target/sonic-vs.bin"
        self.receipt = self.root / build.RECEIPT
        self.transition = self.root / build.TRANSITION
        self.input_provenance = build.verify_inputs.bytes_identity(b"preparation receipt")

    def retained(self):
        self.output.write_bytes(b"previous-bazel-installer")
        self.receipt.write_text('{"schema":1,"producer":"bazel"}\n')

    def test_native_switch_forces_rebuild_and_removes_marker_after_success(self):
        self.retained()

        def execute(command, *, cwd, check):
            self.assertFalse(self.output.exists())
            self.assertTrue(self.transition.is_file())
            self.assertIn("SONIC_BAZEL_NATIVE_PUBLISH=y", command)
            self.assertIn("BUILD_WITH_BAZEL_WHEN_AVAILABLE=n", command)
            self.output.write_bytes(b"new-native-installer")

        build.native_build(self.root, execute)
        self.assertEqual(self.output.read_bytes(), b"new-native-installer")
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.transition.exists())

    def test_failed_native_switch_restores_previous_installer_and_receipt(self):
        self.retained()
        previous = self.receipt.read_bytes()

        def execute(command, **kwargs):
            self.assertTrue(self.transition.is_file())
            self.output.write_bytes(b"partial-output")
            self.receipt.unlink()
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaises(subprocess.CalledProcessError):
            build.native_build(self.root, execute)
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
        self.assertEqual(self.receipt.read_bytes(), previous)
        self.assertFalse(self.transition.exists())
        build.check_transition(self.root)

    def test_native_noop_does_not_invalidate_native_output(self):
        self.output.write_bytes(b"native-installer")
        before = self.output.stat().st_mtime_ns

        def execute(command, **kwargs):
            self.assertEqual(self.output.read_bytes(), b"native-installer")

        build.native_build(self.root, execute)
        self.assertEqual(self.output.stat().st_mtime_ns, before)
        self.assertFalse(self.transition.exists())

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
        self.assertTrue(self.transition.is_file())
        with self.assertRaisesRegex(ValueError, "unfinished native transition"):
            build.check_transition(self.root)

    def test_cancellation_without_receipt_blocks_another_publisher(self):
        self.output.write_bytes(b"native-installer")

        def execute(*args, **kwargs):
            self.assertTrue(self.transition.is_file())
            self.output.write_bytes(b"native-output-in-progress")
            raise KeyboardInterrupt()

        with self.assertRaisesRegex(RuntimeError, "retain.*native-in-progress"):
            build.native_build(self.root, execute)
        self.assertFalse((self.root / build.BACKUP).exists())
        self.assertTrue(self.transition.is_file())
        with self.assertRaisesRegex(ValueError, "unfinished native transition.*remove.*native-in-progress"):
            build.check_transition(self.root)
        with self.assertRaisesRegex(ValueError, "unfinished native transition"):
            build.native_build(self.root, lambda *args, **kwargs: self.fail("must not build"))

    def test_ordinary_failure_without_receipt_clears_transition(self):
        self.output.write_bytes(b"native-installer")

        def execute(command, **kwargs):
            self.assertTrue(self.transition.is_file())
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaises(subprocess.CalledProcessError):
            build.native_build(self.root, execute)
        self.assertFalse(self.transition.exists())
        build.check_transition(self.root)

    def test_kvm_wrapper_holds_publication_lock_through_bin_consumption(self):
        source = Path(__file__).resolve().parents[3]
        for name in ("build_image.sh", "tools/bazel/image/build.py", "tools/bazel/image/verify_inputs.py",
                     "tools/bazel/ci/build_events.py"):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination)
        (self.root / "onie-image.conf").write_text('python3 fixture_writer.py\nexit "$TEST_ASSEMBLY_EXIT"\n')
        (self.root / "fixture_writer.py").write_text(
            "import fcntl\nimport os\nfrom pathlib import Path\n"
            "assert os.environ['SONIC_BAZEL_NATIVE_PUBLISH'] == 'y'\n"
            "assert os.environ['BUILD_WITH_BAZEL_WHEN_AVAILABLE'] == 'y'\n"
            "assert os.environ['IMAGE_TYPE'] == 'kvm'\n"
            "assert Path('target/.sonic-vs.bin.bazel-previous').read_bytes() == b'previous-bazel-installer'\n"
            "assert Path('target/.sonic-vs.bin.native-in-progress').is_file()\n"
            "Path('target/sonic-vs.bin').write_bytes(b'native-kvm-installer')\n"
            "with Path('target/.bazel-vs-image.lock').open('a') as lock:\n"
            "    try:\n"
            "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n"
            "        pass\n"
            "    else:\n"
            "        raise SystemExit('publisher lock was released before KVM consumption')\n"
            "Path('target/sonic-vs.img.gz').write_bytes(Path('target/sonic-vs.bin').read_bytes())\n")
        for exit_code in ("1", "0"):
            with self.subTest(exit_code=exit_code):
                self.retained()
                previous = self.receipt.read_bytes()
                environment = dict(os.environ, TARGET_MACHINE="vs", IMAGE_TYPE="kvm",
                                   BUILD_WITH_BAZEL_WHEN_AVAILABLE="y", SONIC_BAZEL_NATIVE_PUBLISH="n",
                                   TEST_ASSEMBLY_EXIT=exit_code)
                result = subprocess.run(["bash", "build_image.sh"], cwd=self.root, env=environment,
                                        text=True, capture_output=True)
                self.assertEqual(int(exit_code), result.returncode, result.stderr)
                self.assertEqual(b"native-kvm-installer", (self.root / "target/sonic-vs.img.gz").read_bytes())
                self.assertFalse((self.root / build.BACKUP).exists())
                self.assertFalse(self.transition.exists())
                if exit_code == "1":
                    self.assertEqual(b"previous-bazel-installer", self.output.read_bytes())
                    self.assertEqual(previous, self.receipt.read_bytes())
                else:
                    self.assertEqual(b"native-kvm-installer", self.output.read_bytes())
                    self.assertFalse(self.receipt.exists())

    def test_publish_replaces_installer_and_records_exact_bytes(self):
        self.retained()
        source = self.root / "new-image"
        source.write_bytes(b"new-docker-installer")
        expected = {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "bytes": source.stat().st_size}
        build.publish(source, self.output, self.receipt, expected, self.input_provenance)
        self.assertEqual(self.output.read_bytes(), source.read_bytes())
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record["producer"], "bazel")
        self.assertEqual(record["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(record["bytes"], source.stat().st_size)
        self.assertEqual(record["input_provenance"], self.input_provenance)
        self.assertTrue(os.access(self.output, os.X_OK))

    def test_publish_missing_source_preserves_previous_image(self):
        self.retained()
        previous = self.receipt.read_bytes()
        with self.assertRaises(FileNotFoundError):
            build.publish(self.root / "missing", self.output, self.receipt, {}, self.input_provenance)
        self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
        self.assertEqual(self.receipt.read_bytes(), previous)

    def test_publish_rejects_output_replaced_after_bazel_completion(self):
        self.retained()
        previous = self.receipt.read_bytes()
        source = self.root / "reused-bazel-path"
        source.write_bytes(b"other-backend")
        expected = {"sha256": hashlib.sha256(b"first-backend").hexdigest(), "bytes": 13}
        with self.assertRaisesRegex(ValueError, "differ from this build's BEP"):
            build.publish(source, self.output, self.receipt, expected, self.input_provenance)
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
                          {"sha256": hashlib.sha256(b"new").hexdigest(), "bytes": 3}, self.input_provenance)
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

    def prepared_bundle(self):
        inputs = self.root / "target/bazel-image-inputs"
        files = {}
        for name in build.verify_inputs.REQUIRED_INPUTS | {"images/docker-a.gz"}:
            path = inputs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"image_version": "unit-test"}) if name == "installer/config.json"
                            else "{}" if name == "execution-environment.json" else name + " bytes\n")
            files[name] = build.verify_inputs.file_identity(path)
        for name in build.verify_inputs.GRAPH_CONTROLS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# assembly graph\n" if "/vs/" in name else "# " + name + "\n")
        receipt = {
            "schema": build.verify_inputs.SCHEMA,
            "native_config_flags": {"INCLUDE_FIPS": "y", "ENABLE_FIPS": "n"},
            "files": files,
            "graph_controls": {name: build.verify_inputs.file_identity(self.root / name)
                               for name in build.verify_inputs.GRAPH_CONTROLS},
        }
        (inputs / "provenance.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return inputs, receipt

    def bazel_args(self):
        return SimpleNamespace(workspace=self.root, mount_root=self.root,
                               output_user_root=self.root / "state", repository_cache=None,
                               persistent_worker=None, git_config=None)

    def test_verified_worker_spec_is_stable_and_rejects_changed_or_linked_copies(self):
        inputs, receipt = self.prepared_bundle()
        metadata = receipt["files"]["execution-environment.json"]
        path = build.verified_worker_spec(self.root, inputs / "execution-environment.json", metadata)
        before = path.stat().st_mtime_ns
        self.assertEqual(path, build.verified_worker_spec(self.root, inputs / "execution-environment.json", metadata))
        self.assertEqual(before, path.stat().st_mtime_ns)
        self.assertEqual(metadata["sha256"] + ".json", path.name)
        path.chmod(0o644)
        path.write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "retained verified worker specification"):
            build.verified_worker_spec(self.root, inputs / "execution-environment.json", metadata)
        path.unlink()
        path.symlink_to(inputs / "execution-environment.json")
        with self.assertRaisesRegex(ValueError, "retained verified worker specification"):
            build.verified_worker_spec(self.root, inputs / "execution-environment.json", metadata)

    def test_bazel_publication_uses_only_complete_current_build_events(self):
        self.retained()
        inputs, _ = self.prepared_bundle()
        state = self.root / "state"
        source = state / "execroot/project/bazel-out/bin/sonic-vs.bin"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"this-build-installer")
        verified_receipt = state / "execroot/project/bazel-out/bin/verified/provenance.json"
        verified_receipt.parent.mkdir()
        verified_receipt.write_bytes((inputs / "provenance.json").read_bytes())
        args = self.bazel_args()
        events = [
            {"id": {"namedSet": {"id": "output"}}, "namedSetOfFiles": {"files": [{
                "uri": source.as_uri(), "digest": hashlib.sha256(source.read_bytes()).hexdigest(),
                "length": str(source.stat().st_size)}]}},
            {"id": {"namedSet": {"id": "provenance"}}, "namedSetOfFiles": {"files": [{
                "uri": verified_receipt.as_uri(), "digest": hashlib.sha256(verified_receipt.read_bytes()).hexdigest(),
                "length": str(verified_receipt.stat().st_size)}]}},
            {"id": {"targetCompleted": {"label": build.TARGET}}, "completed": {
                "success": True, "outputGroup": [{"name": "default", "fileSets": [{"id": "output"}]}]}},
            {"id": {"targetCompleted": {"label": build.PROVENANCE_TARGET}}, "completed": {
                "success": True, "outputGroup": [{"name": "default", "fileSets": [{"id": "provenance"}]}]}},
            {"id": {"buildFinished": {}}, "finished": {"overallSuccess": True, "exitCode": {"name": "SUCCESS"}}},
        ]
        worker_paths = set()
        graph_paths = set()

        def execute(command, **kwargs):
            self.assertIn(build.PROVENANCE_TARGET, command)
            package_path = next(item.split("=", 1)[1] for item in command if item.startswith("--package_path="))
            graph_name, fallback = package_path.split(":", 1)
            self.assertEqual("%workspace%", fallback)
            graph = Path(graph_name)
            graph_paths.add(graph)
            self.assertNotEqual(inputs, graph / build.verify_inputs.INPUT_DIRECTORY)
            self.assertEqual((inputs / "provenance.json").read_bytes(),
                             (graph / build.verify_inputs.INPUT_DIRECTORY / "provenance.json").read_bytes())
            worker_spec = Path(command[command.index("--worker-spec") + 1])
            worker_paths.add(worker_spec)
            self.assertNotEqual(inputs / "execution-environment.json", worker_spec)
            self.assertEqual((inputs / "execution-environment.json").read_bytes(), worker_spec.read_bytes())
            output = Path(next(item.split("=", 1)[1] for item in command
                               if item.startswith("--build_event_json_file=")))
            output.write_text("".join(json.dumps(event) + "\n" for event in events))
            if failure == "graph-control":
                (inputs / "inputs.bzl").write_text("# changed during build\n")
            elif failure is None:
                original = (inputs / "inputs.bzl").read_bytes()
                (inputs / "inputs.bzl").write_text("IMAGE_INPUTS = {\"epoch\": 999}\n")
                self.assertEqual(original, (graph / build.verify_inputs.INPUT_DIRECTORY / "inputs.bzl").read_bytes())
                (inputs / "inputs.bzl").write_bytes(original)

        for failure in ("incomplete", "failed", "outside-root", "wrong-receipt", "graph-control"):
            previous = self.receipt.read_bytes()
            graph = (inputs / "inputs.bzl").read_bytes()
            with self.subTest(failure=failure), mock.patch.object(build.subprocess, "run", execute):
                if failure == "incomplete":
                    saved = events.pop()
                elif failure == "failed":
                    events[-1]["finished"]["overallSuccess"] = False
                elif failure == "outside-root":
                    args.output_user_root = self.root / "different-state"
                elif failure == "wrong-receipt":
                    saved = events[1]["namedSetOfFiles"]["files"][0]["digest"]
                    events[1]["namedSetOfFiles"]["files"][0]["digest"] = "0" * 64
                with self.assertRaises(ValueError):
                    build.bazel_build(args)
                self.assertEqual(self.output.read_bytes(), b"previous-bazel-installer")
                self.assertEqual(self.receipt.read_bytes(), previous)
                if failure == "incomplete":
                    events.append(saved)
                elif failure == "failed":
                    events[-1]["finished"]["overallSuccess"] = True
                elif failure == "outside-root":
                    args.output_user_root = state
                elif failure == "wrong-receipt":
                    events[1]["namedSetOfFiles"]["files"][0]["digest"] = saved
                elif failure == "graph-control":
                    (inputs / "inputs.bzl").write_bytes(graph)
        failure = None
        with mock.patch.object(build.subprocess, "run", execute):
            build.bazel_build(args)
        self.assertEqual(self.output.read_bytes(), source.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o755)
        self.assertEqual(1, len(worker_paths))
        self.assertEqual(1, len(graph_paths))
        self.assertEqual(json.loads(self.receipt.read_text())["input_provenance"],
                         build.verify_inputs.bytes_identity(verified_receipt.read_bytes()))

    def test_public_build_checks_requests_against_verified_config_before_launch(self):
        inputs, receipt = self.prepared_bundle()
        from native import producer

        def reject(config, environment, flags):
            self.assertNotEqual(inputs / "installer/config.json", config)
            self.assertEqual((inputs / "installer/config.json").read_bytes(), config.read_bytes())
            self.assertEqual(receipt["native_config_flags"], flags)
            raise ValueError("unsupported request")

        with mock.patch.object(producer, "check_installer_request", side_effect=reject), \
                mock.patch.object(build.subprocess, "run") as launch:
            with self.assertRaisesRegex(ValueError, "unsupported request"):
                build.bazel_build(self.bazel_args())
        launch.assert_not_called()

    def test_public_build_rejects_unbound_bundle_without_changing_it(self):
        self.retained()
        inputs, receipt = self.prepared_bundle()
        receipt["schema"] = 1
        (inputs / "provenance.json").write_text(json.dumps(receipt))
        before = (inputs / "provenance.json").read_bytes()
        with mock.patch.object(build.subprocess, "run") as launch:
            with self.assertRaisesRegex(ValueError, "unbound or unsupported"):
                build.bazel_build(self.bazel_args())
        launch.assert_not_called()
        self.assertEqual(before, (inputs / "provenance.json").read_bytes())
        self.assertEqual(b"previous-bazel-installer", self.output.read_bytes())

    def make_workspace(self):
        source = Path(__file__).resolve().parents[3]
        shutil.copyfile(source / "Makefile", self.root / "Makefile")
        (self.root / "Makefile.work").write_text("%:\n\t@echo native-target:$@\n")
        (self.root / "rules").mkdir()
        (self.root / "rules/config").write_text(
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE ?= n\n"
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
