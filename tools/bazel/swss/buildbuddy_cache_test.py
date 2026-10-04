#!/usr/bin/env python3
"""Check the credential-helper boundary without contacting a remote cache."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import buildbuddy_cache


class BuildBuddyCacheTest(unittest.TestCase):
    def setUp(self):
        self.key = "test-only-buildbuddy-key"
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.secret = Path(temporary.name) / "key"
        self.secret.write_text(self.key)
        self.secret.chmod(0o600)
        self.environment = {"BUILDBUDDY_API_KEY_FILE": str(self.secret)}

    def helper(self, request, environment=None):
        return subprocess.run(
            [str(Path(buildbuddy_cache.__file__).resolve()), "get"],
            input=request, capture_output=True, text=True,
            env={"PATH": os.environ["PATH"], **(environment or self.environment)},
        )

    def test_get_protocol_uses_private_file_and_only_expected_header(self):
        result = self.helper(json.dumps({"uri": "https://remote.buildbuddy.io/cache.Service"}))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout),
                         {"headers": {"x-buildbuddy-api-key": [self.key]}})
        self.assertEqual(result.stderr, "")
        self.assertNotIn(self.key, " ".join(result.args))
        self.assertNotIn("BUILDBUDDY_API_KEY", self.environment)

    def test_rejects_other_hosts_ports_insecure_urls_and_malformed_input(self):
        requests = [
            {"uri": "https://evil.example/" + self.key},
            {"uri": "https://remote.buildbuddy.io.evil.example"},
            {"uri": "https://sub.remote.buildbuddy.io"},
            {"uri": "https://remote.buildbuddy.io:444"},
            {"uri": "http://remote.buildbuddy.io"},
            {"uri": "https://" + self.key + "@remote.buildbuddy.io"},
            {"uri": "https://remote.buildbuddy.io:bad"},
            {"uri": None}, [], None,
        ]
        for request in requests:
            with self.subTest(request=request):
                result = self.helper(json.dumps(request))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(self.key, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        result = self.helper("invalid JSON " + self.key)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.key, result.stdout + result.stderr)

    def test_missing_key_fails_without_returning_credentials(self):
        result = self.helper('{"uri":"https://remote.buildbuddy.io"}',
                             {"BUILDBUDDY_API_KEY_FILE": str(self.secret) + ".missing"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_shared_key_file_is_rejected_without_leaking_the_key(self):
        self.secret.chmod(0o644)
        result = self.helper('{"uri":"https://remote.buildbuddy.io"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(self.key, result.stderr)

    def test_custom_endpoint_scopes_helper_to_exact_host(self):
        self.environment["BUILDBUDDY_CACHE_ENDPOINT"] = "grpcs://cache.example:8443"
        result = self.helper('{"uri":"https://cache.example:8443/service"}')
        self.assertEqual(result.returncode, 0)
        settings = buildbuddy_cache.configuration(self.environment)
        self.assertIn("--remote_cache=grpcs://cache.example:8443", settings)
        self.assertIn("--credential_helper=cache.example=%workspace%/", settings)
        self.assertNotIn(self.key, settings)
        self.assertNotIn("remote_header", settings)
        self.assertNotIn("action_env", settings)

    def test_endpoint_cannot_insert_credentials_or_additional_options(self):
        for endpoint in ("http://cache.example", "grpcs://key@cache.example",
                         "grpcs://cache.example/path", "grpcs://cache.example?key=value",
                         "grpcs://cache.example\nbuild --remote_header=secret",
                         "grpcs://*.example", "grpcs://cache.example:99999"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                buildbuddy_cache.configuration({"BUILDBUDDY_CACHE_ENDPOINT": endpoint})

    def test_configure_records_state_without_storing_the_key(self):
        self.secret.unlink()
        self.environment["BUILDBUDDY_API_KEY"] = self.key
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.environment["GITHUB_STEP_SUMMARY"] = str(root / "summary")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                buildbuddy_cache.configure(self.environment, root)
            settings = (root / ".bazelrc.user").read_text()
            summary = (root / "summary").read_text()
            self.assertIn("configured for grpcs://remote.buildbuddy.io", summary)
            self.assertNotIn(self.key, settings + summary + output.getvalue())
            self.assertEqual(self.secret.read_text(), self.key)
            self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                buildbuddy_cache.configure(self.environment, root)

    def test_configure_rejects_a_key_file_inside_the_workspace(self):
        self.environment["BUILDBUDDY_API_KEY"] = self.key
        self.secret.unlink()
        with self.assertRaises(ValueError):
            buildbuddy_cache.configure(self.environment, self.secret.parent)
        self.assertFalse(self.secret.exists())

    def test_missing_secret_reports_local_cache_and_creates_no_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                buildbuddy_cache.configure({}, root)
            self.assertFalse((root / ".bazelrc.user").exists())
            self.assertIn("not configured", output.getvalue())


if __name__ == "__main__":
    unittest.main()
