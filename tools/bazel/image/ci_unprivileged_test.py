#!/usr/bin/env python3
"""Reject altered handoffs and containers with unexpected privileges."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import ci_unprivileged as ci


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        self.handoff = self.root / "handoff"
        self.handoff.mkdir()
        for name in ("inputs.tar", "worker.tar", "native-provenance.json"):
            (self.handoff / name).write_bytes(name.encode())
        self.receipt = {"schema": 1, "source_commit": "a" * 40,
                        "worker_image": "sha256:" + "b" * 64,
                        "files": {path.name: ci.file_info(path) for path in self.handoff.iterdir()}}
        self.save()
        self.revision = patch.object(ci, "revision", return_value="a" * 40)
        self.revision.start()
        self.addCleanup(self.revision.stop)

    def save(self):
        ci.write_json(self.handoff / "handoff.json", self.receipt)

    def test_matching_handoff_is_bound_to_revision_and_all_files(self):
        self.assertEqual(ci.validate_handoff(self.workspace, self.handoff), self.receipt)
        (self.handoff / "inputs.tar").write_bytes(b"different native inputs")
        with self.assertRaisesRegex(ValueError, "checksum"):
            ci.validate_handoff(self.workspace, self.handoff)

    def test_other_revision_and_incomplete_manifest_are_rejected(self):
        self.receipt["source_commit"] = "c" * 40
        self.save()
        with self.assertRaisesRegex(ValueError, "revision"):
            ci.validate_handoff(self.workspace, self.handoff)
        self.receipt["source_commit"] = "a" * 40
        del self.receipt["files"]["worker.tar"]
        self.save()
        with self.assertRaisesRegex(ValueError, "inventory"):
            ci.validate_handoff(self.workspace, self.handoff)

    def archive(self, names):
        archive = self.root / "fixture.tar"
        with tarfile.open(archive, "w") as stream:
            for name, kind in names:
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                member.type = kind
                if kind == tarfile.SYMTYPE:
                    member.linkname = "/tmp/elsewhere"
                    stream.addfile(member)
                else:
                    member.size = 4
                    stream.addfile(member, io.BytesIO(b"data"))
        return archive

    def test_regular_inputs_are_restored_without_native_build(self):
        archive = self.archive([("target/bazel-image-inputs/config.json", tarfile.REGTYPE),
                                ("tools/bazel/image/vs/BUILD.bazel", tarfile.REGTYPE)])
        ci.extract_inputs(archive, self.workspace)
        self.assertEqual((self.workspace / "target/bazel-image-inputs/config.json").read_bytes(), b"data")

    def test_traversal_links_duplicates_and_unrelated_files_are_rejected(self):
        cases = [
            [("target/bazel-image-inputs/../../../outside", tarfile.REGTYPE)],
            [("target/bazel-image-inputs/link", tarfile.SYMTYPE)],
            [("target/bazel-image-inputs/repeated", tarfile.REGTYPE)] * 2,
            [(".git/config", tarfile.REGTYPE)],
        ]
        for members in cases:
            with self.subTest(members=members), self.assertRaises(ValueError):
                ci.extract_inputs(self.archive(members), self.workspace)
        self.assertFalse((self.workspace / "target").exists())

    def test_complete_job_handoff_restores_inputs_and_exact_worker(self):
        inputs = self.workspace / "target/bazel-image-inputs"
        prepared = inputs / "prepared-host"
        prepared.mkdir(parents=True)
        for name in ("fs.squashfs", "boot.tar", "platform.tar.gz", "receipt.json"):
            (prepared / name).write_bytes(("fixture " + name).encode())
        worker = "sha256:" + "b" * 64
        (inputs / "execution-environment.json").write_text(json.dumps({"worker_image": worker}))
        (inputs / "inputs.bzl").write_text("IMAGE_INPUTS = {}\n")
        package = self.workspace / "tools/bazel/image/vs"
        package.mkdir(parents=True)
        for name in ("BUILD.bazel", "BUILD.bazel.in"):
            (package / name).write_text("# fixture template\n")
        native = self.workspace / "target/bazel-native"
        native.mkdir()
        (native / "provenance.json").write_text("{}")
        bundle = self.root / "complete"
        def docker_save(argv, **kwargs):
            self.assertEqual(argv[:3], ["docker", "save", "--output"])
            Path(argv[3]).write_bytes(b"fixture worker tar")
        with patch.object(ci, "command", side_effect=docker_save):
            ci.pack(self.workspace, bundle)
        destination = self.root / "assembly"
        destination.mkdir()
        with patch.object(ci, "command") as command, patch.object(ci, "capture", return_value=worker + "\n"):
            ci.restore(destination, bundle)
        command.assert_called_once_with(["docker", "load", "--input", bundle / "worker.tar"])
        self.assertEqual((destination / "target/bazel-image-inputs/prepared-host/fs.squashfs").read_bytes(),
                         b"fixture fs.squashfs")
        self.assertTrue((destination / "artifacts/unprivileged/native-provenance.json").is_file())

    def test_native_state_remains_writable_to_the_runner_group(self):
        (self.workspace / "target").mkdir()
        target = ci.TARGET + "_host"
        outputs = set()
        for suffix in (".squashfs", ".boot.tar", ".platform.tar.gz", ".receipt.json"):
            path = self.root / ("host" + suffix)
            path.write_bytes(b"fixture output")
            outputs.add(path)
        with patch.object(ci, "command") as command, patch.object(ci.backend, "select_backend"), \
             patch.object(ci, "launcher", return_value=["fixture-launcher"]), \
             patch.object(ci, "distdir_args", return_value=["--distdir=/verified-downloads"]), \
             patch.object(ci, "prepare_worker_paths") as permissions, \
             patch.object(ci, "bep_outputs", return_value={target: outputs}), \
             patch.object(ci, "output_identity", side_effect=lambda events, path: ci.file_info(path)), \
             patch.object(ci.os, "getgid", return_value=1234):
            ci.prepare_host(self.workspace)
        state = self.workspace / "target/bazel-host-state"
        permissions.assert_called_once_with(self.workspace, [state],
                                            [self.workspace / "artifacts/unprivileged/host-events.jsonl"])
        build_calls = [call.args[0] for call in command.call_args_list if "build" in call.args[0]]
        self.assertEqual(len(build_calls), 1)
        self.assertIn("--distdir=/verified-downloads", build_calls[0])
        self.assertIn("--lockfile_mode=off", build_calls[0])
        self.assertIn("--symlink_prefix=/", build_calls[0])

    def test_existing_file_is_not_overwritten(self):
        existing = self.workspace / "target/bazel-image-inputs/config.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("keep")
        with self.assertRaisesRegex(ValueError, "replace"):
            ci.extract_inputs(self.archive([(str(existing.relative_to(self.workspace)), tarfile.REGTYPE)]), self.workspace)
        self.assertEqual(existing.read_text(), "keep")


class OwnershipSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.inputs = self.workspace / "target/bazel-image-inputs"
        self.inputs.mkdir(parents=True)
        (self.inputs / "execution-environment.json").write_text(json.dumps({"worker_image": "sha256:" + "b" * 64}))
        self.state = self.workspace / "target/state"
        self.state.mkdir()
        self.event = self.workspace / "events.jsonl"
        self.event.touch()

    def test_setup_uses_only_required_capabilities_and_explicit_mounts(self):
        with patch.object(ci, "command") as command, patch.object(ci.os, "getgid", return_value=1003):
            ci.prepare_worker_paths(self.workspace, [self.state], [self.event])
        argv = command.call_args.args[0]
        for option in ("--read-only", "--network=none", "--user=0:0", "--cap-drop=ALL",
                       "--cap-add=CHOWN", "--cap-add=FOWNER", "--security-opt=no-new-privileges"):
            self.assertIn(option, argv)
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("sudo", argv)
        self.assertNotIn("--cap-add=DAC_OVERRIDE", argv)
        mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
        self.assertEqual(mounts, ["type=bind,source=" + str(self.state) + ",target=/ownership/0",
                                  "type=bind,source=" + str(self.event) + ",target=/ownership/1"])
        self.assertEqual(argv[-2], "1003")
        self.assertEqual(json.loads(argv[-1]), [["/ownership/0", 0o775], ["/ownership/1", 0o664]])

    def test_setup_rejects_workspace_root_and_linked_paths(self):
        link = self.workspace / "state-link"
        link.symlink_to(self.state, target_is_directory=True)
        for path in (self.workspace, link, self.workspace.parent):
            with self.subTest(path=path), patch.object(ci, "command") as command:
                with self.assertRaisesRegex(ValueError, "explicit job paths"):
                    ci.prepare_worker_paths(self.workspace, [path])
                command.assert_not_called()


class SourceDownloadsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source downloads ")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.sources = {}
        self.archives = []
        for name in ("first", "second"):
            data = (name + " source archive").encode()
            self.sources[name] = ("1.0", "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode())
            path = self.workspace / "target/bazel-image-inputs/source-downloads" / (name + "-1.0") / "src.tar.gz"
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            self.archives.append(path)
        replacement = patch.object(ci, "SOURCES", self.sources)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_all_verified_archives_supply_separate_distdirs(self):
        self.assertEqual(ci.distdir_args(self.workspace),
                         ["--distdir=" + str(path.parent) for path in self.archives])

    def test_modified_missing_and_linked_archives_are_rejected(self):
        archive = self.archives[0]
        archive.write_bytes(b"changed bytes")
        with self.assertRaisesRegex(ValueError, "BCR SHA512"):
            ci.distdir_args(self.workspace)
        archive.unlink()
        with self.assertRaisesRegex(ValueError, "missing carried"):
            ci.distdir_args(self.workspace)
        archive.symlink_to(self.archives[1])
        with self.assertRaisesRegex(ValueError, "missing carried"):
            ci.distdir_args(self.workspace)


class PrivilegeTest(unittest.TestCase):
    def setUp(self):
        self.inspect = {"Image": "sha256:" + "b" * 64,
                        "Config": {"User": "1000:1000"},
                        "HostConfig": {"Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
                                       "SecurityOpt": ["no-new-privileges"], "NetworkMode": "bridge"},
                        "Mounts": []}
        self.runtime = {"uid": 1000, "gid": 1000, "docker_sockets": [], "docker_host": "",
                        "status": {"CapEff": "00000000", "CapPrm": "00000000", "CapBnd": "00000000",
                                   "NoNewPrivs": "1", "Seccomp": "2"}}

    def test_restricted_container_and_process_pass(self):
        report = ci.validate_restrictions(self.inspect, self.runtime)
        self.assertEqual(report["status"], "passed")

    def test_docker_configuration_cannot_hide_privileges(self):
        changes = [
            ("Privileged", True), ("CapAdd", ["SYS_ADMIN"]), ("CapDrop", []),
            ("SecurityOpt", ["no-new-privileges", "seccomp=unconfined"]),
            ("SecurityOpt", []), ("NetworkMode", "host"), ("PidMode", "host"),
            ("Devices", [{"PathOnHost": "/dev/kvm"}]),
        ]
        for key, value in changes:
            info = copy.deepcopy(self.inspect)
            info["HostConfig"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ci.validate_restrictions(info, self.runtime)

    def test_runtime_root_capabilities_seccomp_and_socket_fail(self):
        for key, value in (("uid", 0), ("gid", 0), ("docker_sockets", ["/run/docker.sock"]),
                           ("docker_host", "tcp://host:2375")):
            runtime = copy.deepcopy(self.runtime)
            runtime[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ci.validate_restrictions(self.inspect, runtime)
        for key, value in (("CapEff", "00000001"), ("CapPrm", "00000001"),
                           ("CapBnd", "00000001"), ("NoNewPrivs", "0"), ("Seccomp", "0")):
            runtime = copy.deepcopy(self.runtime)
            runtime["status"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ci.validate_restrictions(self.inspect, runtime)

    def test_mounted_socket_is_rejected_even_if_probe_did_not_see_it(self):
        self.inspect["Mounts"] = [{"Source": "/var/run/docker.sock", "Destination": "/private/daemon"}]
        with self.assertRaisesRegex(ValueError, "socket"):
            ci.validate_restrictions(self.inspect, self.runtime)


if __name__ == "__main__":
    unittest.main()
