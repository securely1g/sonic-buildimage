"""Public cache evidence must exclude command, log and environment payloads."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "p4_verify_cache_summary", ROOT / "tools/bazel/p4/verify_cache.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class PublicSummaryTest(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT / VERIFY.LOCK).read_text())
        self.version = (ROOT / ".bazelversion").read_text().strip()
        packages = [{"filename": p["filename"], "sha256": p["sha256"], "bytes": p["size"],
                     "control": {"Package": p["name"], "Version": p["version"],
                                 "Architecture": "amd64"}, "elf_machine": "AMD64",
                     "elf_files": ["usr/lib/example.so"]} for p in self.lock["packages"]]
        first = self.lock["packages"][0]
        self.result = {"revision": "a" * 40, "revision_parents": ["b" * 40, "c" * 40],
                       "bazel_version": "bazel " + self.version, "architecture": "x86_64",
                       "target": VERIFY.TARGET, "make_invoked": True,
                       "native_package_producer_invoked": False,
                       "package_count": len(packages), "phases": {}}
        for name in ("cold", "warm", "missing", "tampered", "wrong-pin"):
            phase = {"exit_code": 0 if name in ("cold", "warm") else 1,
                     "fresh_checkout": True, "fresh_output_base": True,
                     "repository_downloads_disabled": name in ("warm", "missing", "tampered")}
            if name in ("cold", "warm"):
                phase.update(packages=copy.deepcopy(packages),
                             action_audit={"registered_actions": 0, "target": VERIFY.TARGET},
                             sbom={"filename": first["filename"], "distribution_url": first["url"],
                                   "sha256": first["sha256"], "native_source_attribution": False})
            self.result["phases"][name] = phase

    def summary(self):
        return VERIFY.public_summary(self.result, self.lock, self.version)

    def test_preserves_successful_import_and_negative_cache_evidence(self):
        summary = self.summary()
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(len(summary["phases"]), 5)
        self.assertEqual(summary["phases"]["warm"]["registered_actions"], 0)
        self.assertTrue(summary["phases"]["missing"]["repository_downloads_disabled"])
        self.assertEqual(summary["packages"][0]["sha256"], self.lock["packages"][0]["sha256"])

    def test_drops_raw_commands_logs_urls_and_environment_fields(self):
        marker = "DO_NOT_PUBLISH_PRIVATE_DIAGNOSTIC"
        self.result.update(github_sha=marker, environment={"AUTH": marker}, log=marker)
        for phase in self.result["phases"].values():
            phase.update(command=[marker], stderr=marker)
            for package in phase.get("packages", []):
                package["elf_files"] = [marker]
        encoded = json.dumps(self.summary())
        self.assertNotIn(marker, encoded)
        self.assertNotIn("https://", encoded)
        self.assertNotIn("command", encoded)

    def test_accepts_bazelisk_banner_without_copying_raw_version_output(self):
        self.result["bazel_version"] = "Bazelisk v1.29.0\nbazel " + self.version
        self.assertEqual(self.summary()["bazel_version"], self.version)

    def test_rejects_extra_version_output(self):
        self.result["bazel_version"] = "PRIVATE_DIAGNOSTIC\nbazel " + self.version
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_missing_phase(self):
        del self.result["phases"]["wrong-pin"]
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_succeeded_negative_case(self):
        self.result["phases"]["tampered"]["exit_code"] = 0
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_wrong_package_bytes_or_identity(self):
        for field, value in (("bytes", 1), ("sha256", "d" * 64), ("filename", "other.deb")):
            with self.subTest(field=field):
                result = copy.deepcopy(self.result)
                result["phases"]["warm"]["packages"][0][field] = value
                with self.assertRaises(ValueError):
                    VERIFY.public_summary(result, self.lock, self.version)

    def test_rejects_registered_actions(self):
        self.result["phases"]["cold"]["action_audit"]["registered_actions"] = 1
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_non_digest_revision(self):
        self.result["revision"] = "private environment payload"
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_package_filename_path(self):
        self.lock["packages"][0]["filename"] = "../private.deb"
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_native_producer_claim(self):
        self.result["native_package_producer_invoked"] = True
        with self.assertRaises(ValueError):
            self.summary()

    def test_rejects_network_use_in_offline_case(self):
        self.result["phases"]["warm"]["repository_downloads_disabled"] = False
        with self.assertRaises(ValueError):
            self.summary()


if __name__ == "__main__":
    unittest.main()
