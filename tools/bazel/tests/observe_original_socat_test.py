#!/usr/bin/env python3
"""Bounded metadata-only tests; no DEB construction or executable invocation."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import observe_original_socat as subject

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "tools/bazel/ci/vs_socat_original_binary_comparison.json"
SECTIONS = b"  [ 0]                   NULL            0000000000000000 000000 000000 00      0 0 0\n  [ 1] .text PROGBITS 0000000000000010 000010 000004 00 AX 0 0 1\n"


def tar_body(data):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        item = tarfile.TarInfo(subject.BINARY_NAME)
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


class SocatOriginalComparisonTest(unittest.TestCase):
    def test_fixed_manifest_contains_three_distinct_originals(self):
        value = subject.load_manifest(MANIFEST)
        self.assertEqual(len(value["inputs"]), 3)
        self.assertEqual({r["binary"]["bytes"] for r in value["inputs"]}, {477416})
        self.assertEqual(value["artifact"]["id"], 14030424431)

    def test_tampered_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(MANIFEST.read_bytes() + b" ")
            with self.assertRaisesRegex(subject.original.RecoveryError, "manifest differs"):
                subject.load_manifest(path)

    def test_upstream_sections_keep_file_offsets_and_hashes(self):
        data = bytes(range(64))
        rows = subject.section_metadata(SECTIONS, data)
        self.assertEqual([r["name"] for r in rows], [".text"])
        self.assertEqual(rows[0]["offset"], 16)
        self.assertEqual(rows[0]["sha256"], hashlib.sha256(data[16:20]).hexdigest())

    def test_out_of_file_section_is_rejected(self):
        with self.assertRaisesRegex(subject.original.RecoveryError, "section exceeds"):
            subject.section_metadata(SECTIONS.replace(b"000010 000004", b"000040 000004"), bytes(64))

    def test_duplicate_section_name_is_rejected(self):
        with self.assertRaisesRegex(subject.original.RecoveryError, "repeated ELF section"):
            subject.section_metadata(SECTIONS + SECTIONS.splitlines(keepends=True)[1], bytes(64))

    def test_identical_binaries_have_no_differences(self):
        value = subject.differences(b"abc", b"abc", [])
        self.assertEqual(value["different_bytes"], 0)
        self.assertEqual(value["reported_ranges"], [])

    def test_changed_bytes_are_bound_to_their_section(self):
        left, right = b"x" * 64, b"x" * 17 + b"ab" + b"x" * 45
        value = subject.differences(left, right, [{"name": ".text", "type": "PROGBITS", "offset": 16, "bytes": 4}])
        self.assertEqual(value["different_bytes"], 2)
        self.assertEqual(value["reported_ranges"][0]["sections"], [".text"])
        self.assertEqual(value["reported_ranges"][0]["offset"], 17)

    def test_difference_reports_are_bounded_without_losing_total_count(self):
        value = subject.differences(b"ab" * 100, b"ac" * 100, [])
        self.assertEqual(value["different_bytes"], 100)
        self.assertEqual(value["ranges"], 100)
        self.assertEqual(len(value["reported_ranges"]), 64)
        self.assertTrue(value["ranges_truncated"])

    def test_trailing_size_difference_is_preserved(self):
        value = subject.differences(b"abc", b"abcd", [])
        self.assertEqual(value["different_bytes"], 1)
        self.assertEqual(value["reported_ranges"][0]["offset"], 3)
        self.assertEqual((value["left_bytes"], value["right_bytes"]), (3, 4))

    def test_comparison_does_not_assert_code_equivalence(self):
        records = [{"binary_sha256": hashlib.sha256(bytes([i])).hexdigest(),
                    "body": bytes([i]), "sections": []} for i in range(3)]
        result = subject.compare(records)
        self.assertEqual(len(result["pairs"]), 2)
        self.assertNotIn("source_admission", result)
        self.assertTrue(all(r["different_bytes"] == 1 for r in result["pairs"]))

    def test_wrong_original_binary_hash_fails_before_readelf(self):
        data = b"\x7fELF" + b"x" * 60
        @contextlib.contextmanager
        def reader(*args):
            yield io.BytesIO(tar_body(data))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package = root / "original-input"
            package.write_bytes(b"opaque original; dpkg reader mocked")
            with mock.patch.object(subject.host, "run_upstream", side_effect=reader) as called:
                with self.assertRaisesRegex(subject.original.RecoveryError, "ELF differs"):
                    subject.read_binary(package, {}, root, {"bytes": len(data), "sha256": "0" * 64})
                self.assertEqual(called.call_count, 1)

    def test_only_upstream_dpkg_and_readelf_are_invoked(self):
        data = b"\x7fELF" + b"x" * 60
        calls = []
        @contextlib.contextmanager
        def reader(tool, arguments, source, scratch, bound):
            calls.append((tool, arguments))
            yield io.BytesIO(tar_body(data) if tool == "/usr/bin/dpkg-deb" else SECTIONS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); package = root / "original-input"
            package.write_bytes(b"opaque original; dpkg reader mocked")
            with mock.patch.object(subject.host, "run_upstream", side_effect=reader):
                observed, sections = subject.read_binary(package, {}, root, {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            self.assertEqual(observed, data)
            self.assertEqual([r["name"] for r in sections], [".text"])
            self.assertEqual(calls, [("/usr/bin/dpkg-deb", ["--fsys-tarfile"]), ("/usr/bin/readelf", ["--wide", "--section-headers"])])


if __name__ == "__main__":
    unittest.main()
