#!/usr/bin/env python3
"""Keep inherited-library symbols without restoring obsolete Common companions.

Small tar fixtures exercise filtering and ownership; no Debian package is built.
Real build IDs, DWARF and DWZ lookup are checked against the assembled image.
"""

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

OWNER = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(OWNER))
import base_debug_symbols as subject


def archive(path, entries):
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as output:
        for name, data in entries:
            member = tarfile.TarInfo("./" + name)
            member.size, member.mode, member.mtime = len(data), 0o644, 100
            output.addfile(member, io.BytesIO(data))


class BaseDebugSymbolsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="syncd-base-symbols-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contract_path = self.root / "contract.json"
        self.contract = subject.read_contract()
        self.contents = {name: ("fixture " + name).encode() for name in self.contract["files"]}
        self.stale = "usr/lib/debug/.build-id/ff/obsolete-common.debug"
        self.payload = self.root / "original.tar"
        archive(self.payload, [*self.contents.items(), (self.stale, b"obsolete symbols")])
        for name, content in self.contents.items():
            self.contract["files"][name].update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
        self.contract["original_payload"] = {"sha256": subject.sha(self.payload),
                                             "size": self.payload.stat().st_size, "members": 3}
        self.write_contract()
        self.record = copy.deepcopy(self.contract["package"])
        self.record.update({"payload_" + key: value for key, value in self.contract["original_payload"].items()})

    def write_contract(self):
        self.contract_path.write_text(json.dumps(self.contract))

    def filter(self):
        subject.filter_payload(self.record, self.payload, contract_path=self.contract_path)

    def validate(self, *, variant="debug"):
        return subject.validate_aggregate({"variant": variant, "packages": [self.record]},
                                          self.payload, contract_path=self.contract_path)

    def test_filter_retains_only_companion_and_dwz_with_original_provenance(self):
        """The two base-symbol files survive while old source-library symbols are discarded."""
        original = copy.deepcopy(self.record)
        self.filter()
        self.assertEqual(self.validate(), [subject.descriptor(self.contract_path)])
        for key, expected in self.contract["package"].items():
            self.assertEqual(self.record[key], expected)
        self.assertEqual(self.record["original_payload_sha256"], original["payload_sha256"])
        self.assertNotEqual(self.record["payload_sha256"], original["payload_sha256"])
        with tarfile.open(self.payload) as source:
            self.assertEqual({str(Path(member.name)) for member in source}, set(self.contents))
            self.assertTrue(all(member.mtime == 0 for member in source))

    def test_changed_original_package_or_payload_cannot_be_filtered(self):
        """Hash and control pins reject a different archive before it gains filtered provenance."""
        for field in ("source_sha256", "control_sha256", "payload_sha256"):
            original = self.record[field]
            self.record[field] = "a" * 64
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "original (package|payload) changed"):
                self.filter()
            self.record[field] = original
        self.payload.write_bytes(self.payload.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "extracted payload changed"):
            self.filter()

    def test_filtered_identity_cannot_weaken_original_metadata(self):
        """Validators require the explicit filter and exact original package/control identities."""
        self.filter()
        original = copy.deepcopy(self.record)
        for field, value in (("source_sha256", "a" * 64), ("original_payload_sha256", "b" * 64),
                             ("base_debug_symbols", {}), ("payload_members", 3),
                             ("control_fields", {"Package": "libswsscommon-dbgsym"})):
            self.record = copy.deepcopy(original)
            self.record[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                subject.check_record(self.record, contract_path=self.contract_path)

    def test_aggregate_rejects_changed_missing_or_extra_symbols(self):
        """A valid manifest cannot disguise changed bytes, a lost DWZ file, or obsolete symbols."""
        self.filter()
        entries = list(self.contents.items())
        cases = [entries[:1], entries + [(self.stale, b"obsolete symbols")],
                 [(entries[0][0], b"changed")] + entries[1:]]
        for values in cases:
            archive(self.payload, values)
            with self.subTest(paths=[name for name, _ in values]), self.assertRaises(ValueError):
                self.validate()

    def test_filter_is_debug_only_and_cannot_change_package_owner(self):
        """Runtime rejects even the valid filter, and other packages cannot borrow its exception."""
        self.filter()
        with self.assertRaisesRegex(ValueError, "only in the debug handoff"):
            self.validate(variant="runtime")
        self.record["package"] = "libsairedis-dbgsym"
        with self.assertRaisesRegex(ValueError, "original package changed"):
            self.validate()

    def test_committed_contract_has_only_the_inherited_build_id_and_its_dwz(self):
        """The reviewed input keeps the real base-library pair, not all symbols from the Common DEB."""
        contract = subject.read_contract()
        build_id = contract["runtime"]["build_id"]
        companion = "usr/lib/debug/.build-id/" + build_id[:2] + "/" + build_id[2:] + ".debug"
        self.assertEqual(set(contract["files"]), {
            companion, "usr/lib/debug/.dwz/x86_64-linux-gnu/libswsscommon.debug"})
        self.assertEqual(contract["files"][companion]["build_id"], build_id)
        self.assertEqual(contract["package"]["source_sha256"],
                         "70ee13e76614433ab962620e31d0cac04d429e95575de10fcd3e4d606fd505af")


if __name__ == "__main__":
    unittest.main()
