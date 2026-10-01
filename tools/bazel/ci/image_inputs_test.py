#!/usr/bin/env python3
"""Fail closed when a native-input release differs from its pinned manifest."""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

import image_inputs


PREFIX = "target/bazel-image-inputs/"


def metadata(content, mode=0o644):
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}


def write_archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind, mode in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = mode
            if kind == tarfile.REGTYPE:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            else:
                member.linkname = "outside"
                archive.addfile(member)


def regular(name, content=b"native input", mode=0o644):
    return name, content, tarfile.REGTYPE, mode


class InputVerificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.archive = self.assets / "inputs-01.tar.gz"
        self.name = PREFIX + "host-source.tar"
        self.content = b"native predecessor\x00bytes"
        self.expected = {self.name: metadata(self.content)}
        write_archive(self.archive, [regular(self.name, self.content)])

    def installer_sources(self):
        files = {
            "installer/install.sh": b"#!/bin/sh\n# current checkout installer\n",
            "installer/sharch_body.sh": b"#!/bin/sh\nexit_marker\n",
            "installer/default_platform.conf": b"CURRENT_PLATFORM=yes\n",
            "onie-image.conf": b"CURRENT_ONIE=yes\n",
            "platform/vs/platform.conf": b"PLATFORM=vs\n",
            "platform/vs/platform-modules-vs.mk": b"$(VS_PLATFORM_MODULE)_PLATFORM = x86_64-kvm_x86_64-r0\n",
            "device/virtual/x86_64-kvm_x86_64-r0/installer.conf": b"VAR_LOG_SIZE=1024\n",
        }
        for name, content in files.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755 if name.endswith(".sh") else 0o644)
        return files

    def installer_bundle(self):
        attributes = {
            "image_version": "native.fixture", "epoch": 0,
            "installer_config": "//target/bazel-image-inputs:installer/config.json",
            "installer_files": {"//target/bazel-image-inputs:installer/files/install.sh": "install.sh"},
            "installer_modes": {"install.sh": "493"},
            "source": "//target/bazel-image-inputs:host-source.tar",
            "images": {"docker-orchagent.gz": "//dockers/docker-orchagent:docker-orchagent.gz"},
        }
        return {
            PREFIX + "inputs.bzl": ("IMAGE_INPUTS = " + json.dumps(attributes) + "\n").encode(),
            PREFIX + "installer/config.json": b'{"image_version": "native.fixture"}',
            PREFIX + "installer/files/install.sh": b"#!/bin/sh\n# old release installer\n",
            PREFIX + "installer/files/obsolete.sh": b"# absent from current checkout\n",
        }

    def test_verified_copy_accepts_exact_content_and_rejects_wrong_hash_or_size(self):
        output = io.BytesIO()
        image_inputs.copy_verified(io.BytesIO(self.content), output, metadata(self.content), "fixture")
        self.assertEqual(output.getvalue(), self.content)
        for actual in (self.content[:-1], self.content + b"x", b"X" + self.content[1:]):
            with self.subTest(actual=actual):
                with self.assertRaisesRegex(ValueError, "size|SHA256"):
                    image_inputs.copy_verified(io.BytesIO(actual), io.BytesIO(), metadata(self.content), "fixture")

    def test_fetch_checks_asset_digest_before_extraction(self):
        asset = metadata(self.archive.read_bytes()) | {"name": self.archive.name}
        downloaded = self.root / "downloaded.tar.gz"
        image_inputs.fetch_asset(asset, "https://unused.invalid", downloaded, self.assets)
        self.assertEqual(downloaded.read_bytes(), self.archive.read_bytes())
        # The complete gzip stream remains valid, but these new bytes were not
        # the release asset approved in the manifest.
        write_archive(self.archive, [regular(self.name, b"another predecessor")])
        with self.assertRaisesRegex(ValueError, "size|SHA256"):
            image_inputs.fetch_asset(asset, "https://unused.invalid", self.root / "corrupt.tar.gz", self.assets)

    def test_fetch_refuses_to_overwrite_existing_download(self):
        downloaded = self.root / "existing"
        downloaded.write_bytes(b"keep me")
        asset = metadata(self.archive.read_bytes()) | {"name": self.archive.name}
        with self.assertRaises(FileExistsError):
            image_inputs.fetch_asset(asset, "https://unused.invalid", downloaded, self.assets)
        self.assertEqual(downloaded.read_bytes(), b"keep me")

    def test_extract_preserves_verified_bytes_and_declared_mode(self):
        executable = PREFIX + "installer/files/install.sh"
        write_archive(self.archive, [regular(self.name, self.content), regular(executable, b"#!/bin/sh\n", 0o755)])
        expected = self.expected | {executable: metadata(b"#!/bin/sh\n", 0o755)}
        image_inputs.extract_inputs(self.archive, expected, self.workspace)
        self.assertEqual((self.workspace / self.name).read_bytes(), self.content)
        self.assertEqual((self.workspace / executable).stat().st_mode & 0o777, 0o755)

    def test_manifest_file_digest_is_enforced_independently_of_archive_digest(self):
        altered = b"X" + self.content[1:]
        write_archive(self.archive, [regular(self.name, altered)])
        # Fetch accepts this exact asset; extraction must still enforce the
        # separately pinned digest for its member.
        asset = metadata(self.archive.read_bytes()) | {"name": self.archive.name}
        downloaded = self.root / "verified-archive.tar.gz"
        image_inputs.fetch_asset(asset, "https://unused.invalid", downloaded, self.assets)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            image_inputs.extract_inputs(downloaded, self.expected, self.workspace)

    def test_paths_cannot_escape_or_supply_undeclared_build_outputs(self):
        for name in (
            "../escaped", "/target/bazel-image-inputs/absolute", PREFIX + "../../escaped",
            "target//bazel-image-inputs/file", PREFIX + "./file", "target/sonic-vs.bin",
            "bazel-out/sonic-vs.bin", PREFIX.rstrip("/"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    image_inputs.input_path(name)
                write_archive(self.archive, [regular(name, self.content)])
                with self.assertRaises(ValueError):
                    image_inputs.extract_inputs(self.archive, {name: metadata(self.content)}, self.workspace)
        self.assertFalse((self.root / "escaped").exists())

    def test_symlinks_hardlinks_devices_directories_and_fifos_are_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                     tarfile.BLKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                write_archive(self.archive, [(self.name, b"", kind, 0o644)])
                with self.assertRaisesRegex(ValueError, "non-regular"):
                    image_inputs.extract_inputs(self.archive, {self.name: metadata(b"")}, self.workspace)
                self.assertFalse((self.workspace / self.name).exists())

    def test_duplicate_member_is_rejected_without_overwriting_first_member(self):
        write_archive(self.archive, [regular(self.name, self.content), regular(self.name, b"overwrite")])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            image_inputs.extract_inputs(self.archive, self.expected, self.workspace)
        self.assertEqual((self.workspace / self.name).read_bytes(), self.content)

    def test_missing_or_unexpected_members_fail(self):
        missing = PREFIX + "missing.tar"
        with self.assertRaisesRegex(ValueError, "omits declared inputs"):
            image_inputs.extract_inputs(self.archive, self.expected | {missing: metadata(b"missing")}, self.workspace)
        self.assertFalse((self.workspace / missing).exists())
        write_archive(self.archive, [regular(missing, b"unexpected")])
        with self.assertRaisesRegex(ValueError, "unexpected"):
            image_inputs.extract_inputs(self.archive, self.expected, self.workspace)
        self.assertFalse((self.workspace / missing).exists())

    def test_member_size_and_mode_must_match_manifest(self):
        for content, mode in ((self.content + b"extra", 0o644), (self.content, 0o755)):
            with self.subTest(mode=mode, bytes=len(content)):
                write_archive(self.archive, [regular(self.name, content, mode)])
                with self.assertRaisesRegex(ValueError, "size/mode"):
                    image_inputs.extract_inputs(self.archive, self.expected, self.workspace)
                self.assertFalse((self.workspace / self.name).exists())

    def test_existing_file_or_symlink_cannot_be_overwritten(self):
        output = self.workspace / self.name
        output.parent.mkdir(parents=True)
        output.write_bytes(b"existing input")
        with self.assertRaises(FileExistsError):
            image_inputs.extract_inputs(self.archive, self.expected, self.workspace)
        self.assertEqual(output.read_bytes(), b"existing input")
        output.unlink()
        existing = self.workspace / "existing"
        existing.write_bytes(b"symlink target")
        output.symlink_to(existing)
        with self.assertRaises(FileExistsError):
            image_inputs.extract_inputs(self.archive, self.expected, self.workspace)
        self.assertTrue(output.is_symlink())
        self.assertEqual(existing.read_bytes(), b"symlink target")

    def test_parent_symlink_cannot_redirect_extraction_outside_workspace(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "target").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes workspace"):
            image_inputs.extract_inputs(self.archive, self.expected, self.workspace)
        self.assertEqual(list(outside.iterdir()), [])

    def test_installer_refresh_tracks_current_sources_and_preserves_native_inputs(self):
        self.installer_sources()
        for name, content in self.installer_bundle().items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        native = self.workspace / self.name
        native.write_bytes(self.content)
        inputs_file = self.workspace / PREFIX / "inputs.bzl"
        original = image_inputs.read_image_inputs(inputs_file)
        source = self.workspace / "installer/install.sh"
        staged = self.workspace / PREFIX / "installer/files/install.sh"
        with mock.patch.object(image_inputs.subprocess, "check_output", return_value="a" * 40 + "\n"):
            first = image_inputs.refresh_installer_inputs(self.workspace)
            self.assertEqual(staged.read_bytes(), source.read_bytes())
            self.assertNotEqual(first["released_files"][PREFIX + "installer/files/install.sh"]["sha256"],
                                first["derived_files"][PREFIX + "installer/files/install.sh"]["sha256"])
            self.assertFalse((staged.parent / "obsolete.sh").exists())
            source.write_bytes(source.read_bytes() + b"# one-line PR change\n")
            (source.parent / "new-helper.sh").write_bytes(b"#!/bin/sh\necho current\n")
            second = image_inputs.refresh_installer_inputs(self.workspace)
        self.assertEqual(staged.read_bytes(), source.read_bytes())
        self.assertNotEqual(first["derived_files"][PREFIX + "installer/files/install.sh"]["sha256"],
                            second["derived_files"][PREFIX + "installer/files/install.sh"]["sha256"])
        self.assertEqual(second["source_commit"], "a" * 40)
        self.assertEqual(second["source_provenance"]["source_sha256"]["installer/install.sh"],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        updated = image_inputs.read_image_inputs(inputs_file)
        self.assertEqual({k: v for k, v in original.items() if k not in {"installer_files", "installer_modes"}},
                         {k: v for k, v in updated.items() if k not in {"installer_files", "installer_modes"}})
        self.assertIn("new-helper.sh", updated["installer_files"].values())
        self.assertEqual(native.read_bytes(), self.content)
        for relative, info in second["derived_files"].items():
            self.assertEqual(info["sha256"], hashlib.sha256((self.workspace / relative).read_bytes()).hexdigest())

    def test_input_mapping_parser_never_executes_code(self):
        inputs_file = self.root / "inputs.bzl"
        sentinel = self.root / "executed"
        expression = "__import__('pathlib').Path(" + repr(str(sentinel)) + ").write_text('bad')"
        for text in ("IMAGE_INPUTS = {\"payload\": " + expression + "}\n",
                     "IMAGE_INPUTS = {}\n" + expression + "\n",
                     "OTHER_INPUTS = {}\n"):
            with self.subTest(text=text):
                inputs_file.write_text(text)
                with self.assertRaises(ValueError):
                    image_inputs.read_image_inputs(inputs_file)
                self.assertFalse(sentinel.exists())

    def test_prepare_receipt_records_only_assets_whose_pinned_bytes_passed(self):
        worker_image = "sha256:" + "1" * 64
        self.installer_sources()
        files = {name: b"fixture predecessor" for name in (
            PREFIX + "inputs.bzl", PREFIX + "BUILD.bazel", PREFIX + "host-onie.squashfs",
            PREFIX + "host-config.json", "target/docker-config-engine-trixie.gz",
            "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl",
        )}
        files[self.name] = self.content
        files.update(self.installer_bundle())
        write_archive(self.archive, [regular(name, content) for name, content in files.items()])
        asset = metadata(self.archive.read_bytes()) | {
            "name": self.archive.name, "kind": "inputs",
            "files": {name: metadata(content) for name, content in files.items()},
        }
        worker = self.assets / "worker.tar.gz"
        worker.write_bytes(b"worker fixture for mocked Docker loader")
        worker_asset = metadata(worker.read_bytes()) | {"name": worker.name, "kind": "worker"}
        environment = {"schema": 1, "platform": "linux/amd64", "docker_version": "28.5.2",
                       "storage_driver": "overlay2", "distribution": "trixie"}
        manifest = {
            "schema": 1, "worker": {"reference": "sonic-bazel-vs-worker:test",
                                     "image_ids": [worker_image], "environment": environment},
            "release_url": "https://github.com/securely1g/sonic-buildimage/releases/download/test-inputs",
            "assets": [asset, worker_asset],
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest))
        template = self.workspace / "tools/bazel/image/vs/BUILD.bazel.in"
        template.parent.mkdir(parents=True)
        template.write_text("# test BUILD template\n")
        receipt_path = self.root / "receipt.json"
        inspection = json.dumps([{"Id": worker_image, "Os": "linux", "Architecture": "amd64"}])

        def inspect_or_revision(command, **_kwargs):
            return inspection if command[0] == "docker" else "a" * 40 + "\n"

        # Only Docker process execution is mocked. Manifest validation, asset
        # fetching, digest checks, extraction and receipt writing all run.
        with mock.patch.object(image_inputs.subprocess, "run"), \
                mock.patch.object(image_inputs.subprocess, "check_output", side_effect=inspect_or_revision):
            image_inputs.prepare(path, self.workspace, self.root / "scratch", receipt_path, self.assets)
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["manifest_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(receipt["assets"], [
            {key: entry[key] for key in ("name", "bytes", "sha256", "kind")} for entry in manifest["assets"]
        ])
        self.assertEqual((template.parent / "BUILD.bazel").read_bytes(), template.read_bytes())
        execution = self.workspace / PREFIX / "execution-environment.json"
        self.assertEqual(json.loads(execution.read_text()), environment | {"worker_image": worker_image})
        self.assertEqual(receipt["execution_environment_sha256"], hashlib.sha256(execution.read_bytes()).hexdigest())
        self.assertEqual(receipt["installer_refresh"]["source_commit"], "a" * 40)
        self.assertEqual((self.workspace / PREFIX / "installer/files/install.sh").read_bytes(),
                         (self.workspace / "installer/install.sh").read_bytes())
        # A valid archive with a wrong pinned file digest must leave a failure
        # receipt and cannot be reported as a successfully staged asset.
        failed_workspace = self.root / "failed-workspace"
        failed_workspace.mkdir()
        asset["files"][self.name]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
        with mock.patch.object(image_inputs.subprocess, "run") as docker:
            with self.assertRaisesRegex(ValueError, "SHA256"):
                image_inputs.prepare(path, failed_workspace, self.root / "scratch", receipt_path, self.assets)
            docker.assert_not_called()
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["assets"], [])
        self.assertFalse((failed_workspace / "tools/bazel/image/vs/BUILD.bazel").exists())


if __name__ == "__main__":
    unittest.main()
