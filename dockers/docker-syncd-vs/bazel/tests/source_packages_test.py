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

OWNER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OWNER.parents[1]))
sys.path.insert(0, str(OWNER / "bazel"))
from tools.bazel.tests.oci_base_fixture import digest, oci_files, write_layout
import source_packages as subject


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


class SourcePackagesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-source-packages-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contract = self.root / "source_packages.json"
        self.contract.write_bytes((OWNER / "bazel/source_packages.json").read_bytes())
        self.value = json.loads(self.contract.read_bytes())
        for record in self.value["packages"]:
            for item in record.get("required_inherited_files", {}).values():
                if item["kind"] == "file":
                    item.update(sha256=hashlib.sha256(elf()).hexdigest(), size=len(elf()))
        self.contract.write_bytes(subject.json_bytes(self.value))
        self.packages, self.debug_packages, self.modules = {}, {}, {}
        self.entries = {}
        for index, record in enumerate(self.value["packages"]):
            name = record["package"]
            entries = [("./", b"", 0o755, "directory", 123)]
            installed_paths = {path: "file" for path in record.get("path_modes", {})} | record["required_paths"]
            for path, kind in installed_paths.items():
                source_path = "var/run" + path[len("run"):] if path.startswith("run/") else path
                contents = elf() if kind == "elf" else (Path(path).name + ".0.0") if kind == "symlink" else b"configuration\n"
                entries.append((source_path, contents, 0 if kind == "symlink" else 0o555 if kind == "elf" else
                                0o755 if path.endswith(".lua") else 0o664,
                                "symlink" if kind == "symlink" else "file", 123))
            self.entries[name] = entries
            path = self.root / (name + ".tar")
            archive(path, entries)
            self.packages[name] = path
            path = self.root / (name + ".debug.tar")
            archive(path, [("usr/lib/debug/.build-id/ab/" + str(index) + "cdef.debug", elf(), 0o644, "file", 0)])
            self.debug_packages[name] = path
            source = record["source"]
            path = self.root / (source["module"] + ".MODULE.bazel")
            path.write_text('module(name = "' + source["module"] + '", version = "' + source["version"] + '")\n')
            self.modules[source["module"]] = path
        base_layer = self.root / "base.tar"
        entries = []
        for name, entry in subject.validate_payloads.DIRECTORY_ALIASES.items():
            entries += [(entry["target"], b"", 0o755, "directory", 0),
                        (name, entry["linkname"], 0o777, "symlink", 0)]
        entries += [("usr/lib/x86_64-linux-gnu/libswsscommon.so.0.0.0", elf(), 0o644, "file", 0),
                    ("usr/lib/x86_64-linux-gnu/libswsscommon.so.0", "libswsscommon.so.0.0.0", 0o777, "symlink", 0)]
        for record in self.value["packages"]:
            for path, item in record.get("required_inherited_files", {}).items():
                entries.append((path, item.get("linkname", elf()), item["mode"], item["kind"], 0))
        archive(base_layer, entries)
        layer = base_layer.read_bytes()
        config = {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": [digest(layer)]}}
        self.base = self.root / "base.oci"
        write_layout(self.base, oci_files(json.dumps(config).encode(), [layer]))
        self.runtime_tar, self.debug_tar = self.root / "runtime.tar", self.root / "debug.tar"
        self.receipt = self.root / "receipt.json"

    def build(self):
        result = subject.assemble(self.contract, self.packages, self.debug_packages, self.modules,
                                  self.base, self.runtime_tar, self.debug_tar)
        self.receipt.write_bytes(subject.json_bytes(result))
        return result

    def test_reuses_owner_payload_bytes_and_records_new_source_provenance(self):
        """A source receipt identifies owner modules/tars, never the retired Make DEBs."""
        result = self.build()
        self.assertEqual(subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar), result)
        for record in result["packages"]:
            self.assertEqual(record["input_tar_sha256"], subject.sha(self.packages[record["package"]]))
            self.assertEqual(record["debug"]["input_tar_sha256"], subject.sha(self.debug_packages[record["package"]]))
            self.assertNotIn("source_deb", record)
            self.assertNotIn("source_sha256", record)
            self.assertNotIn("control_sha256", record)
            self.assertTrue(all(item["uid"] == item["gid"] == 0 for item in record["files"].values()))
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

    def test_unmigrated_common_library_remains_bound_to_the_actual_base(self):
        """Reusing Common's tar cannot lose or replace libsonicdbcli, which remains a Make/base output."""
        result = self.build()
        self.assertEqual(result["inherited_files"], self.value["packages"][0]["required_inherited_files"])
        value = copy.deepcopy(self.value)
        path = "usr/lib/x86_64-linux-gnu/libsonicdbcli.so.0.0.0"
        value["packages"][0]["required_inherited_files"][path]["sha256"] = "0" * 64
        self.contract.write_bytes(subject.json_bytes(value))
        with self.assertRaisesRegex(ValueError, "requires unchanged inherited content"):
            self.build()
        self.contract.write_bytes(subject.json_bytes(self.value))
        entries = self.entries["libswsscommon"] + [(path, elf() + b"changed", 0o644, "file", 0)]
        archive(self.packages["libswsscommon"], entries)
        with self.assertRaisesRegex(ValueError, "changes required inherited content"):
            self.build()

    def test_resolved_module_drift_fails_before_publishing_archives(self):
        """A newer transitive module cannot be described as the reviewed old source commit."""
        path = self.modules["sonic-swss-common"]
        path.write_text('module(name="sonic-swss-common", version="0.0.0-new")\n')
        with self.assertRaisesRegex(ValueError, "resolved source module differs"):
            self.build()
        self.assertFalse(self.runtime_tar.exists())
        self.assertFalse(self.debug_tar.exists())

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
        """The symbol adapter cannot smuggle replacement runtime files into the debug image."""
        archive(self.debug_packages["libsairedis"], [("usr/bin/syncd", elf(), 0o755, "file", 0)])
        with self.assertRaisesRegex(ValueError, "symbols contain an unexpected file"):
            self.build()
        archive(self.debug_packages["libsairedis"], [])
        with self.assertRaisesRegex(ValueError, "no debug symbols"):
            self.build()

    def test_identical_transitive_symbols_keep_both_owners_without_duplicate_tar_members(self):
        """Shared debug providers may expose one symbol file twice; only equal bytes can be reused."""
        first = self.debug_packages["libswsscommon"]
        second = self.debug_packages["libsairedis"]
        second.write_bytes(first.read_bytes())
        value = self.build()
        common, sairedis = value["packages"][:2]
        self.assertEqual(common["debug"]["files"], sairedis["debug"]["files"])
        subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar)
        path = next(iter(common["debug"]["files"]))
        archive(second, [(path, elf() + b"different", 0o644, "file", 0)])
        with self.assertRaisesRegex(ValueError, "source packages overlap"):
            self.build()

    def test_receipt_binds_archive_hashes_and_per_package_file_ownership(self):
        """Final validators reject changed archives and owner inventories even with valid JSON."""
        self.build()
        before = self.runtime_tar.read_bytes()
        self.runtime_tar.write_bytes(before + b"changed")
        with self.assertRaisesRegex(ValueError, "archive hash differs"):
            subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar)
        self.runtime_tar.write_bytes(before)
        value = json.loads(self.receipt.read_bytes())
        value["packages"][0]["files"]["usr/bin/swssloglevel"]["sha256"] = "a" * 64
        self.receipt.write_bytes(subject.json_bytes(value))
        with self.assertRaisesRegex(ValueError, "archive inventory differs"):
            subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar)

    def test_contract_rejects_wrong_owner_and_incomplete_dependency_metadata(self):
        """Source provenance and dependency checks cannot be weakened by changing one contract field."""
        variants = []
        value = copy.deepcopy(self.value)
        value["packages"][0]["source"]["target"] = "@sonic_swss//dist:swss_pkg"
        variants.append(value)
        value = copy.deepcopy(self.value)
        del value["packages"][0]["control_fields"]["Depends"]
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
                else:
                    altered["packages"][0][field]["Depends"] = "libc6"
                self.receipt.write_bytes(subject.json_bytes(altered))
                with self.assertRaisesRegex(ValueError, "differs from the reviewed contract"):
                    subject.validate_receipt(self.receipt, self.runtime_tar, self.debug_tar, contract_path=self.contract)

    def test_retained_manifest_keeps_make_and_source_provenance_separate(self):
        """APT sees source dependencies while the original Make input and debug binding remain exact."""
        result = self.build()
        value = {**subject.IDENTITY, "variant": "debug", "features": dict(subject.validate_payloads.FEATURES),
                 "packages": [{"package": "syncd-vs", "source_deb": "syncd-vs_1.0.0_amd64.deb"}],
                 "runtime_manifest_sha256": "1" * 64, "required_packages": ["syncd-vs"]}
        path = self.root / "make.json"
        path.write_bytes(subject.json_bytes(value))
        combined = subject.retained_manifest(path, result, subject.sha(self.receipt), variant="debug")
        self.assertEqual(combined["packages"], value["packages"])
        self.assertEqual(combined["runtime_manifest_sha256"], value["runtime_manifest_sha256"])
        self.assertEqual(combined["make_manifest_sha256"], subject.sha(path))
        self.assertEqual(combined["source_receipt_sha256"], subject.sha(self.receipt))
        self.assertEqual(combined["source_packages"], result["packages"])
        for name in ("libswsscommon", "libsairedis-dbgsym"):
            value["packages"] = [{"package": name}]
            path.write_bytes(subject.json_bytes(value))
            with self.assertRaisesRegex(ValueError, "packages now built from source"):
                subject.retained_manifest(path, result, subject.sha(self.receipt), variant="debug")

    def test_retained_manifest_allows_only_reviewed_base_common_symbols_in_debug(self):
        """Keep the inherited CLI library's symbols while rejecting the old Common runtime/symbol payloads."""
        import base_debug_symbols

        contract = base_debug_symbols.read_contract()
        retained = copy.deepcopy(contract["package"])
        retained.update(original_payload_sha256=contract["original_payload"]["sha256"],
                        original_payload_size=contract["original_payload"]["size"],
                        original_payload_members=contract["original_payload"]["members"],
                        base_debug_symbols=base_debug_symbols.descriptor(),
                        payload_sha256="a" * 64, payload_size=307200, payload_members=2)
        result = self.build()
        value = {**subject.IDENTITY, "variant": "debug", "features": dict(subject.validate_payloads.FEATURES),
                 "packages": [retained], "runtime_manifest_sha256": "1" * 64}
        path = self.root / "make-base-symbols.json"
        path.write_bytes(subject.json_bytes(value))
        combined = subject.retained_manifest(path, result, subject.sha(self.receipt), variant="debug")
        self.assertEqual(combined["packages"], [retained])
        self.assertEqual(combined["source_packages"], result["packages"])

        value["variant"] = "runtime"
        path.write_bytes(subject.json_bytes(value))
        with self.assertRaisesRegex(ValueError, "packages now built from source"):
            subject.retained_manifest(path, result, subject.sha(self.receipt), variant="runtime")
        value["variant"] = "debug"
        for change in ("unfiltered", "wrong_source", "extra_member"):
            with self.subTest(change=change):
                record = copy.deepcopy(retained)
                if change == "unfiltered":
                    del record["base_debug_symbols"]
                elif change == "wrong_source":
                    record["source_sha256"] = "b" * 64
                else:
                    record["payload_members"] = 3
                value["packages"] = [record]
                path.write_bytes(subject.json_bytes(value))
                with self.assertRaises(ValueError):
                    subject.retained_manifest(path, result, subject.sha(self.receipt), variant="debug")


if __name__ == "__main__":
    unittest.main()
