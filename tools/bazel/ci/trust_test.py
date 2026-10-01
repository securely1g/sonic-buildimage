#!/usr/bin/env python3
"""Test explicit execution trust without Docker, network, or host trust changes."""

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import ssl
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

import trust

# Synthetic public test certificates. Their private keys are not stored in Git.
TEST_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIIDUzCCAjugAwIBAgIUS0xg3TY9kBR+xH+X1rFxnms+rt8wDQYJKoZIhvcNAQEL
BQAwOTEbMBkGA1UEAwwSU09OaUMgdW5pdCB0ZXN0IENBMRowGAYDVQQKDBFUZXN0
IGZpeHR1cmUgb25seTAeFw0yNjEwMDExNjQyMjVaFw0zNjA5MjgxNjQyMjVaMDkx
GzAZBgNVBAMMElNPTmlDIHVuaXQgdGVzdCBDQTEaMBgGA1UECgwRVGVzdCBmaXh0
dXJlIG9ubHkwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDpKlWOLfPh
vP+/lwZehkGteHRR4b0+d95gBioRdxMseC2nUQCKH7iO3YQ2zKTOcZyd47Bp58VR
VIqpeRIwiEvpxDJrAX0dZj/uYea9ozqqqXDxlXpUsvIdYqNyZQtxD1oUeSEIDcBc
ardo0W2UoG79/aanrMbecKRruEXCdxhz8IyBMKX+HAZOFn6hdTHXsFX5IsBFZYrk
7q2d2TKCRBxsZ6eLHSoExqO5OOfnv7/3Dt6bzuWHuK7rd+ouIW3RXjqVVqD9MZWQ
DqxsvX+h5DUHvXvSY9aJMwxqyop9WQgvbwhoCgobJ9LzAkU/gjSB9ir4dNBURglh
BJ+FfsZjelrDAgMBAAGjUzBRMB0GA1UdDgQWBBRxFlsLSDrfZcMkp6/qMj7ouoj4
GzAfBgNVHSMEGDAWgBRxFlsLSDrfZcMkp6/qMj7ouoj4GzAPBgNVHRMBAf8EBTAD
AQH/MA0GCSqGSIb3DQEBCwUAA4IBAQDQb3dqDp/RjJFrZJRD7J0VgtnwQEXXIwOP
Bd4cNRZdmIVfqBZw2uqYPM8Q0BmJiIatzo3k+zhLduXMzDarW1FFHxaM6mrfBPKb
JSbgG5e3mLVtUzF987AOiXdQgAZQjz1+edntEpzVobO9r3WCwFDTgdKffFiyU9kz
XSuA6woMcgFZlon6R+J77M3PTUHBG0SSxGlSvjxqZDMCEojgYufM1seIGVMV1xVP
E5utPbR5cH4xbOPspcm7EXAbzKcJgtHgbqsE2dBOTG+VrDLKxRsf+QCJetmWP9IN
V9aSB8LkFYwqa+Oc+mPbt4DMkVpn8rTcRxDkQcmOBQnoha6yj1gK
-----END CERTIFICATE-----
"""

SYSTEM_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIIBnzCCAUWgAwIBAgIUY1rX4DlcSCC+oR8HslDFwwaTwO0wCgYIKoZIzj0EAwIw
JTEjMCEGA1UEAwwaU09OaUMgZGVmYXVsdCByb290IHRlc3QgQ0EwHhcNMjYxMDAx
MTY0NDM2WhcNMzYwOTI4MTY0NDM2WjAlMSMwIQYDVQQDDBpTT05pQyBkZWZhdWx0
IHJvb3QgdGVzdCBDQTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABNauzgTUpWF2
n5MRGgo4Y6SurWGmgPUbm7bZdw3y8CjpusNAHdgKQGDVqND+jrAE8TT6LKmgEISC
OoVtsQa7HdmjUzBRMB0GA1UdDgQWBBS4bS6y8KIW4ikNEskuZt1RgO/oETAfBgNV
HSMEGDAWgBS4bS6y8KIW4ikNEskuZt1RgO/oETAPBgNVHRMBAf8EBTADAQH/MAoG
CCqGSM49BAMCA0gAMEUCIQCJH5eg+AI+X8P9dZVsbG9pSnCMZ5E2dOZU63C6BsJZ
0AIgYLPt8Y3ectXW/kaR0FAIr9kn/sV13yZFCdiQcPfh4f8=
-----END CERTIFICATE-----
"""


class TrustTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "input.pem"
        self.bundle.write_bytes(TEST_CA_PEM)
        self.destination = self.root / "staged.pem"
        self.worker = self.root / "worker"
        self.worker.mkdir()
        self.system_bundle = self.worker / "etc/ssl/certs/ca-certificates.crt"
        self.system_bundle.parent.mkdir(parents=True)
        self.system_bundle.write_bytes(SYSTEM_CA_PEM)
        self.bazel = self.root / "bazel"
        self.write_jdk()

    def write_jdk(self, extra=None):
        with zipfile.ZipFile(self.bazel, "w") as archive:
            for name, contents, mode in (
                    ("bin/java", b"fixture java", 0o100755),
                    ("bin/keytool", b"fixture keytool", 0o100755),
                    ("lib/security/cacerts", b"existing Java trust store", 0o100644)):
                info = zipfile.ZipInfo("embedded_tools/jdk/" + name)
                info.external_attr = mode << 16
                archive.writestr(info, contents)
            if extra:
                name, mode = extra
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, b"escape")

    def test_valid_bundle_is_frozen_byte_for_byte_with_only_hash_metadata(self):
        original = b"\n" + TEST_CA_PEM + b"\n"
        self.bundle.write_bytes(original)
        metadata = trust.stage_bundle(self.bundle, self.destination)
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(metadata, {"enabled": True, "sha256": hashlib.sha256(original).hexdigest(),
                                    "certificate_count": 1})
        self.bundle.write_bytes(SYSTEM_CA_PEM)
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(trust.validate_bundle(self.destination), metadata)

    def test_multiple_and_duplicate_certificates_preserve_input_hash(self):
        data = TEST_CA_PEM + SYSTEM_CA_PEM + TEST_CA_PEM
        self.bundle.write_bytes(data)
        self.assertEqual(len(trust.parse_bundle(data)), 2)
        result = trust.stage_bundle(self.bundle, self.destination)
        self.assertEqual(result["certificate_count"], 2)
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.destination.read_bytes(), data)

    def test_private_keys_other_pem_and_noncertificate_content_are_rejected(self):
        for content in (
                b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
                b"-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n",
                b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
                b"SECRET=value\n", b"leading garbage\n" + TEST_CA_PEM,
                TEST_CA_PEM + b"trailing garbage\n",
                TEST_CA_PEM + b"\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"):
            with self.subTest(content=content[:30]):
                self.bundle.write_bytes(content)
                with self.assertRaises(ValueError):
                    trust.stage_bundle(self.bundle, self.destination)
                self.assertFalse(self.destination.exists())

    def test_base64_that_is_not_x509_is_rejected(self):
        self.bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n")
        with self.assertRaisesRegex(ValueError, "invalid CA"):
            trust.validate_bundle(self.bundle)

    def test_absent_option_is_distinct_from_invalid_empty_input(self):
        self.bundle.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            trust.stage_bundle(self.bundle, self.destination)
        metadata = trust.stage_bundle(None, self.destination)
        self.assertEqual(metadata, {"enabled": False, "sha256": hashlib.sha256(b"").hexdigest(),
                                    "certificate_count": 0})
        self.assertEqual(self.destination.read_bytes(), b"")
        self.assertEqual(trust.validate_bundle(self.destination, allow_empty=True), metadata)

    def test_staging_never_overwrites_a_file_or_follows_destination_symlink(self):
        existing = self.root / "existing"
        existing.write_bytes(b"preserve")
        self.destination.symlink_to(existing)
        with self.assertRaises(FileExistsError):
            trust.stage_bundle(self.bundle, self.destination)
        self.assertEqual(existing.read_bytes(), b"preserve")
        self.destination.unlink()
        self.destination.write_bytes(b"preserve staged")
        with self.assertRaises(FileExistsError):
            trust.stage_bundle(None, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"preserve staged")

    def test_invalid_oversized_and_directory_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "regular file"):
            trust.validate_bundle(self.root)
        with mock.patch.object(trust, "MAX_BUNDLE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "at most"):
                trust.validate_bundle(self.bundle)

    def test_stage_cli_outputs_metadata_without_certificate_contents(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = trust.main(["stage", "--source", str(self.bundle), "--output", str(self.destination)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), trust.validate_bundle(self.bundle))
        self.assertNotIn("CERTIFICATE", output.getvalue())
        self.assertEqual(self.destination.read_bytes(), TEST_CA_PEM)

    def test_stage_cli_splits_unique_ca_files_for_package_postinstall(self):
        self.bundle.write_bytes(TEST_CA_PEM + SYSTEM_CA_PEM + TEST_CA_PEM)
        directory = self.root / "certificates"
        output = io.StringIO()
        with redirect_stdout(output):
            result = trust.main(["stage", "--source", str(self.bundle), "--output", str(self.destination),
                                 "--certificates-output", str(directory)])
        self.assertEqual(result, 0)
        certificates = list(directory.glob("*.crt"))
        self.assertEqual(len(certificates), 2)
        self.assertEqual({path.read_bytes() for path in certificates}, {TEST_CA_PEM, SYSTEM_CA_PEM})
        for path in certificates:
            der = ssl.PEM_cert_to_DER_cert(path.read_text())
            self.assertEqual(path.stem, hashlib.sha256(der).hexdigest())
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertNotIn("CERTIFICATE", output.getvalue())

    def install(self):
        calls = []

        def run(argv):
            calls.append(argv)
            if str(argv[0]).endswith("update-ca-certificates"):
                anchors = list((self.worker / trust.CERTIFICATE_DIRECTORY).glob("*.crt"))
                self.assertEqual(len(anchors), 1)
                self.system_bundle.write_bytes(SYSTEM_CA_PEM + anchors[0].read_bytes())
            else:
                self.assertEqual(Path(argv[0]).name, "keytool")
                self.assertTrue(Path(argv[0]).stat().st_mode & stat.S_IXUSR)
                self.assertEqual(argv[1:4], ["-importcert", "-noprompt", "-trustcacerts"])

        with mock.patch.object(trust, "_run", side_effect=run):
            result = trust.install_bundle(self.bundle, self.bazel, self.worker)
        return result, calls

    def test_installed_bundle_retains_default_roots_and_configures_embedded_jvm(self):
        result, calls = self.install()
        directory = self.worker / trust.TRUST_DIRECTORY
        combined = directory / "ca-bundle.pem"
        self.assertEqual(combined.read_bytes(), SYSTEM_CA_PEM + TEST_CA_PEM)
        self.assertEqual(result["sha256"], hashlib.sha256(TEST_CA_PEM).hexdigest())
        self.assertEqual(result["installed_bundle_sha256"], hashlib.sha256(combined.read_bytes()).hexdigest())
        self.assertEqual(trust.validate_bundle(combined)["certificate_count"], 2)
        self.assertEqual((directory / "java-cacerts").read_bytes(), b"existing Java trust store")
        keytool = calls[1]
        self.assertEqual(keytool[keytool.index("-keystore") + 1], directory / "java-cacerts")
        self.assertEqual(keytool[keytool.index("-storepass") + 1], "changeit")
        self.assertIn("startup --host_jvm_args=-Djavax.net.ssl.trustStore=/usr/local/share/sonic-build-trust/java-cacerts",
                      (self.worker / "etc/bazel.bazelrc").read_text())
        receipt = (directory / "receipt.json").read_text()
        self.assertEqual(json.loads(receipt), result)
        self.assertNotIn("CERTIFICATE", receipt)
        self.assertEqual(combined.stat().st_mode & 0o777, 0o644)
        self.assertFalse(Path(keytool[0]).exists(), "temporary embedded JDK must be removed")

    def test_existing_bazel_configuration_is_preserved(self):
        rc = self.worker / "etc/bazel.bazelrc"
        rc.write_text("startup --max_idle_secs=60\n")
        self.install()
        self.assertTrue(rc.read_text().startswith("startup --max_idle_secs=60\n"))

    def test_disabled_option_does_not_change_system_or_java_trust(self):
        self.bundle.write_bytes(b"")
        with mock.patch.object(trust, "_run") as run:
            result = trust.install_bundle(self.bundle, self.bazel, self.worker)
        run.assert_not_called()
        self.assertFalse(result["enabled"])
        self.assertEqual(self.system_bundle.read_bytes(), SYSTEM_CA_PEM)
        directory = self.worker / trust.TRUST_DIRECTORY
        self.assertEqual(json.loads((directory / "receipt.json").read_text()), result)
        self.assertFalse((directory / "ca-bundle.pem").exists())
        self.assertFalse((directory / "java-cacerts").exists())
        self.assertFalse((self.worker / "etc/bazel.bazelrc").exists())

    def test_invalid_input_does_not_mutate_worker(self):
        self.bundle.write_bytes(b"not a certificate")
        with mock.patch.object(trust, "_run") as run, self.assertRaises(ValueError):
            trust.install_bundle(self.bundle, self.bazel, self.worker)
        run.assert_not_called()
        self.assertFalse((self.worker / trust.TRUST_DIRECTORY).exists())
        self.assertEqual(self.system_bundle.read_bytes(), SYSTEM_CA_PEM)

    def test_embedded_jdk_path_escape_and_symlink_are_rejected(self):
        for name, mode in (("embedded_tools/jdk/../../../escape", 0o100644),
                           ("embedded_tools/jdk/link", 0o120777)):
            with self.subTest(name=name):
                self.write_jdk((name, mode))
                with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                    trust._extract_jdk(self.bazel, Path(directory))
        self.assertFalse((self.root / "escape").exists())

    def test_tool_failure_does_not_write_a_success_receipt(self):
        with mock.patch.object(trust, "_run", side_effect=RuntimeError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                trust.install_bundle(self.bundle, self.bazel, self.worker)
        self.assertFalse((self.worker / trust.TRUST_DIRECTORY / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
