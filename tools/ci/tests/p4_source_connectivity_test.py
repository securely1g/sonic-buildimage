#!/usr/bin/env python3
"""Offline failure/identity checks for the temporary hosted diagnostic."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("probe", Path(__file__).resolve().parents[1] / "p4_source_connectivity.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def dsc(package="p4lang-pi", version="0.1.3-2"):
    upstream = version.rsplit("-", 1)[0]
    sha = hashlib.sha256(b"original source").hexdigest()
    return (f"Format: 3.0 (quilt)\nSource: {package}\nVersion: {version}\nChecksums-Sha256:\n"
            f" {sha} 15 {package}_{upstream}.orig.tar.gz\n"
            f" {sha} 15 {package}_{version}.debian.tar.xz\n").encode()


class ConnectivityTest(unittest.TestCase):
    def test_exact_identity_and_both_sources(self):
        files = probe.descriptor_files(dsc(), "p4lang-pi", "0.1.3-2")
        self.assertEqual(set(files), {"p4lang-pi_0.1.3.orig.tar.gz", "p4lang-pi_0.1.3-2.debian.tar.xz"})
        for package, version in [("p4lang-pi", "0.1.1-1"), ("other", "0.1.3-2")]:
            with self.assertRaises(ValueError):
                probe.descriptor_files(dsc(), package, version)

    def test_missing_duplicate_unsafe_or_weak_checksum_refused(self):
        original = dsc()
        variants = [original.replace(b"Checksums-Sha256", b"Checksums-Sha1"),
                    original + original.splitlines(keepends=True)[-1],
                    original.replace(b"p4lang-pi_0.1.3.orig", b"../p4lang-pi_0.1.3.orig"),
                    original.replace(b"Version: 0.1.3-2", b"Version: 0.1.3-2\nVersion: 0.1.3-2"),
                    original.replace(b" 15 ", b" 999999999999 ")]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.descriptor_files(value, "p4lang-pi", "0.1.3-2")

    def test_clear_signed_descriptor_parses_without_claiming_signature_verification(self):
        value = b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n" + dsc() + b"-----BEGIN PGP SIGNATURE-----\nopaque\n"
        self.assertEqual(probe.descriptor_files(value, "p4lang-pi", "0.1.3-2"),
                         probe.descriptor_files(dsc(), "p4lang-pi", "0.1.3-2"))

    def fake_fetch(self, mismatch=False, failure=False):
        def fetch(out, name, maximum, deadline):
            if failure and name.endswith(".dsc"):
                return {"status": "download_failed", "attempts": [{"curl_exit": 28}]}
            if name.endswith(".dsc"):
                package, version = name[:-4].split("_")
                data = dsc(package, version)
            else:
                data = b"changed source!" if mismatch and "p4lang-p4c" in name else b"original source"
            (out / name).write_bytes(data)
            return {"status": "downloaded", **probe.digest(out / name)}
        return fetch

    def test_valid_sources_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "new"
            with patch.object(probe, "fetch", self.fake_fetch()):
                self.assertEqual(probe.run(out), 0)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(report["packages"]), 3)

    def test_mismatch_fails_and_preserves_original_descriptor_and_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "new"
            with patch.object(probe, "fetch", self.fake_fetch(mismatch=True)):
                self.assertEqual(probe.run(out), 1)
            report = json.loads((out / "report.json").read_text())
            entry = report["packages"]["p4lang-p4c"]
            self.assertEqual(entry["status"], "source_failed")
            self.assertEqual(report["packages"]["p4lang-pi"]["status"], "verified")
            self.assertTrue(all(not v["verified"] for v in entry["sources"].values()))
            self.assertEqual((out / "sources/p4lang-p4c_1.2.4.2-3.dsc").read_bytes(), dsc("p4lang-p4c", "1.2.4.2-3"))
            self.assertEqual((out / "sources/p4lang-p4c_1.2.4.2.orig.tar.gz").read_bytes(), b"changed source!")

    def test_transport_failure_is_reported_for_all_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "new"
            with patch.object(probe, "fetch", self.fake_fetch(failure=True)):
                self.assertEqual(probe.run(out), 1)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(len(report["packages"]), 3)
            self.assertTrue(all(v["status"] == "descriptor_failed" for v in report["packages"].values()))

    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(FileExistsError):
            probe.run(Path(temporary))

    def test_retries_and_curl_transport_are_bounded(self):
        result = subprocess.CompletedProcess([], 28, b"000\n0\n0\n0\n0\n15\n0\n0\n" + probe.BASE.encode(), b"timeout")
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe.subprocess, "run", return_value=result) as call, patch.object(probe.time, "sleep"):
            record = probe.fetch(Path(temporary), "fixed.dsc", 1024, probe.time.monotonic() + 900)
        self.assertEqual(len(record["attempts"]), 3)
        self.assertEqual(call.call_count, 3)
        args = call.call_args.args[0]
        self.assertEqual(args[0:2], ["/usr/bin/curl", "--disable"])
        self.assertEqual(args[args.index("--proto") + 1], "=https")
        self.assertEqual(args[args.index("--proto-redir") + 1], "=https")
        self.assertLessEqual(float(args[args.index("--max-time") + 1]), 120)
        self.assertNotIn("stderr", record["attempts"][0])

    def test_exhausted_budget_does_not_start_curl(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe.subprocess, "run") as call:
            record = probe.fetch(Path(temporary), "fixed.dsc", 1024, probe.time.monotonic() - 1)
        call.assert_not_called()
        self.assertEqual(record["status"], "time_budget_exhausted")


if __name__ == "__main__":
    unittest.main()
