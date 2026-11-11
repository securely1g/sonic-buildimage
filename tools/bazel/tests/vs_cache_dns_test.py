"""Pure DNS policy fixtures: resolver/process calls are mocked, no network."""
import io
from contextlib import redirect_stderr, redirect_stdout
import errno
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import vs_cache_dns as dns


class CacheDNSDiagnosticTest(unittest.TestCase):
    def selected(self, environment):
        with mock.patch.dict(os.environ, environment, clear=True):
            return dns.selected_hosts()

    def detail(self, proxy=False, bypass=False):
        roles = ["cache", "github_control"]
        if proxy and not bypass:
            roles.insert(1, "proxy")
        return {"schema": 1, "proxy_configured": proxy, "proxy_bypassed": bypass,
                "results": [{"role": role, "status": "success"} for role in roles]}

    def test_no_proxy_selects_only_two_fixed_hosts(self):
        hosts, flags = self.selected({})
        self.assertEqual(hosts, [("cache", dns.CACHE_HOST), ("github_control", "api.github.com")])
        self.assertEqual(flags, {"proxy_configured": False, "proxy_bypassed": False})

    def test_lowercase_https_proxy_wins_and_userinfo_is_not_a_host(self):
        hosts, flags = self.selected({"HTTPS_PROXY": "http://wrong.invalid:80",
                                     "https_proxy": "http://user:private@chosen.invalid:8080"})
        self.assertEqual(hosts[1], ("proxy", "chosen.invalid"))
        self.assertEqual(flags, {"proxy_configured": True, "proxy_bypassed": False})

    def test_empty_lowercase_removes_uppercase_proxy(self):
        hosts, flags = self.selected({"HTTPS_PROXY": "http://unused.invalid", "https_proxy": ""})
        self.assertEqual(len(hosts), 2)
        self.assertFalse(flags["proxy_configured"])

    def test_exact_and_suffix_no_proxy_bypass_effective_https_proxy(self):
        for value in (dns.CACHE_HOST, ".eastus.cloudapp.azure.com", "*"):
            with self.subTest(value=value):
                hosts, flags = self.selected({"HTTPS_PROXY": "http://unused.invalid", "NO_PROXY": value})
                self.assertEqual(len(hosts), 2)
                self.assertTrue(flags["proxy_configured"])
                self.assertTrue(flags["proxy_bypassed"])

    def test_unrelated_no_proxy_does_not_bypass(self):
        hosts, flags = self.selected({"https_proxy": "proxy.invalid:3128", "no_proxy": "github.com"})
        self.assertEqual(hosts[1], ("proxy", "proxy.invalid"))
        self.assertFalse(flags["proxy_bypassed"])

    def test_http_and_all_proxy_do_not_select_an_https_proxy(self):
        hosts, flags = self.selected({"HTTP_PROXY": "http://unused.invalid", "ALL_PROXY": "http://also-unused.invalid"})
        self.assertEqual(len(hosts), 2)
        self.assertFalse(flags["proxy_configured"])

    def test_percent_decoded_and_bracketed_proxy_authorities(self):
        for value, expected in (("http://user:secret@proxy%2einvalid:3128", "proxy.invalid"),
                                ("http://[2001:db8::1]:3128", "2001:db8::1")):
            with self.subTest(value=value):
                hosts, flags = self.selected({"HTTPS_PROXY": value})
                self.assertEqual(hosts[1], ("proxy", expected))
                self.assertFalse(flags["proxy_bypassed"])

    def test_invalid_proxy_authority_stops_before_resolution(self):
        for value in ("http:/private-invalid", "http://", "http://proxy.invalid:99999"):
            with self.subTest(value=value), mock.patch.object(dns.socket, "getaddrinfo") as lookup:
                with self.assertRaises(ValueError):
                    self.selected({"HTTPS_PROXY": value})
                lookup.assert_not_called()

    def test_success_never_returns_addresses_or_hostname(self):
        with mock.patch.object(dns.socket, "getaddrinfo", return_value=[(2, 1, 6, "private", ("192.0.2.17", 0))]) as lookup:
            value = dns.resolve_role("cache", "private-host.invalid")
        self.assertEqual(value, {"role": "cache", "status": "success"})
        lookup.assert_called_once_with("private-host.invalid", None, type=socket.SOCK_STREAM)

    def test_dns_failure_projects_only_allowlisted_class_and_integer_errno(self):
        error = socket.gaierror(-2, "private-host.invalid proxy-user:password 192.0.2.18")
        with mock.patch.object(dns.socket, "getaddrinfo", side_effect=error):
            value = dns.resolve_role("proxy", "private-host.invalid")
        self.assertEqual(value, {"role": "proxy", "status": "failure", "error_type": "gaierror", "errno": -2})
        self.assertNotIn("private", json.dumps(value))

    def test_os_and_unicode_failures_are_sanitized(self):
        for error, kind in ((TimeoutError(errno.ETIMEDOUT, "private"), "TimeoutError"),
                            (OSError(errno.EACCES, "private"), "OSError"),
                            (UnicodeError("private"), "UnicodeError")):
            with self.subTest(kind=kind), mock.patch.object(dns.socket, "getaddrinfo", side_effect=error):
                value = dns.resolve_role("cache", "unused")
                self.assertEqual(value["error_type"], kind)
                self.assertNotIn("private", json.dumps(value))

    def test_collect_uses_at_most_three_role_queries_without_auth(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:password@private.invalid"}, clear=True), \
                mock.patch.object(dns.socket, "getaddrinfo", return_value=[object()]) as lookup:
            value = dns.collect_dns()
        self.assertEqual(lookup.call_count, 3)
        self.assertEqual([row["role"] for row in value["results"]], ["cache", "proxy", "github_control"])
        self.assertEqual(dns.validate_dns(value), value)
        for private in ("private.invalid", "password", "user", "192.0.2.1"):
            self.assertNotIn(private, json.dumps(value))

    def test_auth_presence_is_rejected_before_any_dns(self):
        with mock.patch.dict(os.environ, {"BAZEL_REMOTE_AUTH": "private"}, clear=True), \
                mock.patch.object(dns.socket, "getaddrinfo") as lookup:
            with self.assertRaises(dns.transport.DiagnosticFailure):
                dns.collect_dns()
            lookup.assert_not_called()

    def test_child_environment_only_removes_auth_from_existing_transport_environment(self):
        original = {"HTTPS_PROXY": "http://private.invalid", "NO_PROXY": "keep.invalid", "BAZEL_REMOTE_AUTH": "private",
                    "SSL_CERT_FILE": "/fixed.pem", "PATH": "/fixed/bin"}
        with mock.patch.object(dns.transport, "child_environment", return_value=dict(original)) as inherited:
            value = dns.child_environment(object(), {"unused": "value"}, Path("/fixed"))
        self.assertEqual(value, {key: val for key, val in original.items() if key != "BAZEL_REMOTE_AUTH"})
        inherited.assert_called_once()

    def test_public_schema_rejects_address_hostname_or_extra_fields(self):
        for key in ("host", "address", "url", "credential", "message"):
            value = self.detail(); value["results"][0][key] = "private"
            with self.subTest(key=key), self.assertRaises(dns.transport.DiagnosticFailure):
                dns.validate_dns(value)

    def test_public_schema_rejects_missing_reordered_or_duplicate_roles(self):
        values = []
        value = self.detail(True); value["results"].pop(); values.append(value)
        value = self.detail(True); value["results"].reverse(); values.append(value)
        value = self.detail(True); value["results"][1]["role"] = "cache"; values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(dns.transport.DiagnosticFailure):
                dns.validate_dns(value)

    def test_public_schema_rejects_bad_flags_errno_and_error_type(self):
        values = []
        value = self.detail(); value["proxy_bypassed"] = True; values.append(value)
        value = self.detail(); value["schema"] = True; values.append(value)
        for field, wrong in (("errno", True), ("errno", 2**40), ("error_type", "private reason")):
            value = self.detail(); value["results"][0].update(status="failure", error_type="gaierror", errno=-2)
            value["results"][0][field] = wrong; values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(dns.transport.DiagnosticFailure):
                dns.validate_dns(value)

    def test_child_retains_only_validated_detail_in_fresh_owned_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); private = parent / "cache-dns-1-1"; private.mkdir(mode=0o700)
            output = private / "dns-detail.json"
            args = SimpleNamespace(detail_output=output)
            smoke = SimpleNamespace(check_identity=lambda env: {"run": 1, "attempt": 1})
            with mock.patch.dict(os.environ, {"RUNNER_TEMP": temporary}, clear=True), \
                    mock.patch.object(dns, "collect_dns", return_value=self.detail()):
                self.assertEqual(dns.child_main(args, smoke), 0)
                self.assertEqual(json.loads(output.read_text()), self.detail())
                with self.assertRaises(dns.transport.DiagnosticFailure):
                    dns.child_main(args, smoke)


    def test_child_private_exception_text_is_never_printed(self):
        credentials = SimpleNamespace(HOST=dns.CACHE_HOST, ENDPOINT="https://" + dns.CACHE_HOST)
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(dns.sys, "argv", ["dns", "--native-root", "/unused", "--child", "--detail-output", "/unused/detail.json"]), \
                mock.patch.object(dns.transport, "load_native", return_value=(object(), credentials)), \
                mock.patch.object(dns, "child_main", side_effect=ValueError("http://secret:password@private.invalid")), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(dns.main(), 1)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_parent_requires_owned_child_closure_and_uses_existing_45_second_driver(self):
        for closed in (True, False):
            with self.subTest(closed=closed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); private = root / "cache-dns-1-1"; private.mkdir(mode=0o700)
                output = root / "public.json"
                def run(directory, argv, environment, *, cwd, seconds, record):
                    self.assertEqual(directory, private)
                    self.assertEqual(seconds, 45)
                    self.assertNotIn("BAZEL_REMOTE_AUTH", environment)
                    (private / "dns-detail.json").write_text(json.dumps(self.detail()))
                    return {"returncode": 0, "reason": None, "leader_reaped": True,
                            "group_closed": closed, "client_invocations": 1, "stdout_bytes": 0, "stderr_bytes": 0}
                smoke = SimpleNamespace(check_identity=lambda env: {"run": 1, "attempt": 1}, run_command=run)
                credentials = SimpleNamespace(HOST=dns.CACHE_HOST, ENDPOINT="https://" + dns.CACHE_HOST,
                                              probe=mock.Mock(side_effect=AssertionError("No probe allowed")))
                with mock.patch.object(dns.sys, "argv", ["dns", "--native-root", str(root), "--output", str(output)]), \
                        mock.patch.dict(os.environ, {"RUNNER_TEMP": temporary}, clear=True), \
                        mock.patch.object(dns.transport, "load_native", return_value=(smoke, credentials)), \
                        mock.patch.object(dns, "private_root", return_value=private), \
                        mock.patch.object(dns, "child_environment", return_value={"PATH": "/fixed"}):
                    self.assertEqual(dns.main(), 0 if closed else 1)
                value = json.loads(output.read_text())
                self.assertFalse(value["cache_qualified"])
                self.assertEqual(value["HTTP_requests"], 0)
                self.assertEqual("dns" in value, closed)
                credentials.probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
