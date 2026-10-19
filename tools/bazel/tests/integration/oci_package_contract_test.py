#!/usr/bin/env python3
"""Exercise source ownership and mixed-input boundaries with tar/ELF-header fixtures.

These cases never compile a library or create a DEB. Installed ABI and real
runtime/debug correspondence belong to the separate native image validation.
"""

import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
OWNER = ROOT / "platform/vs/docker-syncd-vs"
sys.path.insert(0, str(ROOT))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, write_layout
from tools.bazel.oci import package_contract as subject
from tools.bazel.tests.oci_contract_fixture import identity, owner_policies, package_policy, source_contract

OWNER_POLICIES = {}


def elf(machine=62):
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", data, 16, 3, machine)
    return bytes(data)


def archive(path, entries):
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as output:
        for name, contents, mode, kind, owner in entries:
            member = tarfile.TarInfo(name)
            member.mode, member.uid, member.gid = mode, owner, owner
            if kind == "directory":
                member.type = tarfile.DIRTYPE
            elif kind == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, contents
            else:
                member.size = len(contents)
            output.addfile(member, io.BytesIO(contents) if kind == "file" else None)


class SourcePackageFixture:
    """Generate archives from a declared contract; package count and names are inputs."""

    contract_document = None
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oci-source-packages-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contract = self.root / "source_packages.json"
        if self.contract_document is not None:
            self.value = copy.deepcopy(self.contract_document)
        else:
            self.value = json.loads((OWNER / "config/source_packages.json").read_bytes())
            # Owner declarations contain provenance and paths only. Real package
            # controls are supplied by producer DEBs in image validation. These
            # small TAR tests intentionally use unrelated synthetic controls.
            for record in self.value["packages"]:
                self.assertNotIn("control_fields", record)
                self.assertNotIn("version", record)
                if record.get("kind") == "runtime-layer":
                    record.update(version=record["source"]["version"], architecture=self.value["architecture"])
                    continue
                record.update(version="9.8.7-test", architecture=self.value["architecture"],
                    control_fields={name: "" for name in subject.CONTROL_FIELDS})
                record["control_fields"].update(Package=record["package"], Version=record["version"],
                                                Architecture=record["architecture"], Depends="libc6 (>= 2.36)")
        self.policy = OWNER_POLICIES.get("syncd", package_policy(image=self.value["image"]))
        for record in self.value["packages"]:
            for item in record.get("required_inherited_files", {}).values():
                if item["kind"] == "file":
                    item.update(sha256=hashlib.sha256(elf()).hexdigest(), size=len(elf()))
        self.contract.write_bytes(subject.json_bytes(self.value))
        self.packages, self.debug_packages, self.modules = {}, {}, {}
        self.entries = {}
        debug_entries = []
        for index, record in enumerate(self.value["packages"]):
            name = record["package"]
            entries = [("./", b"", 0o755, "directory", 123)]
            installed_paths = {path: "file" for path in record.get("path_modes", {})} | record["required_paths"]
            for path, kind in installed_paths.items():
                source_path = "var/run" + path[len("run"):] if path.startswith("run/") else path
                contents = elf({"amd64": 62, "arm64": 183}[self.value["architecture"]]) if kind == "elf" else b"configuration\n"
                if kind == "symlink":
                    candidates = [name for name, item_kind in record["required_paths"].items()
                                  if item_kind == "elf" and Path(name).parent == Path(path).parent
                                  and Path(name).name.startswith(Path(path).name + ".")]
                    contents = Path(candidates[0]).name
                entries.append((source_path, contents, 0 if kind == "symlink" else 0o555 if kind == "elf" else
                                0o755 if path.endswith(".lua") else 0o644,
                                "symlink" if kind == "symlink" else "file", 123))
            self.entries[name] = entries
            path = self.root / (name + ".tar")
            archive(path, entries)
            self.packages[name] = path
            path = self.root / (name + ".debug.tar")
            symbols = ("usr/lib/debug/.build-id/ab/" + str(index) + "cdef.debug", elf(subject.elf_machine(self.value["architecture"])), 0o644, "file", 0)
            archive(path, [symbols])
            debug_entries.append(symbols)
            self.debug_packages[name] = path
            source = record["source"]
            path = self.root / (source["module"] + ".MODULE.bazel")
            path.write_text('module(name = "' + source["module"] + '", version = "' + source.get("module_version", source["version"]) + '")\n')
            self.modules[source["module"]] = path
        base_layer = self.root / "base.tar"
        entries = []
        for name, entry in subject.normalization.DIRECTORY_ALIASES.items():
            entries += [(entry["target"], b"", 0o755, "directory", 0),
                        (name, entry["linkname"], 0o777, "symlink", 0)]
        entries += [("usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0", elf(), 0o644, "file", 0),
                    ("usr/lib/x86_64-linux-gnu/libswsscommon.so.0", "libswsscommon.so.0.0.0", 0o777, "symlink", 0)]
        for record in self.value["packages"]:
            for path, item in record.get("required_inherited_files", {}).items():
                entries.append((path, item.get("linkname", elf()), item["mode"], item["kind"], 0))
        archive(base_layer, entries)
        layer = base_layer.read_bytes()
        config = {"architecture": self.value["architecture"], "os": "linux", "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
        self.base = self.root / "base.oci"
        write_layout(self.base, oci_files(json.dumps(config).encode(), [layer]))
        self.runtime_tar, self.debug_tar = self.root / "runtime.tar", self.root / "debug.tar"
        self.receipt = self.root / "receipt.json"
        archive(self.debug_tar, debug_entries)

    def build(self):
        result = subject.assemble(self.contract, self.packages, self.modules, self.base, self.runtime_tar,
                                  package_targets={record["package"]: record["source"]["target"]
                                                   for record in self.value["packages"]})
        self.receipt.write_bytes(subject.json_bytes(result))
        return result


class RuntimeLayerContractTest(SourcePackageFixture, unittest.TestCase):
    """Ordinary SONiC payloads remain TARs without invented Debian identities."""

    contract_document = source_contract()

    def setUp(self):
        super().setUp()
        for record in self.value["packages"]:
            record.pop("control_fields")
            record["kind"] = "runtime-layer"
            record["version"] = record["source"]["version"]
        self.contract.write_bytes(subject.json_bytes(self.value))

    def test_runtime_layer_receipt_preserves_source_without_debian_controls(self):
        result = self.build()
        self.assertTrue(all(record["kind"] == "runtime-layer" and "control_fields" not in record
                            for record in result["packages"]))
        subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract)

    def test_runtime_layer_cannot_claim_debian_dependency_controls(self):
        self.value["packages"][0]["control_fields"] = {}
        self.contract.write_bytes(subject.json_bytes(self.value))
        with self.assertRaisesRegex(ValueError, "must not claim Debian package controls"):
            self.build()


class SourceDebPayloadTest(SourcePackageFixture, unittest.TestCase):
    """Use actual Debian-selected paths and permissions rather than the broad TAR."""

    contract_document = source_contract()

    def setUp(self):
        super().setUp()
        record = self.value["packages"][0]
        name = record["package"]
        record["source"]["target"] = "@example_owner//debian:package"
        entries = [(path, contents, 0o777 if kind == "symlink" else mode, kind, 0)
                   for path, contents, mode, kind, owner in self.entries[name]]
        entries.append(("usr/share/example.lua", b"#!/usr/bin/lua\n", 0o755, "file", 0))
        archive(self.packages[name], entries)
        record["deb"] = {"target": record["source"]["target"], "sha256": "b" * 64,
                         "data_sha256": subject.sha(self.packages[name])}
        self.contract.write_bytes(subject.json_bytes(self.value))

    def test_keeps_selected_permissions_and_excludes_developer_link(self):
        result = self.build()
        record = result["packages"][0]
        self.assertEqual(record["files"]["usr/share/example.lua"]["mode"], 0o755)
        self.assertNotIn("usr/lib/libexample.so", record["files"])
        self.assertEqual(record["deb"], self.value["packages"][0]["deb"])
        self.assertEqual(subject.validate_receipt(self.receipt, self.runtime_tar,
                         contract_path=self.contract), result)

    def test_deb_symlink_mode_uses_linux_permissions_without_changing_target(self):
        name = self.value["packages"][0]["package"]
        entries = [(path, contents, 0 if kind == "symlink" else mode, kind, 0)
                   for path, contents, mode, kind, owner in self.entries[name]]
        archive(self.packages[name], entries)
        self.value["packages"][0]["deb"]["data_sha256"] = subject.sha(self.packages[name])
        self.contract.write_bytes(subject.json_bytes(self.value))
        files = self.build()["packages"][0]["files"]
        self.assertEqual(files["usr/lib/libexample.so.2"], {
            "kind": "symlink", "mode": 0o777, "uid": 0, "gid": 0,
            "linkname": "libexample.so.2.4.0"})

    def test_rejects_data_from_another_deb(self):
        self.value["packages"][0]["deb"]["data_sha256"] = "c" * 64
        self.contract.write_bytes(subject.json_bytes(self.value))
        with self.assertRaisesRegex(ValueError, "payload differs from its actual DEB"):
            self.build()

    def test_rejects_changed_deb_identity_in_receipt(self):
        result = self.build()
        result["packages"][0]["deb"]["sha256"] = "c" * 64
        self.receipt.write_bytes(subject.json_bytes(result))
        with self.assertRaisesRegex(ValueError, "DEB differs from the reviewed contract"):
            subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract)

    def test_rejects_manual_mode_override(self):
        self.value["packages"][0]["path_modes"] = {"usr/share/example.lua": 0o644}
        self.contract.write_bytes(subject.json_bytes(self.value))
        with self.assertRaisesRegex(ValueError, "metadata must not be overridden"):
            self.build()


class SharedLibraryPolicyTest(SourcePackageFixture, unittest.TestCase):
    """Preserve the actual shared-library migration policy as explicit input data."""

    def test_foreign_release_is_bound_to_the_resolved_module_patch_version(self):
        record = copy.deepcopy(self.value["packages"][0])
        record["source"] = {
            "module": "libnl3", "version": "3.7.0.sonic.3", "release": "3.7.0",
            "target": "@libnl3//:libnl-3-dev_pkg",
        }
        if record.get("kind") == "runtime-layer":
            record["version"] = record["source"]["version"]
        subject.package_identity(record, architecture="amd64")
        module = self.root / "foreign.MODULE.bazel"
        module.write_text('module(name = "libnl3", version = "3.7.0.sonic.2")\n')
        with self.assertRaisesRegex(ValueError, "resolved source module differs"):
            subject.verify_modules({"packages": [record]}, {"libnl3": module})
        record["source"]["release"] = "3.8.0"
        with self.assertRaisesRegex(ValueError, "public owner target"):
            subject.package_identity(record, architecture="amd64")
        record["source"].update(release="3.7.0", commit="a" * 40)
        with self.assertRaisesRegex(ValueError, "invalid source package provenance"):
            subject.package_identity(record, architecture="amd64")

    def test_reuses_owner_payload_bytes_and_records_new_source_provenance(self):
        """A source receipt identifies owner modules/tars, never the retired Make DEBs."""
        result = self.build()
        self.assertEqual(subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract), result)
        for record in result["packages"]:
            self.assertEqual(record["input_tar_sha256"], subject.sha(self.packages[record["package"]]))
            self.assertNotIn("debug", record)
            self.assertNotIn("source_deb", record)
            self.assertNotIn("source_sha256", record)
            self.assertNotIn("control_sha256", record)
            declaration = next(item for item in self.value["packages"] if item["package"] == record["package"])
            for path, item in record["files"].items():
                owner = declaration.get("path_owners", {}).get(path, {"uid": 0, "gid": 0})
                self.assertEqual({key: item[key] for key in owner}, owner)
        common = result["packages"][0]["files"]
        self.assertIn("run/redis/sonic-db/database_config.json", common)
        self.assertNotIn("var/run/redis/sonic-db/database_config.json", common)
        self.assertEqual(common["usr/bin/swssloglevel"]["mode"], 0o555)
        self.assertEqual(common["usr/bin/swssloglevel"]["sha256"], hashlib.sha256(elf()).hexdigest())
        self.assertEqual(common["usr/lib/x86_64-linux-gnu/libswsscommon.so.0"]["mode"], 0o777)
        self.assertEqual(common["run/redis/sonic-db/database_config.json"]["mode"], 0o644)
        self.assertTrue(all(item["mode"] == 0o644 for name, item in common.items() if name.endswith(".lua")))

    def test_normalized_symlink_mode_does_not_allow_changing_an_inherited_elf_link(self):
        """Normalize real owner mode 0000, but still reject a different base library target."""
        entries = []
        for path, contents, mode, kind, owner in self.entries["libswsscommon"]:
            if path == "usr/lib/x86_64-linux-gnu/libswsscommon.so.0":
                contents = "libswsscommon.unreviewed.so"
            entries.append((path, contents, mode, kind, owner))
        archive(self.packages["libswsscommon"], entries)
        with self.assertRaisesRegex(ValueError, "changes a base ELF link"):
            self.build()

    def test_common_tar_supplies_its_own_rust_binding(self):
        """The CLI library and its source symbols no longer depend on an imported base DEB."""
        result = self.build()
        path = "usr/lib/x86_64-linux-gnu/libsonicdbcli.so.0.0.0"
        common = next(item for item in result["packages"] if item["package"] == "libswsscommon")
        self.assertIn(path, common["files"])
        self.assertEqual(result["inherited_files"], {})
        entries = [entry for entry in self.entries["libswsscommon"] if entry[0] != path]
        archive(self.packages["libswsscommon"], entries)
        with self.assertRaisesRegex(ValueError, "required installed path"):
            self.build()

    def test_unregistered_source_declares_module_version_without_losing_commit_pin(self):
        record = copy.deepcopy(self.value["packages"][0])
        source = record["source"]
        source["module_version"] = "0.0.4"
        subject.package_identity(record, architecture="amd64")
        module = self.root / "unregistered.MODULE.bazel"
        module.write_text('module(name="' + source["module"] + '", version="0.0.4")\n')
        subject.verify_modules({"packages": [record]}, {source["module"]: module})
        module.write_text('module(name="' + source["module"] + '", version="0.0.5")\n')
        with self.assertRaisesRegex(ValueError, "resolved source module differs"):
            subject.verify_modules({"packages": [record]}, {source["module"]: module})
        module.write_text('module(name="' + source["module"] + '")\n')
        with self.assertRaisesRegex(ValueError, "resolved source module differs"):
            subject.verify_modules({"packages": [record]}, {source["module"]: module})
        source["version"] = "0.0.4"
        with self.assertRaisesRegex(ValueError, "public owner target"):
            subject.package_identity(record, architecture="amd64")

    def test_resolved_module_drift_fails_before_publishing_archives(self):
        """A newer transitive module cannot be described as the reviewed old source commit."""
        path = self.modules["sonic-swss-common"]
        path.write_text('module(name="sonic-swss-common", version="0.0.0-new")\n')
        with self.assertRaisesRegex(ValueError, "resolved source module differs"):
            self.build()
        self.assertFalse(self.runtime_tar.exists())

    def test_missing_owner_payload_and_required_library_are_rejected(self):
        """A tar binding and its real install inventory must both supply every shared library."""
        saved = self.packages.pop("libsairedis")
        with self.assertRaisesRegex(ValueError, "tar inputs"):
            self.build()
        self.packages["libsairedis"] = saved
        archive(saved, [("./", b"", 0o755, "directory", 0)])
        with self.assertRaisesRegex(ValueError, "required installed path"):
            self.build()
        self.assertFalse(self.runtime_tar.exists())

    def test_foreign_elf_and_unsafe_paths_fail_without_replacing_good_outputs(self):
        """An architecture or archive-path regression leaves the previous checked payload intact."""
        self.build()
        before = self.runtime_tar.read_bytes()
        original = self.entries["libsairedis"]
        variants = [
            [(path, elf(183) if kind == "file" else data, mode, kind, owner)
             for path, data, mode, kind, owner in original],
            original + [("../escape", b"data", 0o644, "file", 0)],
            original + [("usr/.wh.hidden", b"data", 0o644, "file", 0)],
        ]
        for entries in variants:
            with self.subTest(extra=entries[-1][0]):
                archive(self.packages["libsairedis"], entries)
                with self.assertRaises(ValueError):
                    self.build()
                self.assertEqual(self.runtime_tar.read_bytes(), before)

    def test_source_packages_cannot_overlap_or_cross_an_unexpected_base_link(self):
        """Package ownership stays unambiguous and source payloads preserve base directory links."""
        entries = self.entries["libsairedis"] + [("usr/bin/swssloglevel", elf(), 0o755, "file", 0)]
        archive(self.packages["libsairedis"], entries)
        with self.assertRaisesRegex(ValueError, "source packages overlap"):
            self.build()
        archive(self.packages["libsairedis"], self.entries["libsairedis"])
        entries = self.entries["libswsscommon"] + [("usr/share", "/elsewhere", 0o777, "symlink", 0),
                                                     ("usr/share/swss/test.lua", b"lua", 0o644, "file", 0)]
        archive(self.packages["libswsscommon"], entries)
        with self.assertRaisesRegex(ValueError, "crosses a non-directory"):
            self.build()

    def test_debug_payload_must_contain_source_symbols_only(self):
        """The shared symbol layer cannot replace runtime files in the debug image."""
        archive(self.debug_tar, [("usr/bin/syncd", elf(), 0o755, "file", 0)])
        with self.assertRaisesRegex(ValueError, "symbols contain an unexpected file"):
            subject.debug_inventory(self.debug_tar, architecture=self.value["architecture"])
        archive(self.debug_tar, [])
        with self.assertRaisesRegex(ValueError, "no debug symbols"):
            subject.debug_inventory(self.debug_tar, architecture=self.value["architecture"])

    def test_runtime_assembly_does_not_require_debug_archives(self):
        """Building runtime metadata and libraries must not request the debug image's symbols."""
        self.debug_tar.unlink()
        for path in self.debug_packages.values():
            path.unlink()
        result = self.build()
        self.assertEqual(result["schema"], 2)
        self.assertNotIn("debug_payload", result)
        self.assertTrue(all("debug" not in package for package in result["packages"]))
        self.assertEqual(subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract), result)

    def test_obsolete_combined_receipts_are_rejected(self):
        """Old receipts coupling runtime and debug inputs must be regenerated, never misinterpreted."""
        result = self.build()
        result["schema"] = 1
        self.receipt.write_bytes(subject.json_bytes(result))
        with self.assertRaisesRegex(ValueError, "runtime-only schema 2"):
            subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract)

    def test_receipt_binds_archive_hashes_and_per_package_file_ownership(self):
        """Final validators reject changed archives and owner inventories even with valid JSON."""
        self.build()
        before = self.runtime_tar.read_bytes()
        self.runtime_tar.write_bytes(before + b"changed")
        with self.assertRaisesRegex(ValueError, "archive hash differs"):
            subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract)
        self.runtime_tar.write_bytes(before)
        value = json.loads(self.receipt.read_bytes())
        value["packages"][0]["files"]["usr/bin/swssloglevel"]["sha256"] = "a" * 64
        self.receipt.write_bytes(subject.json_bytes(value))
        with self.assertRaisesRegex(ValueError, "archive inventory differs"):
            subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract)

    def test_contract_rejects_invalid_owner_label_and_incomplete_dependency_metadata(self):
        """Source provenance and dependency checks cannot be weakened by changing one contract field."""
        variants = []
        value = copy.deepcopy(self.value)
        value["packages"][0]["source"]["target"] = "not-a-bazel-label"
        variants.append(value)
        value = copy.deepcopy(self.value)
        deb = next(record for record in value["packages"] if "control_fields" in record)
        del deb["control_fields"]["Depends"]
        variants.append(value)
        for value in variants:
            with self.subTest(value=value["packages"][0]):
                self.contract.write_bytes(subject.json_bytes(value))
                with self.assertRaises(ValueError):
                    subject.read_contract(self.contract)

    def test_receipt_cannot_claim_contract_hash_with_different_source_or_dependencies(self):
        """The contract digest also binds its actual source and dependency records, not just JSON syntax."""
        value = self.build()
        subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract)
        for field in ("source", "control_fields"):
            with self.subTest(field=field):
                altered = copy.deepcopy(value)
                if field == "source":
                    altered["packages"][0][field].update(commit="a" * 40, version="0.0.1-" + "a" * 40)
                    if "module_version" in altered["packages"][0][field]:
                        altered["packages"][0][field]["module_version"] = "0.0.1"
                    if altered["packages"][0].get("kind") == "runtime-layer":
                        altered["packages"][0]["version"] = altered["packages"][0][field]["version"]
                else:
                    deb = next(record for record in altered["packages"] if "control_fields" in record)
                    deb[field]["Depends"] = "libc6"
                self.receipt.write_bytes(subject.json_bytes(altered))
                with self.assertRaisesRegex(ValueError, "differs from the reviewed contract"):
                    subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract)

    def test_retained_manifest_keeps_make_and_source_provenance_separate(self):
        """APT sees source dependencies while the original Make input and debug binding remain exact."""
        result = self.build()
        value = {**identity(self.value), "schema": 1, "variant": "debug", "features": dict(self.policy["features"]),
                 "packages": [{"package": "libsai", "source_deb": "libsai_1.0.0_amd64.deb"}],
                 "runtime_manifest_sha256": "1" * 64, "required_packages": ["libsai"],
                 "debug_apt_packages": sorted(self.policy["debug_tools"])}
        path = self.root / "make.json"
        path.write_bytes(subject.json_bytes(value))
        combined = subject.retained_manifest(path, result, subject.sha(self.receipt), variant="debug", contract=self.contract, policy=self.policy)
        self.assertEqual(combined["packages"], value["packages"])
        self.assertEqual(combined["runtime_manifest_sha256"], value["runtime_manifest_sha256"])
        self.assertEqual(combined["make_manifest_sha256"], subject.sha(path))
        self.assertEqual(combined["source_receipt_sha256"], subject.sha(self.receipt))
        self.assertEqual(combined["source_packages"], result["packages"])
        for name in ("libswsscommon", "libsairedis-dbgsym"):
            value["packages"] = [{"package": name}]
            path.write_bytes(subject.json_bytes(value))
            with self.assertRaisesRegex(ValueError, "packages now built from source"):
                subject.retained_manifest(path, result, subject.sha(self.receipt), variant="debug", contract=self.contract, policy=self.policy)

    def test_imports_cannot_bypass_symbol_matching_with_common_debug_packages(self):
        """A source-owned package cannot import stale symbols from its retired DEB."""
        result = self.build()
        path = self.root / "make-base-symbols.json"
        for variant in ("runtime", "debug"):
            value = {**identity(self.value), "schema": 1, "variant": variant, "features": dict(self.policy["features"]),
                     "packages": [{"package": "libswsscommon-dbgsym"}]}
            path.write_bytes(subject.json_bytes(value))
            with self.assertRaisesRegex(ValueError, "packages now built from source"):
                subject.retained_manifest(path, result, subject.sha(self.receipt), variant=variant, contract=self.contract, policy=self.policy)


class UnrelatedSourceContractTest(SourcePackageFixture, unittest.TestCase):
    """Prove source handling is independent of Syncd and the three SONiC libraries."""

    contract_document = source_contract()

    def test_installation_owner_and_setgid_change_metadata_only(self):
        """Image-local group assignment preserves the owner's exact executable bytes."""
        name = "usr/lib/libexample.so.2.4.0"
        record = self.value["packages"][0]
        record.update(path_modes={name: 0o2755}, path_owners={name: {"uid": 0, "gid": 104}})
        self.contract.write_bytes(subject.json_bytes(self.value))
        result = self.build()
        subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract)
        entry = result["packages"][0]["files"][name]
        self.assertEqual((entry["uid"], entry["gid"], entry["mode"]), (0, 104, 0o2755))
        with tarfile.open(self.packages["libexample"]) as original, tarfile.open(self.runtime_tar) as installed:
            self.assertEqual(original.extractfile(name).read(), installed.extractfile("./" + name).read())
        result["packages"][0]["files"][name]["gid"] = 105
        self.receipt.write_bytes(subject.json_bytes(result))
        with self.assertRaisesRegex(ValueError, "reviewed file owner"):
            subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract)

    def test_owner_override_cannot_target_a_link_directory_or_missing_file(self):
        for path in ("usr/lib/libexample.so.2", ".", "usr/bin/missing"):
            with self.subTest(path=path):
                self.value["packages"][0]["path_owners"] = {path: {"uid": 0, "gid": 104}}
                self.contract.write_bytes(subject.json_bytes(self.value))
                with self.assertRaisesRegex(ValueError, "regular file|reviewed file owner"):
                    self.build()

    def test_undeclared_archive_owner_is_rejected_even_with_matching_receipt_hash(self):
        result = self.build()
        name = "usr/lib/libexample.so.2.4.0"
        entries = [(path, data, mode, kind, 104 if path == name else 0)
                   for path, data, mode, kind, _ in self.entries["libexample"]]
        archive(self.runtime_tar, entries)
        result["payload"].update(sha256=subject.sha(self.runtime_tar), size=self.runtime_tar.stat().st_size)
        result["packages"][0]["files"][name].update(uid=104, gid=104)
        self.receipt.write_bytes(subject.json_bytes(result))
        with self.assertRaisesRegex(ValueError, "reviewed ownership"):
            subject.validate_receipt(self.receipt, self.runtime_tar, contract_path=self.contract)

    def test_invalid_ownership_and_permission_declarations_are_rejected(self):
        name = "usr/lib/libexample.so.2.4.0"
        for owners in ({name: {"uid": True, "gid": 1}}, {name: {"uid": 0, "gid": -1}},
                       {name: {"uid": 0, "gid": 2**32 - 1}}, {name: {"gid": 104}},
                       {"../escape": {"uid": 0, "gid": 104}}):
            with self.subTest(owners=owners):
                self.value["packages"][0]["path_owners"] = owners
                self.contract.write_bytes(subject.json_bytes(self.value))
                with self.assertRaises(ValueError):
                    self.build()
        self.value["packages"][0].pop("path_owners")
        self.value["packages"][0]["path_modes"] = {name: 0o10000}
        self.contract.write_bytes(subject.json_bytes(self.value))
        with self.assertRaisesRegex(ValueError, "file modes"):
            self.build()

    def test_one_package_with_an_unrelated_owner_is_sufficient(self):
        """Accept the complete one-package declaration and bind its own target, module and version."""
        result = self.build()
        self.assertEqual([item["package"] for item in result["packages"]], ["libexample"])
        self.assertEqual(result["image"], self.value["image"])
        self.assertEqual(result["packages"][0]["version"], "2.4.0")
        self.assertEqual(subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract), result)

    def test_declared_bazel_target_must_match_the_tar_binding(self):
        """A valid-looking declaration cannot attribute an archive to a different Bazel target."""
        with self.assertRaises(ValueError):
            subject.assemble(self.contract, self.packages, self.modules, self.base, self.runtime_tar,
                             package_targets={"libexample": "@unrelated//:other"})
        self.assertFalse(self.runtime_tar.exists())

    def test_production_receipt_validation_requires_the_reviewed_contract(self):
        """Receipt JSON alone cannot prove that its claimed source identities were approved."""
        self.build()
        with self.assertRaises(ValueError):
            subject.validate_receipt(self.receipt, self.runtime_tar)

    def test_owner_policies_are_explicit_and_do_not_leak_between_containers(self):
        """One engine accepts both owner identities without changing process-global feature policy."""
        if not OWNER_POLICIES:
            self.skipTest("BUILD-generated owner policies are supplied by the Bazel target")
        for name in ("swss", "syncd", "swss"):
            with self.subTest(owner=name):
                policy = subject.validate_policy(copy.deepcopy(OWNER_POLICIES[name]))
                self.assertEqual(policy["image"], {"swss": "docker-orchagent", "syncd": "docker-syncd-vs"}[name])
                self.assertEqual(subject.identity(policy), identity(OWNER_POLICIES[name]))



class ArmSourceContractTest(UnrelatedSourceContractTest):
    """The same source assembly supports an unrelated ARM64 declaration."""

    contract_document = source_contract(image="docker-router", architecture="arm64")


class SwssInstallInventoryTest(unittest.TestCase):
    """Compare synthetic payloads with source-owned program and Lua declarations."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        for name in ("dist", "debian", "cfgmgr"):
            (self.source / name).mkdir()
        programs = ["//cfgmgr:program" + str(number) for number in range(29)]
        (self.source / "dist/BUILD.bazel").write_text(
            "CPP_BINARIES = " + repr(programs) + "\n"
            "LUA_FILES = ['//cfgmgr:vs.lua']\n"
            "LUA_INSTALL_ALIASES = {'//cfgmgr:vs.lua': '//cfgmgr:mellanox.lua'}\n"
            "# Nonliteral build expressions must never be evaluated by this reader.\n"
            "OTHER = arbitrary_build_expression()\n")
        (self.source / "debian/swss.install").write_text("target/release/countersyncd usr/bin\n")
        (self.source / "cfgmgr/vs.lua").write_text("VS source is not installed\n")
        (self.source / "cfgmgr/mellanox.lua").write_text("Installed implementation\n")
        self.install_policy = {
            "declaration_file": "dist/BUILD.bazel", "install_file": "debian/swss.install",
            "groups": [
                {"variable": "CPP_BINARIES", "destination": "usr/bin", "kind": "elf", "mode": 0o755},
                {"variable": "LUA_FILES", "destination": "usr/share/swss", "kind": "file", "mode": 0o644,
                 "aliases_variable": "LUA_INSTALL_ALIASES"},
            ],
            "generated_programs": ["target/release/countersyncd"],
            "executable_directories": ["usr/bin"], "expected_program_count": 30, "architecture": "amd64",
        }
        self.files = {"usr/bin/program" + str(number):
                      {"kind": "file", "mode": 0o755, "elf_machine": 62}
                      for number in range(29)}
        self.files["usr/bin/countersyncd"] = {"kind": "file", "mode": 0o755, "elf_machine": 62}
        self.files["usr/share/swss/vs.lua"] = {
            "kind": "file", "mode": 0o644,
            "sha256": subject.sha(self.source / "cfgmgr/mellanox.lua"),
        }

    def test_current_source_declarations_and_debian_rust_binary(self):
        """Combine C++ and Debian-declared Rust programs without a generated source map."""
        self.assertEqual(len(subject.validate_install_inventory(self.source, self.files, self.install_policy)), 30)
        self.assertFalse((self.source / "bazel/production_sources.bzl").exists())

    def test_missing_program_is_rejected(self):
        """Detect a source-declared executable missing from the archive payload."""
        del self.files["usr/bin/program0"]
        with self.assertRaisesRegex(ValueError, "differs from the source install contract"):
            subject.validate_install_inventory(self.source, self.files, self.install_policy)

    def test_unexpected_payload_is_rejected(self):
        """Catch an extra executable not declared by the SWSS source inventory."""
        self.files["usr/bin/extra"] = {"kind": "file", "mode": 0o755, "elf_machine": 62}
        with self.assertRaisesRegex(ValueError, "differs from the source install contract"):
            subject.validate_install_inventory(self.source, self.files, self.install_policy)

    def test_alias_must_install_the_selected_implementation(self):
        """Check aliased Lua content against the selected implementation's bytes."""
        self.files["usr/share/swss/vs.lua"]["sha256"] = subject.sha(self.source / "cfgmgr/vs.lua")
        with self.assertRaisesRegex(ValueError, "installed data differs from source"):
            subject.validate_install_inventory(self.source, self.files, self.install_policy)

    def test_missing_alias_declaration_is_rejected(self):
        """Reject source metadata that omits the required Lua alias declaration."""
        build = self.source / "dist/BUILD.bazel"
        build.write_text(build.read_text().replace(
            "LUA_INSTALL_ALIASES = {'//cfgmgr:vs.lua': '//cfgmgr:mellanox.lua'}\n", ""))
        with self.assertRaisesRegex(ValueError, "missing.*install declarations"):
            subject.validate_install_inventory(self.source, self.files, self.install_policy)


class UnrelatedInstallInventoryTest(unittest.TestCase):
    """A second install schema proves that paths, variables and program count are policy."""

    def test_two_program_inventory_with_custom_destination_and_alias(self):
        """Validate a non-SWSS install layout using the same inventory reader and byte checks."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build").mkdir()
            (root / "source").mkdir()
            (root / "build/install.bzl").write_text(
                "COMMANDS = ['//source:daemon']\n"
                "ASSETS = ['//source:default.conf']\n"
                "ALIASES = {'//source:default.conf': '//source:chosen.conf'}\n")
            (root / "source/chosen.conf").write_text("selected configuration\n")
            (root / "build/install.list").write_text("output/helper opt/example/bin\n")
            policy = {
                "declaration_file": "build/install.bzl", "install_file": "build/install.list",
                "groups": [
                    {"variable": "COMMANDS", "destination": "opt/example/bin", "kind": "elf", "mode": 0o750},
                    {"variable": "ASSETS", "destination": "opt/example/etc", "kind": "file", "mode": 0o640,
                     "aliases_variable": "ALIASES"},
                ],
                "generated_programs": ["output/helper"], "executable_directories": ["opt/example/bin"],
                "expected_program_count": 2, "architecture": "arm64",
            }
            files = {
                "opt/example/bin/daemon": {"kind": "file", "mode": 0o750, "elf_machine": 183},
                "opt/example/bin/helper": {"kind": "file", "mode": 0o755, "elf_machine": 183},
                "opt/example/etc/default.conf": {"kind": "file", "mode": 0o640,
                                                   "sha256": subject.sha(root / "source/chosen.conf")},
            }
            self.assertEqual(subject.validate_install_inventory(root, files, policy),
                             ["opt/example/bin/daemon", "opt/example/bin/helper"])
            files["opt/example/bin/daemon"]["elf_machine"] = 62
            with self.assertRaises(ValueError):
                subject.validate_install_inventory(root, files, policy)


class RetainedPackagePolicyTest(unittest.TestCase):
    """Retained requirements are container declarations, independent of OpenSSH or FIPS names."""

    def test_an_unrelated_required_variant_must_be_inherited(self):
        """Enforce the declared version marker in runtime and prohibit a separate debug copy."""
        policy = package_policy()
        policy["retained_requirements"] = [{"package": "example-client", "version_contains": "+reviewed", "debug": "inherit"}]
        record = {"package": "example-client", "version": "7.2+reviewed", "architecture": "amd64",
                  "control_fields": {"Package": "example-client", "Version": "7.2+reviewed", "Architecture": "amd64"}}
        subject.validate_retained_packages([record], policy, "runtime")
        subject.validate_retained_packages([], policy, "debug")
        for records, variant in (([], "runtime"), ([record], "debug")):
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                subject.validate_retained_packages(records, policy, variant)
        record["version"] = record["control_fields"]["Version"] = "7.2"
        with self.assertRaises(ValueError):
            subject.validate_retained_packages([record], policy, "runtime")


class LibraryPayloadPolicyTest(unittest.TestCase):
    """Protect any source library's bytes and SONAME inventory with declarative paths."""

    def setUp(self):
        self.policy = {"architecture": "arm64", "runtime_path": "opt/example/lib/libsample.so.9.2",
                       "soname_links": {"opt/example/lib/libsample.so.9": "libsample.so.9.2"},
                       "exclusive_pattern": "*/libsample.so*"}
        self.original = {self.policy["runtime_path"]: {"kind": "file", "mode": 0o555, "uid": 123, "gid": 456,
                        "elf_machine": 183, "sha256": "a" * 64}}
        self.combined = {name: dict(value, uid=0, gid=0) for name, value in self.original.items()}
        self.combined["opt/example/lib/libsample.so.9"] = {"kind": "symlink", "mode": 0o777,
                                                         "linkname": "libsample.so.9.2"}

    def test_unrelated_source_library_preserves_bytes_and_soname(self):
        """The shared check accepts another architecture, version and install prefix."""
        subject.validate_library_payload(self.combined, self.original, self.policy)

    def test_repacked_library_bytes_cannot_change(self):
        """Changing content or permissions while adding a source archive must invalidate the layer."""
        for field, value in (("sha256", "b" * 64), ("mode", 0o755)):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.combined)
                changed[self.policy["runtime_path"]][field] = value
                with self.assertRaises(ValueError):
                    subject.validate_library_payload(changed, self.original, self.policy)

    def test_conflicting_version_and_incorrect_soname_link_are_rejected(self):
        """A second full library or a link to another implementation cannot satisfy the declared runtime."""
        changed = copy.deepcopy(self.combined)
        changed["opt/example/lib/libsample.so.9.3"] = changed[self.policy["runtime_path"]]
        with self.assertRaises(ValueError):
            subject.validate_library_payload(changed, self.original, self.policy)
        self.combined["opt/example/lib/libsample.so.9"]["linkname"] = "libsample.so.9.3"
        with self.assertRaises(ValueError):
            subject.validate_library_payload(self.combined, self.original, self.policy)


if __name__ == "__main__":
    OWNER_POLICIES, remaining = owner_policies()
    unittest.main(argv=[__file__, *remaining])
