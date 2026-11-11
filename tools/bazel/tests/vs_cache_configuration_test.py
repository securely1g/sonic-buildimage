"""Pure bounded cache configuration fixtures; no network or project builds."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import vs_cache_configuration as config


class CacheConfigurationTest(unittest.TestCase):
    def test_origin_removes_all_private_url_parts(self):
        actual = config.origin("https://user:private-token@CACHE.example:443/private-path?token=private-query#private-fragment")
        self.assertEqual(actual, {"status": "origin", "origin": "https://cache.example:443"})
        self.assertNotIn("private", json.dumps(actual))

    def test_schemes_ports_and_ipv6_are_literal_candidates(self):
        for value in ("http://cache.example", "https://cache.example:443", "grpc://cache.example:9092", "grpcs://[::1]:443"):
            with self.subTest(value=value):
                self.assertEqual(config.origin(value), {"status": "origin", "origin": value})

    def test_invalid_and_disabled_values_do_not_leak(self):
        self.assertEqual(config.origin(""), {"status": "disabled"})
        self.assertEqual(config.origin("x" * (config.VALUE_LIMIT + 1)), {"status": "value_too_large"})
        for value in ("https://cache.example\nsecret", "file:///secret", "https://", "https://cache:0", "https://cache:99999", "https://${TOKEN}", "https://%73ecret"):
            with self.subTest(value=value):
                self.assertEqual(config.origin(value), {"status": "invalid_value"})

    def test_environment_is_fixed_and_does_not_access_auth(self):
        class Selected(dict):
            def __getitem__(self, key):
                if key not in config.URL_ENV_NAMES:
                    raise AssertionError("Unrelated variable accessed")
                return super().__getitem__(key)
        values = Selected(BAZEL_REMOTE_CACHE_URL="https://private:secret@cache/a?token=x", BAZEL_REMOTE_AUTH="never-read", OTHER_URL="https://other")
        rows = config.environment_candidates(values)
        self.assertEqual([row["env_name"] for row in rows], list(config.URL_ENV_NAMES))
        self.assertEqual(rows[1], {"env_name": "BAZEL_REMOTE_CACHE_URL", "status": "origin", "origin": "https://cache"})
        self.assertEqual([row["status"] for row in rows], ["absent", "origin", "absent", "absent"])

    def test_rc_equals_spaced_quotes_and_continuation(self):
        raw = b'common --remote_cache="https://user:secret@cache/a?secret=x" # ignored\nbuild:remote --remote_cache \\\n "grpcs://cache:443/private"\n'
        row = config.rc_options(raw)
        self.assertEqual(row["options"], [{"status": "origin", "origin": "https://cache"}, {"status": "origin", "origin": "grpcs://cache:443"}])
        self.assertFalse(row["complex_remote_cache_lines_not_interpreted"])

    def test_unrelated_flags_imports_and_complex_lines_not_guessed(self):
        raw = b'import /private/secret.bazelrc\ntry-import %workspace%/secret\ncommon --remote_header "--remote_cache=https://secret.invalid"\ncommon --remote_cache=https://cache --remote_header=private\n# common --remote_cache=https://comment\n'
        row = config.rc_options(raw)
        self.assertEqual(row["options"], [])
        self.assertTrue(row["import_directives_ignored"])
        self.assertTrue(row["complex_remote_cache_lines_not_interpreted"])
        self.assertNotIn("secret", json.dumps(row))

    def test_rc_empty_value_is_explicit_disabled_candidate(self):
        self.assertEqual(config.rc_options(b'common --remote_cache=\ncommon --remote_cache ""\n')["options"], [{"status": "disabled"}, {"status": "disabled"}])

    def test_rc_bounds_and_malformed_input_refuse(self):
        for raw in (b"x" * (config.RC_LIMIT + 1), b"\xff", b"\x00", b"x" * (config.VALUE_LIMIT + 1), b'common --remote_cache="unterminated', b"common " + bytes([92]), b"common --remote_cache=https://cache\n" * (config.MAX_RC_OPTIONS + 1)):
            with self.subTest(size=len(raw)):
                with self.assertRaises((ValueError, UnicodeError, config.transport.DiagnosticFailure)):
                    config.rc_options(raw)

    def test_file_reader_regular_and_exact_absence(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "rc"
            path.write_bytes(b"common --remote_cache=https://cache\n")
            row = config.read_rc("system_bazelrc", path)
            self.assertEqual(row["status"], "read")
            self.assertEqual(row["options"], [{"status": "origin", "origin": "https://cache"}])
            self.assertEqual(config.read_rc("system_bazelrc", path.with_name("missing")), {"category": "system_bazelrc", "status": "absent"})

    def test_file_and_ancestor_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "real").mkdir()
            (root / "real/rc").write_bytes(b"common --remote_cache=https://cache\n")
            (root / "link").symlink_to(root / "real/rc")
            (root / "parent-link").symlink_to(root / "real", target_is_directory=True)
            for path in (root / "link", root / "parent-link/rc"):
                self.assertEqual(config.read_rc("system_bazelrc", path)["status"], "refused")

    def test_nonregular_and_size_bound_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            os.mkfifo(root / "fifo")
            (root / "oversize").write_bytes(b"x" * (config.RC_LIMIT + 1))
            for path in (root / "fifo", root / "oversize", root):
                self.assertEqual(config.read_rc("system_bazelrc", path)["status"], "refused")

    def test_same_name_replacement_refused(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "rc"
            path.write_bytes(b"common --remote_cache=https://cache\n")
            original_read, changed = os.read, False
            def replace_after_read(fd, count):
                nonlocal changed
                body = original_read(fd, count)
                if not changed:
                    changed = True
                    path.unlink()
                    path.write_bytes(b"common --remote_cache=https://other\n")
                return body
            with mock.patch.object(config.transport.os, "read", side_effect=replace_after_read):
                self.assertEqual(config.read_rc("system_bazelrc", path)["status"], "refused")

    def test_only_three_fixed_paths_are_requested(self):
        calls = []
        def read(root, relative, cap):
            calls.append((root, relative, cap))
            return b'ENDPOINT = "https://cache"\n' if relative.startswith("opt/") else b"import /private/never-follow\ncommon --remote_cache=https://cache\n"
        with mock.patch.dict(os.environ, {"HOME": "/home/runner"}, clear=True), mock.patch.object(config.transport, "read_under", side_effect=read):
            result = config.collect_files()
        self.assertEqual(calls, [(Path("/"), "etc/bazel.bazelrc", 65536), (Path("/"), "home/runner/.bazelrc", 65536), (Path("/"), "opt/sonic-runner/cache_credentials.py", 65536)])
        self.assertEqual([row["category"] for row in result["rc_files"]], list(config.SOURCES))

    def test_invalid_home_and_auth_fail_before_any_file_read(self):
        for env in ({"HOME": "/home/runner/../secret"}, {"HOME": "/home/runner", "BAZEL_REMOTE_AUTH": "not-read"}):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(config.transport, "read_under") as read:
                with self.assertRaises(config.transport.DiagnosticFailure):
                    config.collect_files()
                read.assert_not_called()

    def test_projection_rejects_extra_fields_and_private_url(self):
        detail = {"schema": 1, "rc_files": [{"category": category, "status": "absent"} for category in config.SOURCES], "runtime_endpoint": {"category": "runtime_credentials_literal_endpoint", "status": "absent"}}
        self.assertEqual(config.validate_files(detail), detail)
        detail["rc_files"][0]["raw"] = "secret"
        with self.assertRaises(config.transport.DiagnosticFailure):
            config.validate_files(detail)
        for value in ("https://user:secret@cache", "https://cache/path", "https://cache?token=secret"):
            with self.assertRaises(config.transport.DiagnosticFailure):
                config.validate_candidate({"status": "origin", "origin": value})

    def test_child_environment_excludes_auth_before_adapter(self):
        class Guarded(dict):
            def __getitem__(self, key):
                if key in ("BAZEL_REMOTE_AUTH", "UNRELATED_SECRET"):
                    raise AssertionError("Secret lookup")
                return super().__getitem__(key)
        inherited = Guarded(HOME="/home/runner", BAZEL_REMOTE_AUTH="secret", UNRELATED_SECRET="secret")
        seen = []
        def build(smoke, selected, directory):
            seen.append(selected)
            return selected
        with mock.patch.object(config.dns, "child_environment", side_effect=build):
            actual = config.child_environment(object(), inherited, Path("/tmp/private"))
        self.assertEqual(actual, {"HOME": "/home/runner"})
        self.assertEqual(seen, [actual])

    def test_file_and_environment_collection_does_not_resolve_or_probe(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(config.dns.socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")))
            stack.enter_context(mock.patch.object(config.transport, "load_native", side_effect=AssertionError("No runtime imports in collection")))
            stack.enter_context(mock.patch.dict(os.environ, {"HOME": "/home/runner"}, clear=True))
            stack.enter_context(mock.patch.object(config.transport, "read_under", return_value=b"common --remote_cache=https://cache\n"))
            self.assertEqual(config.collect_files()["rc_files"][0]["status"], "read")
            self.assertEqual(config.environment_candidates({"REMOTE_CACHE_URL": "https://cache"})[-1]["origin"], "https://cache")

    def test_literal_endpoint_ast_never_executes_or_retains_other_values(self):
        source = b'raise RuntimeError("must not execute")\nTOKEN = "never-read"\nENDPOINT = "https://user:secret@cache/path?token=x"\n'
        self.assertEqual(config.endpoint_from_source(source), {"status": "read", "endpoint": {"status": "origin", "origin": "https://cache"}})

    def test_dynamic_duplicate_and_missing_endpoint_are_uninterpreted(self):
        for source in (b'ENDPOINT = get_secret()\n', b'ENDPOINT = "https://cache"\nENDPOINT += private()\n', b'ENDPOINT = "https://cache"\nENDPOINT = "https://other"\n', b'import secret\nTOKEN = "never-read"\n'):
            self.assertEqual(config.endpoint_from_source(source), {"status": "uninterpreted"})

    def test_runtime_source_reader_preserves_safe_refusal(self):
        for error in (OSError("private-path"), config.transport.DiagnosticFailure("private-detail"), SyntaxError("private-source")):
            with mock.patch.object(config.transport, "read_under", side_effect=error):
                self.assertEqual(config.read_runtime_endpoint(), {"category": "runtime_credentials_literal_endpoint", "status": "refused"})

    def test_known_https_host_expression_is_source_only(self):
        source = b'HOST = "cache.example"\nENDPOINT = "https://" + HOST\nraise RuntimeError("must not execute")\nTOKEN = "never-retain"\n'
        self.assertEqual(config.endpoint_from_source(source), {"status": "read", "endpoint": {"status": "origin", "origin": "https://cache.example"}})

    def test_dynamic_rebound_or_other_host_expressions_uninterpreted(self):
        for source in (b'HOST = "cache"\nHOST = "other"\nENDPOINT = "https://" + HOST\n',
                       b'HOST = get_secret()\nENDPOINT = "https://" + HOST\n',
                       b'HOST = "cache"\nHOST += get_secret()\nENDPOINT = "https://" + HOST\n',
                       b'HOST = "cache"\nfrom private import HOST\nENDPOINT = "https://" + HOST\n',
                       b'HOST = "cache"\nENDPOINT = "http://" + HOST\n',
                       b'HOST = "cache"\nENDPOINT = f"https://{HOST}"\n'):
            self.assertEqual(config.endpoint_from_source(source), {"status": "uninterpreted"})

    def test_conditional_host_or_endpoint_rebinding_is_uninterpreted(self):
        for source in (b'HOST = "cache"\nENDPOINT = "https://" + HOST\nif flag:\n from private import HOST\n',
                       b'ENDPOINT = "https://cache"\nif flag:\n import private as ENDPOINT\n',
                       b'HOST = "cache"\nENDPOINT = "https://" + HOST\nif flag:\n class HOST: pass\n'):
            self.assertEqual(config.endpoint_from_source(source), {"status": "uninterpreted"})

    def test_parent_rejects_unclosed_or_noisy_child(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            private = root / "private"
            private.mkdir()
            identity = {"run": "123", "attempt": "1"}
            smoke = SimpleNamespace(check_identity=lambda env: identity)
            for override in ({"group_closed": False}, {"stdout_bytes": 1}, {"returncode": 1}):
                output = root / ("receipt-" + next(iter(override)) + ".json")
                result = dict(returncode=0, reason=None, leader_reaped=True, group_closed=True, client_invocations=1, stdout_bytes=0, stderr_bytes=0)
                result.update(override)
                smoke.run_command = mock.Mock(return_value=result)
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(config.transport, "load_native", return_value=(smoke, object())))
                    stack.enter_context(mock.patch.object(config, "private_root", return_value=private))
                    stack.enter_context(mock.patch.object(config, "child_environment", return_value={}))
                    stack.enter_context(mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}, clear=True))
                    stack.enter_context(mock.patch.object(config.sys, "argv", ["configuration", "--native-root", str(root), "--output", str(output)]))
                    self.assertEqual(config.main(), 1)
                receipt = json.loads(output.read_text())
                self.assertEqual(receipt["status"], "FAILED_CACHE_CONFIGURATION_DIAGNOSTIC")
                self.assertFalse(receipt["cache_qualified"])
                self.assertNotIn("configuration", receipt)
                self.assertEqual(smoke.run_command.call_args.kwargs["seconds"], 45)


if __name__ == "__main__":
    unittest.main()
