#!/usr/bin/env python3
"""Check CI output provenance and cleanup without Docker or a full build."""

import argparse
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

    def events(self, output=None):
        return [
            {"id": {"targetCompleted": {"label": image.IMAGE}}, "completed": {
                "success": True, "outputGroup": [{"name": "default", "fileSets": [{"id": "parent"}]}]}},
            {"id": {"namedSet": {"id": "parent"}}, "namedSetOfFiles": {"fileSets": [{"id": "child"}]}},
            {"id": {"namedSet": {"id": "child"}}, "namedSetOfFiles": {
                "files": [{"uri": (output or self.output).as_uri()}]}},
            {"id": {"buildFinished": {}}, "finished": {"exitCode": {"code": 0}}},
        ]

    def resolve(self, events):
        self.bep.write_text("".join(json.dumps(event) + "\n" for event in events))
        return image.bep_outputs(self.bep, [image.IMAGE], self.output_root)

    def test_outputs_resolve_transitive_sets_independent_of_event_order(self):
        result = self.resolve(list(reversed(self.events())))
        self.assertEqual(result, {image.IMAGE: {self.output}})
        self.assertEqual(image.named_output(result, image.IMAGE, "sonic-vs.bin"), self.output)

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

    def test_failed_build_stops_exact_worker_restores_owner_and_writes_failed_receipt(self):
        manifest = self.root / "inputs.json"
        manifest.write_text("{}")
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state,
                                       artifacts=self.artifacts, manifest=manifest, local_assets=None)

        def prepare(*_args):
            inputs = self.workspace / "target/bazel-image-inputs"
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
                mock.patch.object(image.image_inputs, "load_manifest", return_value={"worker": {"bazel_version": "8.5.1"}}), \
                mock.patch.object(image.image_inputs, "prepare", side_effect=prepare), \
                mock.patch.object(image, "source_provenance", return_value={"source_commit": "abc123"}), \
                mock.patch.object(image, "execute", side_effect=execute):
            self.assertEqual(image.build(arguments), 1)
        self.assertEqual(chown.call_args_list, [mock.call(self.workspace, 1000, 1000),
                         mock.call(self.state, 1000, 1000), mock.call(self.workspace, *owner)])
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[1], commands[0][:commands[0].index("--")] + ["--worker-action", "stop"])
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("fixture compilation failed", receipt["error"])
        self.assertFalse((self.artifacts / "sonic-vs.bin").exists())

    def test_worker_bazel_version_must_match_checked_out_version(self):
        self.assertEqual(image.check_bazel_version({"worker": {"bazel_version": "8.5.1"}}, self.workspace), "8.5.1")
        for worker in ({}, {"bazel_version": "8.4.2"}):
            with self.subTest(worker=worker), self.assertRaisesRegex(ValueError, "Bazel version"):
                image.check_bazel_version({"worker": worker}, self.workspace)

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
        manifest = self.root / "inputs.json"
        manifest.write_text("{}")
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state,
                                       artifacts=self.artifacts, manifest=manifest, local_assets=None)
        with mock.patch.object(image.os, "geteuid", return_value=1000), \
                mock.patch.object(image.image_inputs, "load_manifest", return_value={"worker": {"bazel_version": "8.5.1"}}), \
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
                self.assertRaisesRegex(ValueError, "component contains modified or untracked"):
            image.source_provenance(self.workspace)

    def test_success_reuses_worker_disables_full_image_disk_cache_and_publishes_verified_bytes(self):
        manifest = self.root / "inputs.json"
        manifest.write_text("{}")
        arguments = argparse.Namespace(workspace=self.workspace, state=self.state,
                                       artifacts=self.artifacts, manifest=manifest, local_assets=None)

        def prepare(*_args):
            inputs = self.workspace / "target/bazel-image-inputs"
            inputs.mkdir(parents=True)
            (inputs / "execution-environment.json").write_text('{"schema": 1}')
            (inputs / "host-config.json").write_text('{"identity": {"image_version": "frozen"}}')

        commands = []

        def execute(command, *_args):
            commands.append(command)
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
                events.append({"id": {"buildFinished": {}}, "finished": {"exitCode": {"code": 0}}})
                (self.artifacts / "image.bep.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))
            if "--installer" in command:
                installer = Path(command[command.index("--installer") + 1])
                (self.artifacts / "image-verification.json").write_text(json.dumps({
                    "status": "passed", "installer": {"sha256": image.image_inputs.sha256(installer)}}))

        with mock.patch.object(image.os, "geteuid", return_value=1000), \
                mock.patch.object(image.image_inputs, "load_manifest", return_value={"worker": {"bazel_version": "8.5.1"}}), \
                mock.patch.object(image.image_inputs, "prepare", side_effect=prepare), \
                mock.patch.object(image, "source_provenance", return_value={"source_commit": "abc123"}), \
                mock.patch.object(image, "execute", side_effect=execute):
            self.assertEqual(image.build(arguments), 0)
        self.assertEqual(len(commands), 4)
        self.assertIn("--disk_cache=" + str(self.state / "package-cache"), commands[0])
        self.assertIn("--disk_cache=", commands[1])
        for command in (commands[0], commands[1], commands[-1]):
            self.assertEqual(command[command.index("--worker-cpus") + 1], "4")
            self.assertEqual(command[command.index("--worker-memory-gib") + 1], "12")
        self.assertIn("--local_resources=memory=10000", commands[1])
        self.assertEqual(commands[0][:commands[0].index("--")], commands[1][:commands[1].index("--")])
        self.assertEqual(commands[-1][-2:], ["--worker-action", "stop"])
        self.assertEqual((self.artifacts / "sonic-vs.bin").read_bytes(), b"built sonic-vs.bin")
        receipt = json.loads((self.artifacts / "image-receipt.json").read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(set(receipt["outputs"]), {"installer", "runtime"})
        self.assertEqual(len((self.artifacts / "SHA256SUMS").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
