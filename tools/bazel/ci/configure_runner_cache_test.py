#!/usr/bin/env python3
"""Behavioral checks for sanitized runner cache setup and local fallback."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request


SPEC = importlib.util.spec_from_file_location(
    "configure_runner_cache", Path(__file__).with_name("configure_runner_cache.py"))
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)
SECRET = "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="
ENDPOINT = "https://cache.example.invalid/"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redacted", None, None)


def credentials():
    return types.SimpleNamespace(ENDPOINT=ENDPOINT, NoRedirects=NoRedirects,
                                 authorization=mock.Mock(return_value=SECRET))


class RemoteClassificationTests(unittest.TestCase):
    def test_success_uses_only_reviewed_origin_header_and_bounded_timeout(self):
        helper = credentials()
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        with mock.patch.object(cache.urllib.request, "build_opener", return_value=opener) as factory:
            result = cache.check_remote(helper, SECRET)
        self.assertEqual(result, {"mode": "local_and_remote", "error_class": None, "http_status": 200})
        self.assertIsInstance(factory.call_args.args[0], NoRedirects)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT + "status")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), SECRET)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 8})
        opener.open.return_value.__enter__.return_value.read.assert_not_called()

    def test_http_errors_only_allow_server_unavailability_to_fallback(self):
        for status, expected in [(500, "remote_unavailable"), (503, "remote_unavailable"),
                                 (599, "remote_unavailable"), (401, "authentication_rejected"),
                                 (403, "authentication_rejected"), (301, "redirect_refused"),
                                 (302, "redirect_refused"), (307, "redirect_refused"),
                                 (308, "redirect_refused"), (404, "unexpected_http_status"),
                                 (429, "unexpected_http_status")]:
            with self.subTest(status=status):
                opener = mock.Mock()
                opener.open.side_effect = urllib.error.HTTPError(
                    ENDPOINT, status, SECRET, None, None)
                with mock.patch.object(cache.urllib.request, "build_opener", return_value=opener):
                    if status >= 500:
                        result = cache.check_remote(credentials(), SECRET)
                        self.assertEqual(result, {"mode": "local_only", "error_class": expected,
                                                  "http_status": status})
                    else:
                        with self.assertRaises(cache.ConfigurationFailure) as caught:
                            cache.check_remote(credentials(), SECRET)
                        self.assertEqual(caught.exception.error_class, expected)
                        self.assertEqual(caught.exception.http_status, status)
                        self.assertNotIn(SECRET, str(caught.exception))

    def test_other_success_status_is_not_accepted_as_cache_access(self):
        with self.assertRaises(cache.ConfigurationFailure) as caught:
            cache.response_mode(204)
        self.assertEqual(caught.exception.error_class, "unexpected_http_status")

    def test_transport_and_tls_failures_fallback_without_exception_text(self):
        cases = [(ssl.SSLError(SECRET), "tls_error"),
                 (urllib.error.URLError(ssl.SSLError(SECRET)), "tls_error"),
                 (urllib.error.URLError(ConnectionResetError(SECRET)), "transport_error"),
                 (TimeoutError(SECRET), "transport_error")]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                opener = mock.Mock()
                opener.open.side_effect = error
                with mock.patch.object(cache.urllib.request, "build_opener", return_value=opener):
                    result = cache.check_remote(credentials(), SECRET)
                self.assertEqual(result, {"mode": "local_only", "error_class": expected, "http_status": None})
                self.assertNotIn(SECRET, json.dumps(result))


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rc = self.root / ".bazelrc"
        self.output = self.root / "artifacts/vs/cache-configuration.json"
        self.cache_dir = self.root / "cache"
        self.original = "startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts\n"
        self.rc.write_text(self.original)
        self.helper = credentials()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(cache, "load_credentials", return_value=self.helper).start()

    def run_main(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cache.main(["--cache-dir", str(self.cache_dir), "--rc", str(self.rc),
                               "--output", str(self.output)])
        return code, json.loads(self.output.read_text()), stdout.getvalue()

    def install_fake_runtime(self):
        # A real child proves secret forwarding through the environment and
        # suppression of helper output, while its persisted RC stays public.
        runtime = self.root / "runtime"
        runtime.mkdir()
        (runtime / "cache_probe.py").write_text(
            "import argparse, os, pathlib, sys\n"
            "p=argparse.ArgumentParser(); p.add_argument('command'); p.add_argument('--cache-dir'); p.add_argument('--rc'); a=p.parse_args()\n"
            "assert a.command == 'configure'\n"
            "assert os.environ['BAZEL_REMOTE_AUTH'].startswith('Basic ')\n"
            "print(os.environ['BAZEL_REMOTE_AUTH']); print(os.environ['BAZEL_REMOTE_AUTH'], file=sys.stderr)\n"
            "root=pathlib.Path(a.cache_dir); (root/'disk').mkdir(parents=True, exist_ok=True); (root/'repository').mkdir(exist_ok=True)\n"
            "rc=pathlib.Path(a.rc)\n"
            "with rc.open('a') as f:\n"
            " f.write('common --disk_cache='+str(root/'disk')+'\\ncommon --repository_cache='+str(root/'repository')+'\\ncommon --remote_cache=https://cache.example.invalid/\\n')\n")
        mock.patch.object(cache, "RUNTIME_ROOT", runtime).start()
        mock.patch.dict(os.environ, {"BAZEL_REMOTE_AUTH": SECRET}).start()

    def test_real_child_receives_secret_but_local_fallback_persists_only_public_settings(self):
        self.install_fake_runtime()
        mock.patch.object(cache, "check_remote", return_value={
            "mode": "local_only", "error_class": "tls_error", "http_status": None}).start()
        code, receipt, stdout = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(receipt, {"status": "configured", "mode": "local_only",
                                   "error_class": "tls_error", "http_status": None})
        self.assertEqual(json.loads(stdout), receipt)
        contents = self.rc.read_text()
        self.assertTrue(contents.startswith(self.original))
        self.assertIn("common --disk_cache=" + str(self.cache_dir / "disk"), contents)
        self.assertIn("common --repository_cache=" + str(self.cache_dir / "repository"), contents)
        self.assertGreater(contents.index("common --remote_cache=\n"),
                           contents.index("common --remote_cache=https://"))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text())
        self.assertNotIn(SECRET, stdout)

    def test_remote_mode_removes_previous_local_override_and_preserves_trust(self):
        self.install_fake_runtime()
        cache.set_local_fallback(self.rc, True)
        mock.patch.object(cache, "check_remote", return_value={
            "mode": "local_and_remote", "error_class": None, "http_status": 200}).start()
        code, receipt, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(receipt["mode"], "local_and_remote")
        contents = self.rc.read_text()
        self.assertNotIn(cache.FALLBACK_BEGIN, contents)
        self.assertNotIn("common --remote_cache=\n", contents)
        self.assertTrue(contents.startswith(self.original))

    def test_missing_or_malformed_secret_fails_before_network_or_configure(self):
        self.helper.authorization.side_effect = ValueError(SECRET)
        with mock.patch.object(cache, "check_remote") as remote, mock.patch.object(cache.subprocess, "run") as child:
            code, receipt, stdout = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(receipt["error_class"], "credentials_invalid")
        remote.assert_not_called()
        child.assert_not_called()
        self.assertEqual(self.rc.read_text(), self.original)
        self.assertNotIn(SECRET, stdout + self.output.read_text())

    def test_auth_failure_is_fatal_and_never_reconfigures(self):
        mock.patch.object(cache, "check_remote", side_effect=cache.ConfigurationFailure(
            "authentication_rejected", 403)).start()
        with mock.patch.object(cache.subprocess, "run") as child:
            code, receipt, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(receipt, {"status": "failure", "mode": "unconfigured",
                                   "error_class": "authentication_rejected", "http_status": 403})
        child.assert_not_called()
        self.assertEqual(self.rc.read_text(), self.original)

    def test_helper_failure_is_sanitized_and_arguments_have_no_secret(self):
        mock.patch.object(cache, "check_remote", return_value={
            "mode": "local_and_remote", "error_class": None, "http_status": 200}).start()
        error = subprocess.CalledProcessError(1, ["helper"], output=SECRET, stderr=SECRET)
        with mock.patch.object(cache.subprocess, "run", side_effect=error) as child:
            code, receipt, stdout = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(receipt["error_class"], "configuration_failed")
        self.assertEqual(child.call_args.args[0][1:], ["/opt/sonic-runner/cache_probe.py", "configure",
                         "--cache-dir", str(self.cache_dir), "--rc", str(self.rc)])
        self.assertEqual(child.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(child.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("env", child.call_args.kwargs)
        self.assertNotIn(SECRET, repr(child.call_args) + stdout + self.output.read_text())

    def test_fallback_is_idempotent_and_rejects_broken_markers(self):
        cache.set_local_fallback(self.rc, True)
        first = self.rc.read_text()
        cache.set_local_fallback(self.rc, True)
        self.assertEqual(self.rc.read_text(), first)
        self.rc.write_text(self.original + cache.FALLBACK_BEGIN)
        with self.assertRaises(cache.ConfigurationFailure):
            cache.set_local_fallback(self.rc, True)


if __name__ == "__main__":
    unittest.main()
