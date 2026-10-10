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
from unittest import mock
import copy

OWNER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OWNER.parents[1]))
sys.path.insert(0, str(OWNER / "bazel"))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, tar_entries, write_layout
import validate_payloads as subject
import base_debug_symbols


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
        entries = [("usr/share/" + package + "/data", data, 0o644)]
        if variant == "runtime":
            entries.append(("usr/bin/ssh", b"sample FIPS OpenSSH\n", 0o755))
        payload.write_bytes(tar_entries(entries))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        record = {
            "package": package, "version": "1.0", "architecture": "amd64",
            "source_deb": package + "_1.0_amd64.deb", "source_size": 1,
            "source_sha256": hashlib.sha256(package.encode()).hexdigest(),
            "control_sha256": hashlib.sha256((package + " control").encode()).hexdigest(),
            "payload_sha256": digest, "payload_size": payload.stat().st_size, "payload_members": 1,
        }
        records = [record]
        if variant == "runtime":
            fips = dict(record, package="openssh-client", version="1:10.0p1-7+fips",
                        source_deb="openssh-client_10.0p1-7+fips_amd64.deb",
                        control_fields={"Package": "openssh-client", "Version": "1:10.0p1-7+fips", "Architecture": "amd64"})
            records.append(fips)
        manifest = {
            "schema": 1, "image": "docker-syncd-vs", "variant": variant,
            "architecture": "amd64", "distribution": "trixie", "features": dict(subject.FEATURES),
            "required_packages": [r["package"] for r in records], "debug_apt_packages": [], "packages": records,
            "payload": {"path": "payload.tar", "sha256": digest, "size": payload.stat().st_size, "members": len(entries)},
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
        self.assertEqual(result["package_count"], 2)
        self.assertEqual(result["packages"][1]["version"], "1:10.0p1-7+fips")

    def test_runtime_requires_fips_even_if_manifest_omits_the_requirement(self):
        """Stale runtime inputs cannot silently restore the public Debian SSH package."""
        for change in ("missing", "ordinary"):
            with self.subTest(change=change):
                manifest, payload = self.fixture()
                value = json.loads(manifest.read_bytes())
                fips = value["packages"][1]
                value["required_packages"] = ["syncd-vs"]
                if change == "missing":
                    value["packages"].remove(fips)
                else:
                    fips["version"] = "1:10.0p1-7+deb13u4"
                    fips["control_fields"]["Version"] = fips["version"]
                manifest.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "requires the Make FIPS"):
                    subject.validate(manifest, payload, variant="runtime")

    def test_fips_identity_and_integrity_remain_checked(self):
        """A FIPS version label must agree with the package controls and integrity fields."""
        for field in ("Package", "Version", "Architecture"):
            with self.subTest(field=field):
                manifest, payload = self.fixture()
                self.mutate(manifest, lambda value: value["packages"][1]["control_fields"].update({field: "wrong"}))
                with self.assertRaisesRegex(ValueError, "identity differs from its Debian control"):
                    subject.validate(manifest, payload, variant="runtime")
        for kind in ("source", "payload", "control"):
            with self.subTest(kind=kind):
                manifest, payload = self.fixture()
                self.mutate(manifest, lambda value: value["packages"][1].update({kind + "_sha256": "wrong"}))
                with self.assertRaisesRegex(ValueError, "invalid package " + kind + " integrity"):
                    subject.validate(manifest, payload, variant="runtime")

    def test_debug_must_inherit_the_fips_runtime_package(self):
        """Debug inputs cannot carry OpenSSH or reference an old non-FIPS runtime manifest."""
        for version in ("1:10.0p1-7+fips", "1:10.0p1-7+deb13u4"):
            with self.subTest(version=version):
                runtime, _ = self.fixture()
                debug, payload = self.fixture("debug", runtime=runtime)
                self.mutate(debug, lambda value: value["packages"][0].update(package="openssh-client", version=version))
                self.mutate(debug, lambda value: value.update(required_packages=[]))
                with self.assertRaisesRegex(ValueError, "must inherit runtime FIPS"):
                    subject.validate(debug, payload, variant="debug", runtime_manifest=runtime)
        for change in ("missing", "ordinary", "control"):
            with self.subTest(change=change):
                runtime, _ = self.fixture()
                value = json.loads(runtime.read_bytes())
                fips = value["packages"][1]
                if change == "missing":
                    value["packages"].remove(fips)
                elif change == "ordinary":
                    fips["version"] = "1:10.0p1-7+deb13u4"
                    fips["control_fields"]["Version"] = fips["version"]
                else:
                    fips["control_fields"]["Version"] = "1:10.0p1-7+deb13u4"
                runtime.write_text(json.dumps(value))
                debug, payload = self.fixture("debug", runtime=runtime)
                with self.assertRaisesRegex(ValueError, "FIPS openssh-client"):
                    subject.validate(debug, payload, variant="debug", runtime_manifest=runtime)

    def test_make_handoffs_reject_libraries_and_symbols_owned_by_bazel(self):
        """Stale runtime or debug Make inputs cannot replace the shared source outputs."""
        for variant in ("runtime", "debug"):
            for package in sorted(subject.SOURCE_PACKAGE_NAMES):
                with self.subTest(variant=variant, package=package):
                    runtime, runtime_payload = self.fixture()
                    if variant == "runtime":
                        manifest, payload = runtime, runtime_payload
                    else:
                        manifest, payload = self.fixture("debug", runtime=runtime)
                    self.mutate(manifest, lambda value: value["packages"][0].update(package=package))
                    with self.assertRaisesRegex(ValueError, "now built from source|inherited base-symbol"):
                        subject.validate(manifest, payload, variant=variant,
                                         runtime_manifest=runtime if variant == "debug" else None)

    def test_debug_rejects_a_stale_make_runtime_even_when_its_hash_matches(self):
        """Hash binding alone must not admit the removed Make libraries through inheritance."""
        for package in sorted(subject.SOURCE_PACKAGE_NAMES):
            with self.subTest(package=package):
                runtime, _ = self.fixture()
                self.mutate(runtime, lambda value: value["packages"][0].update(package=package))
                debug, payload = self.fixture("debug", runtime=runtime)
                with self.assertRaisesRegex(ValueError, "now built from source|inherited base-symbol"):
                    subject.validate(debug, payload, variant="debug", runtime_manifest=runtime)

    def filtered_base_symbols(self):
        runtime, _ = self.fixture()
        manifest, payload = self.fixture("debug", runtime=runtime)
        value = json.loads(manifest.read_bytes())
        original = dict(value["packages"][0], package="libswsscommon-dbgsym",
                        source_deb="libswsscommon-dbgsym_1.0_amd64.deb")
        contract = copy.deepcopy(base_debug_symbols.read_contract())
        contract["package"] = {key: item for key, item in original.items() if not key.startswith("payload_")}
        contract["original_payload"] = {key: original["payload_" + key] for key in ("sha256", "size", "members")}
        members = [(name, ("fixture " + name).encode(), 0o644) for name in contract["files"]]
        for name, contents, mode in members:
            contract["files"][name].update(sha256=hashlib.sha256(contents).hexdigest(), size=len(contents),
                                           mode=mode, uid=0, gid=0)
        contract_path = self.root / "base-symbol-contract.json"
        contract_path.write_text(json.dumps(contract))
        payload.write_bytes(tar_entries(members))
        record = dict(original, **{"original_payload_" + key: item for key, item in contract["original_payload"].items()},
                      payload_sha256=base_debug_symbols.sha(payload), payload_size=payload.stat().st_size,
                      payload_members=2, base_debug_symbols=base_debug_symbols.descriptor(contract_path))
        value.update(packages=[record], required_packages=[record["package"]],
                     payload={"path": "payload.tar", "sha256": record["payload_sha256"],
                              "size": record["payload_size"], "members": 2})
        manifest.write_text(json.dumps(value))
        return runtime, manifest, payload, contract_path, members

    def test_debug_allows_only_the_checked_inherited_base_symbols(self):
        """Keep the base library's two companions without reintroducing replaced Make symbols."""
        runtime, manifest, payload, contract, _ = self.filtered_base_symbols()
        with mock.patch.object(base_debug_symbols, "CONTRACT", contract):
            result = subject.validate(manifest, payload, variant="debug", runtime_manifest=runtime)
            self.assertEqual(result["package_count"], 1)
            record = json.loads(manifest.read_bytes())["packages"][0]
            with self.assertRaisesRegex(ValueError, "now built from source"):
                subject.reject_source_packages([record])
            changed = dict(record, source_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "original package changed"):
                subject.reject_source_packages([changed], allow_base_symbols=True)

    def test_filtered_base_symbols_check_bytes_beyond_the_aggregate_hash(self):
        """Updating an outer tar hash cannot disguise changed or extra inherited symbols."""
        runtime, manifest, payload, contract, members = self.filtered_base_symbols()
        payload.write_bytes(tar_entries([(members[0][0], b"changed", 0o644), members[1]]))
        self.mutate(manifest, lambda value: value["payload"].update(
            sha256=base_debug_symbols.sha(payload), size=payload.stat().st_size))
        with mock.patch.object(base_debug_symbols, "CONTRACT", contract):
            with self.assertRaisesRegex(ValueError, "bytes or metadata changed"):
                subject.validate(manifest, payload, variant="debug", runtime_manifest=runtime)

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
