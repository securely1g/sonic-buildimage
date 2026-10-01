#!/usr/bin/env python3
"""Check CI output provenance and cleanup without Docker or a full build."""

import argparse
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import image


class ImageControllerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        (self.workspace / "MODULE.bazel").write_text("module(name = 'fixture')\n")
        (self.workspace / ".bazelversion").write_text("8.5.1\n")
        self.state = self.root / "state"
        self.artifacts = self.workspace / "artifacts/image"
        self.output_root = self.root / "output"
        self.output_root.mkdir()
        self.output = self.output_root / "sonic-vs.bin"
        self.output.write_bytes(b"this build's output")
        self.bep = self.root / "bep.jsonl"
        self.lifecycle = []

        def clone(workspace, destination, source):
            self.lifecycle.append("clone")
            self.bazel_workspace = destination
            destination.mkdir()
            return {"schema": 1, "status": "passed", "source_commit": source["source_commit"]}

        def audit(*_args):
            self.lifecycle.append("audit")
            return {"schema": 1, "clean": False, "repositories": {
                "src/sonic-swss": {"changes": [{"path": "Cargo.lock", "kind": "file"}]}}}

        def verify(*_args):
            self.lifecycle.append("verify-source")
            return {"status": "passed", "clean": True}

        # Real Git isolation is exercised by source_workspace_test. These
        # controller fixtures verify ordering and the workspace at each edge.
        for name, implementation in (("clone", clone), ("audit", audit), ("verify", verify)):
            patcher = mock.patch.object(image.source_workspace, name, side_effect=implementation)
            setattr(self, name + "_source", patcher.start())
            self.addCleanup(patcher.stop)

    def events(self, output=None):
        return [
            {"id": {"targetCompleted": {"label": image.IMAGE}}, "completed": {
                "success": True, "outputGroup": [{"name": "default", "fileSets": [{"id": "parent"}]}]}},
            {"id": {"namedSet": {"id": "parent"}}, "namedSetOfFiles": {"fileSets": [{"id": "child"}]}},
            {"id": {"namedSet": {"id": "child"}}, "namedSetOfFiles": {
                "files": [{"uri": (output or self.output).as_uri()}]}},
            {"id": {"buildFinished": {}}, "finished": {
                "overallSuccess": True, "exitCode": {"name": "SUCCESS"}}},
        ]

    def resolve(self, events):
        self.bep.write_text("".join(json.dumps(event) + "\n" for event in events))
        return image.bep_outputs(self.bep, [image.IMAGE], self.output_root)

    def test_outputs_resolve_transitive_sets_independent_of_event_order(self):
        result = self.resolve(list(reversed(self.events())))
        self.assertEqual(result, {image.IMAGE: {self.output}})
        self.assertEqual(image.named_output(result, image.IMAGE, "sonic-vs.bin"), self.output)

    def test_successful_protojson_completion_accepts_omitted_or_explicit_zero(self):
        # Match the real Bazel 8.5.1 completion: code=0 is omitted, not null.
        for exit_code in ({"name": "SUCCESS"}, {"name": "SUCCESS", "code": 0}, {"code": 0}, {}):
            with self.subTest(exit_code=exit_code):
                events = self.events()
                events[-1]["finished"]["exitCode"] = exit_code
                self.assertEqual(self.resolve(events), {image.IMAGE: {self.output}})

    def test_missing_malformed_failed_and_contradictory_completions_are_rejected(self):
        completions = [
            None, {}, {"overallSuccess": True}, {"overallSuccess": True, "exitCode": None},
            {"overallSuccess": True, "exitCode": []}, {"exitCode": {"name": "SUCCESS"}},
            {"overallSuccess": False, "exitCode": {"name": "SUCCESS", "code": 0}},
            {"overallSuccess": 1, "exitCode": {"name": "SUCCESS"}},
            {"overallSuccess": True, "exitCode": {"name": "SUCCESS", "code": 1}},
            {"overallSuccess": True, "exitCode": {"name": "BUILD_FAILURE", "code": 0}},
            {"overallSuccess": True, "exitCode": {"name": "SUCCESS", "code": None}},
            {"overallSuccess": True, "exitCode": {"name": "SUCCESS", "code": False}},
            {"overallSuccess": True, "exitCode": {"name": "SUCCESS", "code": "0"}},
        ]
        for completion in completions:
            with self.subTest(completion=completion):
                events = self.events()
                events[-1]["finished"] = completion
                with self.assertRaises(ValueError):
                    self.resolve(events)
        events = self.events()
        events.append(events[-1])
        with self.assertRaisesRegex(ValueError, "ambiguous build completion"):
            self.resolve(events)

    def test_stale_external_output_and_symlink_escape_are_rejected(self):
        stale = self.workspace / "sonic-vs.bin"
        stale.write_bytes(b"a previous build")
        link = self.output_root / "stale-link"
        link.symlink_to(stale)
        for path in (stale, link):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "escapes"):
                self.resolve(self.events(path))

    def test_failed_missing_incomplete_and_cyclic_bep_fail_closed(self):
        failed_build = self.events()
        failed_build[-1]["finished"]["exitCode"]["code"] = 1
        failed_target = self.events()
        failed_target[0]["completed"]["success"] = False
        incomplete = self.events()
        incomplete[0]["completed"]["outputGroup"][0]["incomplete"] = True
        cycle = self.events()
        cycle[2]["namedSetOfFiles"]["fileSets"] = [{"id": "parent"}]
        for events in (failed_build, failed_target, incomplete, cycle,
                       self.events()[:-1], [self.events()[0], self.events()[-1]]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                self.resolve(events)

    def test_remote_uri_and_empty_output_are_rejected(self):
        remote = self.events()
        remote[2]["namedSetOfFiles"]["files"][0]["uri"] = "bytestream://cache/output"
        with self.assertRaisesRegex(ValueError, "local file"):
            self.resolve(remote)
        self.output.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.resolve(self.events())

    def test_mount_root_is_only_checkout_parent_and_rejects_worktree_git_file(self):
        paths = image.validate_paths(self.workspace, self.state, self.artifacts)
        self.assertEqual(paths[-1], self.root)
        for state in (self.root.parent / "outside-state", self.workspace / "state", self.root):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "state"):
                image.validate_paths(self.workspace, state, self.artifacts)
        (self.workspace / ".git").rmdir()
        (self.workspace / ".git").write_text("gitdir: /another/checkout/.git/worktrees/example\n")
        with self.assertRaisesRegex(ValueError, "standalone clone"):
            image.validate_paths(self.workspace, self.state, self.artifacts)

    def test_artifact_directory_cannot_publish_stale_results(self):
        self.artifacts.mkdir(parents=True)
        (self.artifacts / "sonic-vs.bin").write_bytes(b"old image")
        with self.assertRaisesRegex(ValueError, "stale"):
            image.validate_paths(self.workspace, self.state, self.artifacts)

    def test_publish_hardlinks_verified_bytes_without_copying(self):
        self.artifacts.mkdir(parents=True)
        with mock.patch.object(image.shutil, "copyfileobj") as copy:
            info = image.publish(self.output, self.artifacts, "sonic-vs.bin")
        copy.assert_not_called()
        published = self.artifacts / "sonic-vs.bin"
        self.assertEqual(published.stat().st_ino, self.output.stat().st_ino)
        self.assertEqual(info["publication_method"], "hardlink")
        self.assertEqual(info["sha256"], image.image_inputs.sha256(self.output))

    def test_publish_falls_back_to_exclusive_copy_only_across_filesystems(self):
        self.artifacts.mkdir(parents=True)
        with mock.patch.object(image.os, "link", side_effect=OSError(errno.EXDEV, "different filesystems")):
            info = image.publish(self.output, self.artifacts, "sonic-vs.bin")
        published = self.artifacts / "sonic-vs.bin"
        self.assertEqual(published.read_bytes(), self.output.read_bytes())
        self.assertNotEqual(published.stat().st_ino, self.output.stat().st_ino)
        self.assertEqual(info["publication_method"], "copy")
        with mock.patch.object(image.os, "link", side_effect=OSError(errno.EPERM, "not allowed")), \
                mock.patch.object(image.shutil, "copyfileobj") as copy, self.assertRaises(PermissionError):
            image.publish(self.output, self.artifacts, "another.bin")
        copy.assert_not_called()
        self.assertFalse((self.artifacts / "another.bin").exists())

    def test_publish_never_overwrites_existing_file_or_symlink(self):
        self.artifacts.mkdir(parents=True)
        existing = self.artifacts / "existing.bin"
        existing.write_bytes(b"keep existing bytes")
        linked = self.artifacts / "existing-link.bin"
        linked.symlink_to(existing)
        for name in (existing.name, linked.name):
            with self.subTest(name=name):
                with self.assertRaises(FileExistsError):
                    image.publish(self.output, self.artifacts, name)
                with mock.patch.object(image.os, "link", side_effect=OSError(errno.EXDEV, "different filesystems")), \
                        self.assertRaises(FileExistsError):
                    image.publish(self.output, self.artifacts, name)
        self.assertEqual(existing.read_bytes(), b"keep existing bytes")
        self.assertTrue(linked.is_symlink())

    def test_failed_build_stops_exact_worker_restores_owner_and_writes_failed_receipt(self):
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state,
                                       artifacts=self.artifacts)

        def prepare(*_args):
            inputs = _args[2] / "target/bazel-image-inputs"
            inputs.mkdir(parents=True)
            (inputs / "execution-environment.json").write_text('{"schema": 1}')
            (inputs / "host-config.json").write_text('{"identity": {"image_version": "frozen"}}')

        commands = []

        def execute(command, *_args):
            commands.append(command)
            if "build" in command:
                raise RuntimeError("fixture compilation failed")

        owner = (self.workspace.stat().st_uid, self.workspace.stat().st_gid)
        with mock.patch.object(image.os, "geteuid", return_value=0), \
                mock.patch.object(image.os, "chown"), \
                mock.patch.object(image, "chown_tree") as chown, \
                mock.patch.object(image, "build_worker", return_value=self.state / "worker-spec.json"), \
                mock.patch.object(image.image_inputs, "prepare", side_effect=prepare), \
                mock.patch.object(image, "source_provenance", return_value={"source_commit": "abc123"}), \
                mock.patch.object(image, "execute", side_effect=execute):
            self.assertEqual(image.build(arguments), 1)
        self.assertEqual(chown.call_args_list, [mock.call(self.workspace, 1000, 1000),
                         mock.call(self.state, 1000, 1000),
                         mock.call(self.bazel_workspace / "target", 1000, 1000),
                         mock.call(self.workspace, *owner)])
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[2], commands[1][:commands[1].index("--")] + ["--worker-action", "stop"])
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("fixture compilation failed", receipt["error"])
        self.assertFalse((self.artifacts / "sonic-vs.bin").exists())
        self.assertEqual(receipt["output_cleanup"]["status"], "retained")
        self.assertTrue(Path(receipt["output_user_root"]).is_dir())

    def test_worker_is_built_from_recipe_and_actual_bazel_version_is_checked(self):
        recipe = self.workspace / "tools/bazel/image/worker"
        recipe.mkdir(parents=True)
        for name in ("Dockerfile", ".dockerignore", "prepare-worker-inputs.sh"):
            (recipe / name).write_text("fixture " + name)
        installer = self.workspace / "tools/bazel/ci/trust.py"
        installer.parent.mkdir(parents=True)
        installer.write_text("fixture trust installer")
        self.state.mkdir()
        self.artifacts.mkdir(parents=True)
        worker_id = "sha256:" + "c" * 64
        commands = []
        def execute(command, *_args):
            commands.append(command)
            if "--iidfile" in command:
                Path(command[command.index("--iidfile") + 1]).write_text(worker_id)
        details = json.dumps([{"Id": worker_id, "Os": "linux", "Architecture": "amd64"}])
        for invocation, version in (("valid", "bazel 8.5.1"), ("invalid", "bazel 8.4.2")):
            receipt = {"commands": []}
            with mock.patch.object(image, "execute", side_effect=execute), \
                    mock.patch.object(image, "capture", side_effect=[details, version]):
                if invocation == "invalid":
                    with self.assertRaisesRegex(ValueError, "Bazel version"):
                        image.build_worker(self.workspace, self.state, self.artifacts, receipt, invocation)
                else:
                    spec = image.build_worker(self.workspace, self.state, self.artifacts, receipt, invocation)
                    self.assertEqual(json.loads(spec.read_text())["worker_image"], worker_id)
                    self.assertEqual(receipt["bazel_version"], "8.5.1")
                    self.assertFalse(receipt["execution_trust"]["enabled"])
                    self.assertEqual((spec.parent / "build-ca-bundle.pem").read_bytes(), b"")
                    self.assertEqual((spec.parent / "install-trust.py").read_text(), installer.read_text())
        self.assertEqual(commands[0][0], "bash")
        self.assertEqual(commands[1][:2], ["docker", "build"])
        self.assertFalse(any("load" in command for command in commands))

    def test_component_commit_must_match_gitlink_and_tree_must_be_clean(self):
        component = image.COMPONENTS[0]
        (self.workspace / component / ".git").mkdir(parents=True)
        responses = ["root-commit", "", "", "160000 commit recorded-commit\t" + component, "different-commit"]
        with mock.patch.object(image.subprocess, "check_output", side_effect=responses), \
                self.assertRaisesRegex(ValueError, "HEAD differs"):
            image.source_provenance(self.workspace)
        import subprocess
        with mock.patch.object(image.subprocess, "check_output", side_effect=[
                "root-commit", subprocess.CalledProcessError(1, ["git", "diff"])]), \
                self.assertRaises(subprocess.CalledProcessError):
            image.source_provenance(self.workspace)

    def test_untracked_root_source_fails_before_input_staging_or_worker_start(self):
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state,
                                       artifacts=self.artifacts)
        with mock.patch.object(image.os, "geteuid", return_value=1000), \
                mock.patch.object(image, "build_worker", return_value=self.state / "worker-spec.json"), \
                mock.patch.object(image.image_inputs, "prepare") as prepare, \
                mock.patch.object(image, "execute") as execute, \
                mock.patch.object(image.subprocess, "check_output", side_effect=["root-commit", "", "?? extra.cc\n"]):
            self.assertEqual(image.build(arguments), 1)
        prepare.assert_not_called()
        execute.assert_not_called()
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("untracked source files", receipt["error"])

    def test_untracked_component_source_is_rejected_even_if_parent_status_is_clean(self):
        component = image.COMPONENTS[0]
        (self.workspace / component / ".git").mkdir(parents=True)
        responses = ["root-commit", "", "", "160000 commit recorded-commit\t" + component,
                     "recorded-commit", "", "?? extra.cc\n"]
        with mock.patch.object(image.subprocess, "check_output", side_effect=responses), \
                self.assertRaisesRegex(ValueError, "checkout contains modified or untracked"):
            image.source_provenance(self.workspace)

    def test_all_recorded_recursive_submodules_are_checked(self):
        component = image.COMPONENTS[0]
        (self.workspace / component / ".git").mkdir(parents=True)
        (self.workspace / component / "nested/.git").mkdir(parents=True)
        responses = [
            "root", "", "", "160000 commit first\t" + component + "\0",
            "first", "", "", "160000 commit second\tnested\0",
            "second", "", "", "100644 blob ignored\tfile.cc\0",
        ]
        with mock.patch.object(image, "COMPONENTS", [component]), \
                mock.patch.object(image.subprocess, "check_output", side_effect=responses):
            result = image.source_provenance(self.workspace)
        self.assertEqual(result["components"], {
            component: {"commit": "first", "gitlink": "first"},
            component + "/nested": {"commit": "second", "gitlink": "second"}})
        (self.workspace / component / "nested/.git").rmdir()
        with mock.patch.object(image, "COMPONENTS", [component]), \
                mock.patch.object(image.subprocess, "check_output", side_effect=responses), \
                self.assertRaisesRegex(ValueError, "not initialized.*nested"):
            image.source_provenance(self.workspace)

    def test_native_cancellation_allows_owned_container_cleanup_to_finish(self):
        self.artifacts.mkdir(parents=True)
        for name, timeout in (("native-build", 105), ("package", 30)):
            with self.subTest(name=name):
                process = mock.Mock()
                process.stdout = mock.MagicMock()
                process.stdout.__iter__.side_effect = InterruptedError("cancelled")
                process.poll.return_value = None
                process.returncode = -15
                with mock.patch.object(image.subprocess, "Popen", return_value=process), \
                        self.assertRaisesRegex(InterruptedError, "cancelled"):
                    image.execute(["fixture"], self.workspace, self.artifacts, {"commands": []}, name)
                process.terminate.assert_called_once_with()
                process.wait.assert_called_once_with(timeout=timeout)
                process.kill.assert_not_called()

    def test_retained_native_outputs_are_rejected_before_worker_or_native_build(self):
        (self.workspace / "target").mkdir()
        (self.workspace / "target/docker-config-engine-trixie.gz").write_bytes(b"retained output")
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state, artifacts=self.artifacts)
        with mock.patch.object(image.os, "geteuid", return_value=1000), \
                mock.patch.object(image, "build_worker") as worker, \
                mock.patch.object(image, "execute") as execute:
            self.assertEqual(image.build(arguments), 1)
        worker.assert_not_called()
        execute.assert_not_called()
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        self.assertIn("without retained target outputs", receipt["error"])

    def test_native_failure_blocks_bazel_and_input_preparation(self):
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state, artifacts=self.artifacts)
        with mock.patch.object(image.os, "geteuid", return_value=1000), \
                mock.patch.object(image, "source_provenance", return_value={"source_commit": "abc123"}), \
                mock.patch.object(image, "build_worker", return_value=self.state / "worker-spec.json"), \
                mock.patch.object(image, "execute", side_effect=RuntimeError("native compilation failed")) as execute, \
                mock.patch.object(image.image_inputs, "prepare") as prepare:
            self.assertEqual(image.build(arguments), 1)
        execute.assert_called_once()
        self.assertTrue(execute.call_args.args[0][1].endswith("native_build.py"))
        prepare.assert_not_called()
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("native compilation failed", receipt["error"])
        self.assertEqual(receipt["output_cleanup"]["status"], "retained")
        self.clone_source.assert_called_once()
        self.audit_source.assert_called_once()
        self.verify_source.assert_not_called()
        self.assertTrue((self.artifacts / "native-source-audit.json").is_file())

    def run_build_fixture(self, failure=None, stop_hook=None):
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state,
                                       artifacts=self.artifacts)
        for cache in ("package-cache", "repository-cache"):
            (self.state / cache).mkdir(parents=True, exist_ok=True)
            (self.state / cache / "retained-entry").write_bytes(b"keep cache")

        def prepare(*_args):
            self.lifecycle.append("prepare")
            self.assertEqual(_args[1], self.workspace)
            self.assertEqual(_args[2], self.bazel_workspace)
            inputs = self.bazel_workspace / "target/bazel-image-inputs"
            inputs.mkdir(parents=True)
            (inputs / "execution-environment.json").write_text('{"schema": 1}')
            (inputs / "host-config.json").write_text('{"identity": {"image_version": "frozen"}}')

        commands = []

        def execute(command, *_args):
            self.lifecycle.append(_args[-1])
            self.assertEqual(_args[0], self.workspace if _args[-1] == "native-build" else self.bazel_workspace)
            commands.append(command)
            if _args[-1] == "native-build" and failure == "native":
                raise RuntimeError("fixture native compilation failed")
            if "--worker-action" in command:
                if failure == "worker-stop":
                    raise RuntimeError("fixture worker stop failed")
                if stop_hook is not None:
                    stop_hook(Path(command[command.index("--output-user-root") + 1]))
            if image.IMAGE in command:
                output_root = Path(command[command.index("--output-user-root") + 1])
                events = []
                for number, (target, names) in enumerate({
                    image.IMAGE: ["sonic-vs.bin"], image.RUNTIME: ["docker-orchagent.gz"],
                    image.IMAGE + "_host": ["sonic-vs.bin_host.squashfs", "sonic-vs.bin_host.boot.tar",
                                           "sonic-vs.bin_host.platform.tar.gz", "sonic-vs.bin_host.receipt.json"],
                    image.IMAGE + "_fs": ["sonic-vs.bin_fs.zip"],
                    image.IMAGE + "_dockerfs": ["sonic-vs.bin_dockerfs.tar.gz"],
                }.items()):
                    for name in names:
                        (output_root / name).write_bytes(b"built " + name.encode())
                    events.extend([
                        {"id": {"targetCompleted": {"label": target}}, "completed": {"success": True,
                            "outputGroup": [{"name": "default", "fileSets": [{"id": str(number)}]}]}},
                        {"id": {"namedSet": {"id": str(number)}}, "namedSetOfFiles": {
                            "files": [{"uri": (output_root / name).as_uri()} for name in names]}},
                    ])
                events.append({"id": {"buildFinished": {}}, "finished": {
                    "overallSuccess": True, "exitCode": {"name": "SUCCESS"}}})
                (self.artifacts / "image.bep.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
            if "--installer" in command:
                if failure == "verify":
                    raise RuntimeError("fixture verification failed")
                installer = Path(command[command.index("--installer") + 1])
                (self.artifacts / "image-verification.json").write_text(json.dumps({
                    "status": "passed", "installer": {"sha256": image.image_inputs.sha256(installer)}}))

        with mock.patch.object(image.os, "geteuid", return_value=1000), \
                mock.patch.object(image, "build_worker", return_value=self.state / "worker-spec.json"), \
                mock.patch.object(image.image_inputs, "prepare", side_effect=prepare), \
                mock.patch.object(image, "source_provenance", return_value={"source_commit": "abc123"}), \
                mock.patch.object(image, "execute", side_effect=execute):
            result = image.build(arguments)
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        return result, commands, receipt

    def test_success_reuses_worker_disables_full_image_disk_cache_and_publishes_verified_bytes(self):
        result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 0)
        self.assertEqual(self.lifecycle, ["clone", "native-build", "audit", "verify-source", "prepare",
                                         "package", "image", "verify-image", "worker-stop"])
        self.assertEqual(len(commands), 5)
        native = commands.pop(0)
        self.assertTrue(native[1].endswith("tools/bazel/ci/native_build.py"))
        self.assertIn("--invocation", native)
        self.assertIn("--worker-spec", native)
        self.assertEqual(native[native.index("--workspace") + 1], str(self.workspace))
        self.assertIn("--disk_cache=" + str(self.state / "package-cache"), commands[0])
        self.assertIn("--disk_cache=", commands[1])
        for command in (commands[0], commands[1], commands[-1]):
            self.assertEqual(command[command.index("--workspace") + 1], str(self.bazel_workspace))
            self.assertEqual(command[command.index("--worker-cpus") + 1], "4")
            self.assertEqual(command[command.index("--worker-memory-gib") + 1], "12")
        self.assertIn("--local_resources=memory=10000", commands[1])
        self.assertEqual(commands[0][:commands[0].index("--")], commands[1][:commands[1].index("--")])
        self.assertEqual(commands[-1][-2:], ["--worker-action", "stop"])
        self.assertEqual((self.artifacts / "sonic-vs.bin").read_bytes(), b"built sonic-vs.bin")
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["bazel_source"]["verification_after_native"]["clean"])
        self.assertFalse(json.loads((self.artifacts / "native-source-audit.json").read_text())["clean"])
        self.assertEqual(set(receipt["outputs"]), {"installer", "runtime"})
        self.assertEqual(len((self.artifacts / "SHA256SUMS").read_text().splitlines()), 2)
        self.assertEqual(receipt["output_cleanup"]["status"], "removed")
        self.assertFalse(Path(receipt["output_user_root"]).exists())
        self.assertEqual((self.artifacts / "docker-orchagent.gz").read_bytes(), b"built docker-orchagent.gz")
        for output in receipt["outputs"].values():
            self.assertEqual(output["publication_method"], "hardlink")
            self.assertEqual(image.image_inputs.sha256(self.artifacts / output["file"]), output["sha256"])
        for cache in ("package-cache", "repository-cache"):
            self.assertEqual((self.state / cache / "retained-entry").read_bytes(), b"keep cache")

    def test_changed_pristine_checkout_blocks_staging_and_bazel(self):
        self.verify_source.side_effect = ValueError("pristine checkout contains ignored source")
        result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0][1].endswith("native_build.py"))
        self.assertNotIn("prepare", self.lifecycle)
        self.assertIn("ignored source", receipt["error"])
        self.assertFalse((self.artifacts / "sonic-vs.bin").exists())

    def test_clone_failure_blocks_native_build(self):
        self.clone_source.side_effect = ValueError("recorded source object is missing")
        result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(commands, [])
        self.assertIn("source object", receipt["error"])
        self.audit_source.assert_not_called()

    def test_native_audit_failure_blocks_staging_after_successful_make(self):
        self.audit_source.side_effect = OSError("source audit unavailable")
        result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(len(commands), 1)
        self.assertIn("audit unavailable", receipt["native_source_audit_error"])
        self.verify_source.assert_not_called()

    def test_native_audit_failure_does_not_hide_native_build_failure(self):
        self.audit_source.side_effect = OSError("source audit unavailable")
        result, commands, receipt = self.run_build_fixture(failure="native")
        self.assertEqual(result, 1)
        self.assertEqual(len(commands), 1)
        self.assertIn("native compilation failed", receipt["error"])
        self.assertIn("audit unavailable", receipt["native_source_audit_error"])
        self.verify_source.assert_not_called()
        self.assertNotIn("prepare", self.lifecycle)

    def test_verification_failure_retains_this_invocations_output(self):
        result, commands, receipt = self.run_build_fixture(failure="verify")
        self.assertEqual(result, 1)
        self.assertEqual(commands[-1][-2:], ["--worker-action", "stop"])
        self.assertEqual(receipt["output_cleanup"]["status"], "retained")
        self.assertTrue((Path(receipt["output_user_root"]) / "sonic-vs.bin").is_file())
        self.assertFalse((self.artifacts / "sonic-vs.bin").exists())

    def test_worker_stop_failure_retains_output_even_after_successful_publication(self):
        result, _, receipt = self.run_build_fixture(failure="worker-stop")
        self.assertEqual(result, 1)
        self.assertIn("fixture worker stop failed", receipt["worker_cleanup_error"])
        self.assertEqual(receipt["output_cleanup"]["status"], "retained")
        original = Path(receipt["output_user_root"]) / "sonic-vs.bin"
        self.assertTrue(original.is_file())
        self.assertEqual(original.stat().st_ino, (self.artifacts / "sonic-vs.bin").stat().st_ino)

    def test_publication_failure_retains_output_after_worker_stop(self):
        with mock.patch.object(image, "publish", side_effect=OSError("fixture publication failed")):
            result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(commands[-1][-2:], ["--worker-action", "stop"])
        self.assertEqual(receipt["output_cleanup"]["status"], "retained")
        self.assertTrue((Path(receipt["output_user_root"]) / "sonic-vs.bin").is_file())

    def test_output_cleanup_error_fails_the_receipt(self):
        with mock.patch.object(image.shutil, "rmtree", side_effect=OSError("fixture cleanup failed")):
            result, _, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(receipt["output_cleanup"]["status"], "failed")
        self.assertIn("fixture cleanup failed", receipt["output_cleanup"]["error"])
        self.assertTrue(Path(receipt["output_user_root"]).is_dir())
        self.assertEqual((self.artifacts / "sonic-vs.bin").read_bytes(), b"built sonic-vs.bin")

    def test_cleanup_refuses_a_replaced_output_directory(self):
        def replace(root):
            root.rename(self.state / "retained-original-output")
            root.mkdir()
            (root / "unrelated").write_bytes(b"do not delete")
        result, _, receipt = self.run_build_fixture(stop_hook=replace)
        self.assertEqual(result, 1)
        self.assertEqual(receipt["output_cleanup"]["status"], "failed")
        self.assertIn("replaced invocation", receipt["output_cleanup"]["error"])
        self.assertEqual((Path(receipt["output_user_root"]) / "unrelated").read_bytes(), b"do not delete")
        self.assertTrue((self.state / "retained-original-output/sonic-vs.bin").is_file())

    def test_cleanup_refuses_output_root_replaced_by_cache_symlink(self):
        def replace(root):
            root.rename(self.state / "retained-original-output")
            root.symlink_to(self.state / "package-cache", target_is_directory=True)
        result, _, receipt = self.run_build_fixture(stop_hook=replace)
        self.assertEqual(result, 1)
        self.assertEqual(receipt["output_cleanup"]["status"], "failed")
        self.assertTrue(Path(receipt["output_user_root"]).is_symlink())
        self.assertEqual((self.state / "package-cache/retained-entry").read_bytes(), b"keep cache")

    def test_existing_output_root_is_not_adopted_or_cleaned_up(self):
        self.state.mkdir()
        existing = self.state / "output-0123456789abcdef"
        existing.mkdir()
        (existing / "unrelated").write_bytes(b"do not delete")
        with mock.patch.object(image.uuid, "uuid4", return_value=mock.Mock(hex="0123456789abcdef")):
            result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(commands, [])
        self.assertEqual(receipt["output_cleanup"]["status"], "not_created")
        self.assertEqual((existing / "unrelated").read_bytes(), b"do not delete")

    def test_artifacts_inside_selected_output_root_are_rejected_before_building(self):
        self.artifacts = self.state / "output-0123456789abcdef/artifacts"
        with mock.patch.object(image.uuid, "uuid4", return_value=mock.Mock(hex="0123456789abcdef")):
            result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(commands, [])
        self.assertIn("artifacts cannot be inside", receipt["error"])
        self.assertEqual(receipt["output_cleanup"]["status"], "not_created")

    def test_preexisting_output_root_symlink_is_rejected_without_touching_target(self):
        self.state.mkdir()
        target = self.state / "unrelated"
        target.mkdir()
        (target / "keep").write_bytes(b"do not delete")
        selected = self.state / "output-0123456789abcdef"
        selected.symlink_to(target, target_is_directory=True)
        with mock.patch.object(image.uuid, "uuid4", return_value=mock.Mock(hex="0123456789abcdef")):
            result, commands, receipt = self.run_build_fixture()
        self.assertEqual(result, 1)
        self.assertEqual(commands, [])
        self.assertIn("must not be a symlink", receipt["error"])
        self.assertEqual(receipt["output_cleanup"]["status"], "not_created")
        self.assertEqual((target / "keep").read_bytes(), b"do not delete")


if __name__ == "__main__":
    unittest.main()
