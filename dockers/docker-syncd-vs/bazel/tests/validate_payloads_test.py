#!/usr/bin/env python3
"""Check the OCI input validator using tar fixtures; this test creates no DEBs."""

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile
import unittest

OWNER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OWNER.parents[1]))
sys.path.insert(0, str(OWNER / "bazel"))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, tar_entries, write_layout
import validate_payloads as subject


class ValidatePayloadsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-payload-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, variant="runtime", *, runtime=None):
        directory = self.root / variant
        directory.mkdir(parents=True, exist_ok=True)
        package = "syncd-vs" if variant == "runtime" else "syncd-vs-dbgsym"
        payload = directory / "payload.tar"
        data = b"sample installed data\n"
        payload.write_bytes(tar_entries([("usr/share/" + package + "/data", data, 0o644)]))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        record = {
            "package": package, "version": "1.0", "architecture": "amd64",
            "source_deb": package + "_1.0_amd64.deb", "source_size": 1,
            "source_sha256": hashlib.sha256(package.encode()).hexdigest(),
            "control_sha256": hashlib.sha256((package + " control").encode()).hexdigest(),
            "payload_sha256": digest, "payload_size": payload.stat().st_size, "payload_members": 1,
        }
        manifest = {
            "schema": 1, "image": "docker-syncd-vs", "variant": variant,
            "architecture": "amd64", "distribution": "trixie", "features": dict(subject.FEATURES),
            "required_packages": [package], "debug_apt_packages": [], "packages": [record],
            "payload": {"path": "payload.tar", "sha256": digest, "size": payload.stat().st_size, "members": 1},
        }
        if variant == "debug":
            manifest["runtime_manifest_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
            manifest["debug_apt_packages"] = ["gdb", "gdbserver", "sshpass", "strace", "vim"]
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return path, payload

    def mutate(self, path, function):
        manifest = json.loads(path.read_bytes())
        function(manifest)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def merged_base(self, *, lib_target="usr/lib"):
        path = self.root / "base.oci"
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for name, value in subject.DIRECTORY_ALIASES.items():
                directory = tarfile.TarInfo(value["target"])
                directory.type, directory.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(directory)
                alias = tarfile.TarInfo(name)
                alias.type, alias.mode = tarfile.SYMTYPE, 0o777
                alias.linkname = lib_target if name == "lib" else value["linkname"]
                archive.addfile(alias)
        layer = data.getvalue()
        config = {"architecture": "amd64", "os": "linux",
                  "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
        write_layout(path, oci_files(json.dumps(config).encode(), [layer]))
        return path

    def test_valid_payload_returns_exact_receipt(self):
        manifest, payload = self.fixture()
        result = subject.validate(manifest, payload, variant="runtime")
        self.assertEqual(result["manifest_sha256"], hashlib.sha256(manifest.read_bytes()).hexdigest())
        self.assertEqual(result["payload_sha256"], hashlib.sha256(payload.read_bytes()).hexdigest())
        self.assertEqual(result["package_count"], 1)

    def test_changed_missing_and_wrong_payload_files_are_rejected(self):
        manifest, payload = self.fixture()
        wrong = self.root / "wrong.tar"
        wrong.write_bytes(payload.read_bytes())
        with self.assertRaisesRegex(ValueError, "declared syncd-vs payload file differs"):
            subject.validate(manifest, wrong, variant="runtime")
        original = payload.read_bytes()
        payload.unlink()
        with self.assertRaisesRegex(ValueError, "changed aggregate package payload"):
            subject.validate(manifest, payload, variant="runtime")
        payload.write_bytes(original + b"changed bytes")
        with self.assertRaisesRegex(ValueError, "changed aggregate package payload"):
            subject.validate(manifest, payload, variant="runtime")

    def test_unsupported_configuration_and_segment_counts_are_rejected(self):
        manifest, payload = self.fixture()
        self.mutate(manifest, lambda value: value.update(architecture="arm64"))
        with self.assertRaisesRegex(ValueError, "unsupported configuration"):
            subject.validate(manifest, payload, variant="runtime")
        self.mutate(manifest, lambda value: value.update(architecture="amd64"))
        self.mutate(manifest, lambda value: value["packages"][0].update(payload_members=2))
        with self.assertRaisesRegex(ValueError, "aggregate package member count differs"):
            subject.validate(manifest, payload, variant="runtime")

    def test_debug_is_tied_to_the_current_runtime_manifest(self):
        runtime, _ = self.fixture()
        debug, payload = self.fixture("debug", runtime=runtime)
        self.assertEqual(subject.validate(debug, payload, variant="debug", runtime_manifest=runtime)["variant"], "debug")
        runtime.write_text(runtime.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "different runtime handoff"):
            subject.validate(debug, payload, variant="debug", runtime_manifest=runtime)

    def test_debug_cannot_change_a_runtime_package(self):
        runtime, _ = self.fixture()
        debug, payload = self.fixture("debug", runtime=runtime)
        self.mutate(debug, lambda value: value["packages"][0].update(package="syncd-vs"))
        self.mutate(debug, lambda value: value.update(required_packages=["syncd-vs"]))
        with self.assertRaisesRegex(ValueError, "changes a runtime package"):
            subject.validate(debug, payload, variant="debug", runtime_manifest=runtime)

    def test_unsafe_payload_member_is_rejected_even_with_matching_hash(self):
        manifest, payload = self.fixture()
        with tarfile.open(payload, "w", format=tarfile.GNU_FORMAT) as archive:
            entry = tarfile.TarInfo("../escape")
            archive.addfile(entry, io.BytesIO())
        self.mutate(manifest, lambda value: value["payload"].update(
            sha256=hashlib.sha256(payload.read_bytes()).hexdigest(), size=payload.stat().st_size))
        with self.assertRaisesRegex(ValueError, "unsafe package payload path"):
            subject.validate(manifest, payload, variant="runtime")

    def test_merged_usr_normalization_preserves_files_and_links(self):
        base = self.merged_base()
        payload = self.root / "native.tar"
        with tarfile.open(payload, "w", format=tarfile.GNU_FORMAT) as archive:
            for name in ("lib", "lib/example"):
                entry = tarfile.TarInfo(name)
                entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(entry)
            entry = tarfile.TarInfo("lib/example/libexample.so.1")
            entry.size, entry.mode, entry.uid, entry.gid = 7, 0o640, 123, 456
            archive.addfile(entry, io.BytesIO(b"payload"))
            entry = tarfile.TarInfo("lib/example/libexample.so")
            entry.type, entry.mode, entry.linkname = tarfile.SYMTYPE, 0o777, "libexample.so.1"
            archive.addfile(entry)
            entry = tarfile.TarInfo("lib/example/hardlink")
            entry.type, entry.mode, entry.linkname = tarfile.LNKTYPE, 0o640, "./lib/example/libexample.so.1"
            archive.addfile(entry)
            for name in ("var/run", "var/run/redis"):
                entry = tarfile.TarInfo(name)
                entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(entry)
            entry = tarfile.TarInfo("var/run/redis/config")
            entry.size, entry.mode = 6, 0o644
            archive.addfile(entry, io.BytesIO(b"config"))
        output = self.root / "normalized.tar"
        result = subject.normalize(payload, base, output)
        self.assertEqual((result["output_members"], result["rewritten_members"], result["skipped_alias_entries"]), (6, 6, 2))
        with tarfile.open(output, "r:") as archive:
            entries = {str(PurePosixPath(member.name)): member for member in archive}
            self.assertNotIn("lib", entries)
            self.assertNotIn("var/run", entries)
            file = entries["usr/lib/example/libexample.so.1"]
            self.assertEqual(archive.extractfile(file).read(), b"payload")
            self.assertEqual((file.mode, file.uid, file.gid), (0o640, 123, 456))
            self.assertEqual(entries["usr/lib/example/libexample.so"].linkname, "libexample.so.1")
            self.assertEqual(entries["usr/lib/example/hardlink"].linkname, "./usr/lib/example/libexample.so.1")
            self.assertEqual(archive.extractfile(entries["run/redis/config"]).read(), b"config")

    def test_merged_usr_normalization_rejects_a_changed_base_alias(self):
        _, payload = self.fixture()
        with self.assertRaisesRegex(ValueError, "unsupported directory alias: lib"):
            subject.normalize(payload, self.merged_base(lib_target="elsewhere"), self.root / "normalized.tar")

    def test_reserved_whiteout_is_rejected(self):
        manifest, payload = self.fixture()
        with tarfile.open(payload, "w", format=tarfile.GNU_FORMAT) as archive:
            archive.addfile(tarfile.TarInfo("usr/bin/.wh.syncd"))
        self.mutate(manifest, lambda value: value["payload"].update(
            sha256=hashlib.sha256(payload.read_bytes()).hexdigest(), size=payload.stat().st_size))
        with self.assertRaisesRegex(ValueError, "reserved OCI whiteout"):
            subject.validate(manifest, payload, variant="runtime")


if __name__ == "__main__":
    unittest.main()
