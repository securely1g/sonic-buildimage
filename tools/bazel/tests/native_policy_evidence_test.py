"""Reuse canonical policy safety checks; no packages, processes, or network."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from tools.bazel.ci import image_worker_receipt as worker
from tools.bazel.ci import native_payload_inventory as inventory
from tools.bazel.ci import native_payload_recovery as recovery
from tools.bazel.ci import public_artifacts


def policy():
    return {"schema": 1, "image_validation": {
        "environment": {"DEBIAN_FRONTEND": "noninteractive"},
        "architecture": "amd64"}}


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


class NativePolicyEvidenceTest(unittest.TestCase):
    def test_existing_checked_in_environment_preserves_original_value(self):
        for family in ("syncd", "gearbox"):
            with self.subTest(family=family):
                value = policy()
                original = copy.deepcopy(value)
                with self.assertRaisesRegex(ValueError, "diagnostic-json-field"):
                    public_artifacts.safe_json(value)
                self.assertIs(worker.validate_public_evidence(
                    value, "policy.json", {"image": family}), value)
                self.assertEqual(value, original)

    def test_other_environments_remain_rejected(self):
        for environment in ({}, {"DEBIAN_FRONTEND": "interactive"},
                            {"DEBIAN_FRONTEND": "noninteractive", "HOME": "/tmp"},
                            [], None):
            with self.subTest(environment=environment):
                value = policy()
                value["image_validation"]["environment"] = environment
                with self.assertRaises(ValueError):
                    worker.validate_public_evidence(value, "policy.json", {"image": "syncd"})

    def test_exception_does_not_apply_to_other_records_or_p4rt(self):
        for name, family in (("ordinary.json", "syncd"), ("policy.json", "p4rt")):
            with self.subTest(name=name, family=family), self.assertRaises(ValueError):
                worker.validate_public_evidence(policy(), name, {"image": family})

    def test_other_diagnostics_and_credentials_remain_rejected(self):
        for key, child in (("environment", {"HOME": "/tmp"}),
                           ("argv", ["fixture"]), ("access_token", "fixture-only")):
            with self.subTest(key=key):
                value = policy()
                value[key] = child
                with self.assertRaises(ValueError):
                    worker.validate_public_evidence(value, "policy.json", {"image": "gearbox"})

    def test_file_reader_keeps_exact_original_bytes(self):
        raw = encoded(policy()) + b"\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            path.write_bytes(raw)
            self.assertEqual(worker.read_evidence(path, {"image": "syncd"}), policy())
            self.assertEqual(path.read_bytes(), raw)

    def selected(self, request):
        raw = encoded(policy()) + b"\n"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("policy.json", raw)
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            return inventory.selected_public_json(archive, archive.infolist(),
                [{"name": "policy.json", "sha256": hashlib.sha256(raw).hexdigest()}],
                time.monotonic() + 10, request), raw

    def test_collector_uses_canonical_safety_and_retains_original_bytes(self):
        for family in ("syncd", "gearbox"):
            with self.subTest(family=family):
                selected, raw = self.selected({"image": family})
                self.assertEqual(selected["policy.json"]["raw"], raw)
                self.assertEqual(selected["policy.json"]["validator"],
                                 "worker.validate_public_evidence")

    def test_collector_requires_its_admitted_policy_request(self):
        for request in (None, {"image": "p4rt"}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.selected(request)

    def test_original_upload_review_reuses_policy_guard_without_acceptance(self):
        public = {"policy.json": encoded(policy()),
                  "public-receipt.json": encoded({"artifacts": {}})}
        rows = {name: {"type": "regular", "bytes": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest()}
                for name, raw in public.items()}
        admission = {"original_request": {"image": "syncd"},
                     "canonical_native_acceptance": False, "original_conclusion": "failure"}
        with mock.patch.object(recovery, "admit_original", return_value=admission), \
             mock.patch.object(recovery, "checked_inventory", return_value=rows), \
             mock.patch.object(worker, "public_json_names", return_value=set(public)), \
             mock.patch.object(recovery, "public_semantics", return_value={"observed": {}}):
            result = recovery.review_public_upload(b"fixture", {"outputs": {}}, {}, {}, {},
                {}, public, {name: name for name in public})
        self.assertEqual(result["status"], "PUBLIC_INVENTORY_CONSISTENT_NOT_ACCEPTED")
        self.assertFalse(result["canonical_native_acceptance"])
        self.assertEqual(result["original_conclusion"], "failure")


if __name__ == "__main__":
    unittest.main()
