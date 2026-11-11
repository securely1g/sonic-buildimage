"""Pure transport evidence fixtures; no real helper, credential or network calls."""
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import types
import unittest
from unittest import mock
import urllib.error

from tools.bazel.ci import vs_cache_transport as subject

class TransportDiagnosticTest(unittest.TestCase):
    def result(self, code=1):
        return {"returncode": code, "reason": None, "leader_reaped": True,
                "group_closed": True, "client_invocations": 1, "stdout_bytes": 0}

    def failed(self):
        return {"endpoint": "https://fixed.example", "authenticated": False, "error_type": "URLError"}

    def detail(self):
        return {"schema": 1, "probe_returncode": 1, "transport": {"reason_type": "gaierror", "errno": -2}}

    def test_dns_numeric_reason_without_text(self):
        error = urllib.error.URLError(socket.gaierror(-2, "private proxy and credential text"))
        self.assertEqual(subject.transport_reason(error), {"reason_type": "gaierror", "errno": -2})

    def test_certificate_verification_code_without_message(self):
        reason = ssl.SSLCertVerificationError(1, "private certificate URL")
        reason.verify_code = 20
        self.assertEqual(subject.transport_reason(urllib.error.URLError(reason)),
                         {"reason_type": "SSLCertVerificationError", "errno": 1, "ssl_verification_code": 20})

    def test_connection_reason_without_url(self):
        reason = ConnectionRefusedError(111, "https://user:secret@example.invalid")
        self.assertEqual(subject.transport_reason(urllib.error.URLError(reason)),
                         {"reason_type": "ConnectionRefusedError", "errno": 111})

    def test_text_and_unknown_reason_are_not_serialized(self):
        self.assertEqual(subject.transport_reason(urllib.error.URLError("https://user:secret@proxy")),
                         {"reason_type": "str"})
        class PrivateReason:
            def __str__(self):
                raise AssertionError("Exception text must not be inspected")
        self.assertEqual(subject.transport_reason(urllib.error.URLError(PrivateReason())),
                         {"reason_type": "unclassified"})

    def test_http_error_is_separate_from_transport_reason(self):
        error = urllib.error.HTTPError("https://private.example", 403, "private text", {}, None)
        self.assertIsNone(subject.transport_reason(error))
        self.assertIsNone(subject.transport_reason(ValueError("private text")))

    def test_probe_api_is_called_once_and_original_failure_receipt_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "original.json"
            detail = Path(directory) / "detail.json"
            original = b'{"authenticated":false,"error_type":"URLError","endpoint":"https://fixed.example"}\n'
            def failure(path):
                path.write_bytes(original)
                raise urllib.error.URLError(socket.gaierror(-2, "private data"))
            probe = mock.Mock(side_effect=failure)
            with mock.patch.object(subject.sys, "stderr", io.StringIO()) as stream:
                code = subject.call_existing_probe(types.SimpleNamespace(probe=probe), output, detail)
            self.assertEqual(code, 1)
            probe.assert_called_once_with(output)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(stream.getvalue(), "Bazel remote cache access failed.\n")
            self.assertEqual(json.loads(detail.read_text()), self.detail())

    def test_successful_probe_remains_success_without_transport_details(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "original.json"
            detail = Path(directory) / "detail.json"
            def success(path):
                path.write_text('{"authenticated":true,"status":200,"endpoint":"https://fixed.example"}')
            probe = mock.Mock(side_effect=success)
            self.assertEqual(subject.call_existing_probe(types.SimpleNamespace(probe=probe), output, detail), 0)
            probe.assert_called_once_with(output)
            self.assertEqual(json.loads(detail.read_text()), {"schema": 1, "probe_returncode": 0, "transport": None})

    def test_failed_probe_stays_false_during_diagnostic_projection(self):
        access = self.failed()
        before = dict(access)
        self.assertEqual(subject.validate_detail(self.detail(), access, self.result()),
                         {"reason_type": "gaierror", "errno": -2})
        self.assertEqual(access, before)
        self.assertFalse(access["authenticated"])

    def test_unclosed_interrupted_or_extra_child_is_rejected(self):
        for field, value in (("group_closed", False), ("leader_reaped", False),
                             ("reason", "CLIENT_DEADLINE"), ("client_invocations", 2),
                             ("stdout_bytes", 1), ("returncode", 0)):
            with self.subTest(field=field):
                result = self.result(); result[field] = value
                with self.assertRaises(subject.DiagnosticFailure):
                    subject.validate_detail(self.detail(), self.failed(), result)

    def test_unknown_fields_boolean_codes_and_arbitrary_class_are_rejected(self):
        for transport in ({"reason_type": "gaierror", "message": "secret"},
                          {"reason_type": "gaierror", "errno": True},
                          {"reason_type": "arbitrary_secret"},
                          {"reason_type": "gaierror", "ssl_verification_code": 20},
                          {"reason_type": "SSLCertVerificationError", "ssl_verification_code": True}):
            with self.subTest(transport=transport):
                detail = self.detail(); detail["transport"] = transport
                with self.assertRaises(subject.DiagnosticFailure):
                    subject.validate_detail(detail, self.failed(), self.result())
        detail = self.detail(); detail["schema"] = True
        with self.assertRaises(subject.DiagnosticFailure):
            subject.validate_detail(detail, self.failed(), self.result())

    def test_sanitized_environment_keeps_managed_transport_without_other_secrets(self):
        inherited = {key: "identity" for key in subject.IDENTITY_ENV}
        inherited.update(BAZEL_REMOTE_AUTH="Basic private", HTTPS_PROXY="https://private-proxy",
                         GH_TOKEN="do-not-forward", BASH_ENV="do-not-forward")
        def existing(env, directory, version):
            return {"BAZEL_REMOTE_AUTH": env["BAZEL_REMOTE_AUTH"], "HTTPS_PROXY": env["HTTPS_PROXY"],
                    "USE_BAZEL_VERSION": version, "BAZELISK_HOME": str(directory)}
        result = subject.child_environment(types.SimpleNamespace(version_environment=existing), inherited, Path("/private"))
        self.assertEqual(result["BAZEL_REMOTE_AUTH"], inherited["BAZEL_REMOTE_AUTH"])
        self.assertEqual(result["HTTPS_PROXY"], inherited["HTTPS_PROXY"])
        self.assertNotIn("GH_TOKEN", result); self.assertNotIn("BASH_ENV", result)
        self.assertNotIn("USE_BAZEL_VERSION", result); self.assertNotIn("BAZELISK_HOME", result)
        self.assertEqual({key: result[key] for key in subject.IDENTITY_ENV},
                         {key: inherited[key] for key in subject.IDENTITY_ENV})

    def test_original_file_reader_refuses_links_and_oversized_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value").write_bytes(b"abc")
            (root / "link").symlink_to("value")
            self.assertEqual(subject.read_under(root, "value", 3), b"abc")
            with self.assertRaises((OSError, subject.DiagnosticFailure)):
                subject.read_under(root, "link", 3)
            with self.assertRaises(subject.DiagnosticFailure):
                subject.read_under(root, "value", 2)

    def test_duplicate_or_nonfinite_detail_json_is_rejected(self):
        for value in ('{"schema":1,"schema":1}', '{"errno":NaN}'):
            with self.assertRaises(ValueError):
                subject.json_decoder().decode(value)

    def test_existing_detail_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "original.json"
            detail = Path(directory) / "detail.json"
            detail.write_bytes(b"retained evidence")
            probe = mock.Mock()
            with self.assertRaises(subject.DiagnosticFailure):
                subject.call_existing_probe(types.SimpleNamespace(probe=probe), output, detail)
            self.assertEqual(detail.read_bytes(), b"retained evidence")
            probe.assert_not_called()

    def test_prior_api_intent_stops_before_another_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "original.json"
            detail = Path(directory) / "detail.json"
            (Path(directory) / "probe-api-intent.json").write_bytes(b"original intent")
            probe = mock.Mock()
            with self.assertRaises(FileExistsError):
                subject.call_existing_probe(types.SimpleNamespace(probe=probe), output, detail)
            probe.assert_not_called()

if __name__ == "__main__":
    unittest.main()
