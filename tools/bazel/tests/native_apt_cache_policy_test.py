#!/usr/bin/env python3
"""Inert policy and retry fixtures: no APT, network, root writes, or packages."""

import ast
import copy
import errno
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import native_apt_cache_policy as policy

WORKER_TREE = ast.parse((ROOT / "tools/bazel/ci/source_image_worker.py").read_text())
RETRY = next(node.value.value for node in WORKER_TREE.body if isinstance(node, ast.Assign)
             and any(isinstance(name, ast.Name) and name.id == "APT_OWNED_CACHE_TRANSPORT" for name in node.targets))
SOURCES = b"Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n"


def proof():
    roles = ("helper", "hook", "endpoint", "policy", "marker", "sources", "keyring")
    result = {"schema": 1, "status": "eligible", "scope": "owned_optional_apt_cache_debian_source_only",
              "uid": 0, "stage": "complete", "files": {name: {"bytes": 8, "sha256": "a" * 64,
                  "uid": 0, "mode": 0o444} for name in roles}}
    for name in ("proxy_environment_absent", "helper_matches", "hook_matches", "endpoint_matches",
                 "owned_policy_link", "policy_matches", "keyring_matches", "official_signed_sources", "configuration_matches"):
        result[name] = True
    return result


FIXTURE_ENDPOINT = b"http://192.0.2.1:3128"


class PolicyTest(unittest.TestCase):
    def installed_inspection(self, changed_role=None):
        values = {"Acquire::" + scheme + "::Proxy-Auto-Detect": policy.HELPER for scheme in ("http", "https")}
        policy_bytes = ("Acquire::http::Proxy-Auto-Detect \"" + policy.HELPER + "\";\n"
                        "Acquire::https::Proxy-Auto-Detect \"" + policy.HELPER + "\";\n"
                        "Acquire::https::deb.debian.org::CAInfo \"/run/sonic-native-apt/ca-bundle.pem\";\n").encode()
        bodies = {"native_apt_proxy.py": b"helper", "proxy-job-started.sh": b"hook", "endpoint": FIXTURE_ENDPOINT,
                  "policy": policy_bytes, ".native-proxy-owned": b"", "debian.sources": SOURCES,
                  "debian-archive-keyring.pgp": b"public keyring"}
        def read(path, root, cap):
            data = bodies[path.name]
            sha = {"native_apt_proxy.py": policy.HELPER_SHA, "proxy-job-started.sh": policy.HOOK_SHA, "debian-archive-keyring.pgp": policy.KEYRING_SHA}.get(path.name, hashlib.sha256(data).hexdigest())
            if path.name == changed_role:
                sha = "0" * 64
            return data, {"bytes": len(data), "sha256": sha, "uid": 0, "mode": 0o444}
        with mock.patch.object(policy, "ENDPOINT_SHA", hashlib.sha256(FIXTURE_ENDPOINT).hexdigest()), mock.patch.object(policy.os, "getuid", return_value=0), mock.patch.dict(policy.os.environ, {}, clear=True), mock.patch.object(policy, "read_public_file", side_effect=read) as reads, mock.patch.object(policy, "protected_directory"), mock.patch.object(Path, "resolve", autospec=True, side_effect=lambda p, strict: p), mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)), mock.patch.object(policy.os, "readlink", return_value=str(policy.STATE / "policy")), mock.patch.object(Path, "exists", return_value=False), mock.patch.object(Path, "is_symlink", return_value=False), mock.patch.object(Path, "iterdir", side_effect=AssertionError("Unrelated source directory must not be read")), mock.patch.object(policy, "configuration", return_value=values) as config:
            result = policy.inspect()
        return result, reads.call_args_list, config.call_count

    def test_caller_proxy_and_apt_config_presence_are_preserved(self):
        self.assertTrue(policy.proxy_environment_absent({"PATH": "/usr/bin"}))
        for name in ("http_proxy", "hTtPs_PrOxY", "NO_PROXY", "ALL_PROXY", "APT_CONFIG"):
            self.assertFalse(policy.proxy_environment_absent({name: ""}))

    def test_only_exact_owned_policy_variants_are_admitted(self):
        prefix = ("Acquire::http::Proxy-Auto-Detect \"" + policy.HELPER + "\";\n"
                  "Acquire::https::Proxy-Auto-Detect \"" + policy.HELPER + "\";\n")
        for name in ("ca-bundle.pem", "apt-ca-bundle.pem"):
            value = (prefix + "Acquire::https::deb.debian.org::CAInfo \"/run/sonic-native-apt/" + name + "\";\n").encode()
            self.assertTrue(policy.expected_policy(value))
            self.assertFalse(policy.expected_policy(value + b'Acquire::https::Verify-Peer "false";\n'))

    def test_configured_official_signed_sources_are_preserved(self):
        self.assertTrue(policy.official_sources(SOURCES))
        self.assertTrue(policy.official_sources(SOURCES.replace(b"https://", b"http://")))

    def test_alternative_origins_credentials_and_unsigned_sources_are_rejected(self):
        for old, new in ((b"deb.debian.org", b"mirror.example"),
                         (b"https://", b"https://user:password@"),
                         (b"Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp", b"Trusted: yes"),
                         (b"Suites: trixie trixie-updates", b"Suites: unstable")):
            with self.assertRaises(ValueError):
                policy.official_sources(SOURCES.replace(old, new))

    def test_extra_and_continuation_source_fields_are_rejected(self):
        for value in (SOURCES + b"Trusted: yes\n", SOURCES + b" allow-insecure=yes\n", SOURCES + b"Types: deb\n"):
            with self.assertRaises(ValueError):
                policy.official_sources(value)

    def test_configuration_requires_only_the_owned_routes(self):
        values = {"Acquire::" + scheme + "::Proxy-Auto-Detect": policy.HELPER for scheme in ("http", "https")}
        self.assertTrue(policy.configuration_matches(values))
        for key in ("Acquire::http::Proxy", "Acquire::https::Proxy::deb.debian.org",
                    "Binary::http::Acquire::http::Proxy", "Binary::https::Acquire::https::ProxyAutoDetect"):
            self.assertFalse(policy.configuration_matches({**values, key: "DIRECT"}))
        self.assertFalse(policy.configuration_matches({**values, "Acquire::https::Proxy-Auto-Detect": "/other"}))

    def test_insecure_trust_and_alternate_configuration_roots_are_rejected(self):
        values = {"Acquire::" + scheme + "::Proxy-Auto-Detect": policy.HELPER for scheme in ("http", "https")}
        for key in policy.TRUST_KEYS:
            self.assertFalse(policy.configuration_matches({**values, key: "true"}))
        self.assertFalse(policy.configuration_matches({**values, "Dir::Etc::sourceparts": "other"}))

    def test_configuration_query_is_read_only_and_uses_apt_get_scope(self):
        with mock.patch.object(policy.subprocess, "run", return_value=SimpleNamespace(stdout="K2='" + policy.HELPER + "'")) as run:
            self.assertEqual(policy.configuration(), {policy.CONFIG_KEYS[2]: policy.HELPER})
        self.assertEqual(run.call_args.args[0][:2], ["apt-get", "shell"])
        self.assertEqual(run.call_args.kwargs["executable"], "/usr/bin/apt-config")
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_malformed_duplicate_and_oversized_configuration_is_rejected(self):
        for value in ("K2=x K2=y", "UNKNOWN=x", "K2", "x" * 65537):
            with mock.patch.object(policy.subprocess, "run", return_value=SimpleNamespace(stdout=value)):
                with self.assertRaises(ValueError):
                    policy.configuration()

    def test_public_report_rejects_contents_and_unbounded_fields(self):
        value = proof()
        self.assertEqual(policy.validate_report(value), value)
        for changed in ({**value, "contents": "private"}, {**value, "error_class": "raw error text"},
                        {**value, "files": {"unknown": value["files"]["helper"]}}):
            with self.assertRaises(ValueError):
                policy.validate_report(changed)

    def test_incomplete_eligible_report_is_rejected(self):
        value = proof()
        del value["files"]["marker"]
        with self.assertRaises(ValueError):
            policy.validate_report(value)

    def test_metadata_inspection_does_not_read_files_when_caller_overrides_exist(self):
        with mock.patch.object(policy.os, "getuid", return_value=0), mock.patch.dict(policy.os.environ, {"HTTPS_PROXY": "existing"}, clear=True), mock.patch.object(policy, "read_public_file") as read:
            value = policy.inspect()
        self.assertEqual(value["status"], "not_eligible")
        self.assertEqual(value["stage"], "uid_and_environment")
        read.assert_not_called()

    def test_exact_installed_owner_metadata_is_eligible_without_network_or_contents(self):
        value, reads, config_count = self.installed_inspection()
        self.assertEqual(policy.validate_report(value)["status"], "eligible")
        self.assertEqual(len(reads), 7)
        self.assertEqual(config_count, 1)
        self.assertNotIn(FIXTURE_ENDPOINT.decode(), json.dumps(value))
        self.assertNotIn(SOURCES.decode(), json.dumps(value))

    def test_changed_installed_helper_or_hook_is_rejected_before_config_query(self):
        for name in ("native_apt_proxy.py", "proxy-job-started.sh"):
            value, _, config_count = self.installed_inspection(name)
            self.assertEqual(value["status"], "not_eligible")
            self.assertEqual(value["stage"], "owner_identity")
            self.assertEqual(config_count, 0)

    def test_referenced_pgp_keyring_identity_is_checked_before_configuration(self):
        value, _, calls = self.installed_inspection("debian-archive-keyring.pgp")
        self.assertFalse(value["keyring_matches"])
        self.assertEqual(value["status"], "not_eligible")
        self.assertEqual(calls, 0)

    def test_other_source_directories_are_not_part_of_the_selected_scope(self):
        value, reads, _ = self.installed_inspection()
        self.assertEqual(value["status"], "eligible")
        self.assertEqual(value["scope"], "owned_optional_apt_cache_debian_source_only")
        paths = [call.args[0] for call in reads]
        self.assertIn(policy.SOURCE, paths)
        self.assertIn(policy.KEYRING, paths)
        self.assertNotIn(Path("/etc/apt/sources.list"), paths)

    def test_reviewed_stock_source_uses_the_referenced_pgp_keyring(self):
        source = (b"Types: deb\n# http://snapshot.debian.org/archive/debian/20260824T000000Z\n"
                  b"URIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\n"
                  b"Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp\n\n"
                  b"Types: deb\n# http://snapshot.debian.org/archive/debian-security/20260824T000000Z\n"
                  b"URIs: http://deb.debian.org/debian-security\nSuites: trixie-security\nComponents: main\n"
                  b"Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp\n")
        self.assertEqual(hashlib.sha256(source).hexdigest(), "46b768cfd5db09b20052dccc2cfd365fa90277491b767035cfd8b0078b42d33c")
        self.assertTrue(policy.official_sources(source))
        with self.assertRaises(ValueError):
            policy.official_sources(source.replace(b".pgp", b".gpg"))


class HeldPublicFileTest(unittest.TestCase):
    def info(self, **changed):
        values = dict(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o444, st_uid=0, st_gid=0,
                      st_nlink=1, st_size=6, st_mtime_ns=3, st_ctime_ns=4)
        values.update(changed)
        return SimpleNamespace(**values)

    def read(self, before=None, after=None, resolutions=None):
        before = before or self.info()
        after = after or before
        class Buffer(io.BytesIO):
            def fileno(self):
                return 7
        root, path = Path("/fixed"), Path("/fixed/file")
        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolutions or (lambda p, strict: p)), mock.patch.object(Path, "stat", return_value=after), mock.patch.object(policy, "protected_directory"), mock.patch.object(policy.os, "open", return_value=7), mock.patch.object(policy.os, "fdopen", return_value=Buffer(b"public")), mock.patch.object(policy.os, "fstat", side_effect=[before, after]):
            return policy.read_public_file(path, root, 64)

    def test_held_regular_file_keeps_exact_bytes_and_public_identity(self):
        data, row = self.read()
        self.assertEqual(data, b"public")
        self.assertEqual(row["bytes"], 6)
        self.assertEqual(row["mode"], 0o444)
        self.assertEqual(row["sha256"], hashlib.sha256(b"public").hexdigest())

    def test_writable_nonroot_oversized_or_nonregular_source_is_rejected(self):
        for changed in (dict(st_mode=stat.S_IFREG | 0o666), dict(st_uid=1000), dict(st_size=65),
                        dict(st_mode=stat.S_IFIFO | 0o444)):
            with self.assertRaises(ValueError):
                self.read(before=self.info(**changed))

    def test_changed_file_or_rotated_projection_is_rejected(self):
        with self.assertRaises(ValueError):
            self.read(after=self.info(st_mtime_ns=9))
        with self.assertRaises(ValueError):
            self.read(resolutions=[Path("/fixed"), Path("/fixed/file"), Path("/fixed/replacement")])

    def test_readonly_projected_directory_may_retain_mode_write_bits(self):
        with mock.patch.object(Path, "stat", return_value=self.info(st_mode=stat.S_IFDIR | 0o777)), mock.patch.object(policy.os, "statvfs", return_value=SimpleNamespace(f_flag=policy.os.ST_RDONLY)):
            policy.protected_directory(Path("/fixed"))

    def test_writable_projected_directory_is_rejected(self):
        with mock.patch.object(Path, "stat", return_value=self.info(st_mode=stat.S_IFDIR | 0o777)), mock.patch.object(policy.os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaises(ValueError):
                policy.protected_directory(Path("/fixed"))

    def test_actual_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fifo"
            policy.os.mkfifo(path, 0o600)
            with mock.patch.object(policy, "protected_directory"):
                with self.assertRaises(ValueError):
                    policy.read_public_file(path, root, 64)


class PrivateEndpointTest(unittest.TestCase):
    def address(self, data, *, recorded=None, expected=None):
        digest = hashlib.sha256(data).hexdigest()
        with mock.patch.object(policy, "read_public_file", return_value=(data, {"sha256": recorded or digest})) as read, mock.patch.object(policy, "ENDPOINT_SHA", expected or digest):
            value = policy.verified_cache_address()
        read.assert_called_once_with(policy.MOUNT / "endpoint", policy.MOUNT, 256)
        return value

    def test_verified_numeric_address_stays_internal(self):
        self.assertEqual(self.address(FIXTURE_ENDPOINT), ("192.0.2.1", 3128))

    def test_endpoint_digest_and_read_metadata_must_both_match(self):
        for kwargs in ({"recorded": "0" * 64}, {"expected": "0" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.address(FIXTURE_ENDPOINT, **kwargs)

    def test_hostname_is_never_resolved(self):
        with mock.patch("socket.getaddrinfo") as resolve, self.assertRaises(ValueError):
            self.address(b"http://cache.example:3128")
        resolve.assert_not_called()

    def test_url_shape_cannot_expand_the_fixed_transport(self):
        for data in (b"https://192.0.2.1:3128", b"http://user@192.0.2.1:3128", b"http://192.0.2.1:3128/path", b"http://192.0.2.1:3128?x", b"http://192.0.2.1:3128#x", b"http://[2001:db8::1]:3128", b"http://192.0.2.1:0"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.address(data)

    def test_changed_endpoint_after_policy_proof_stops_before_probe(self):
        namespace = {"__name__": "fixture"}
        with mock.patch.object(sys, "argv", ["fixture", str(ROOT), '["libarchive-tools"]']), mock.patch.object(sys, "path", sys.path.copy()), mock.patch.object(policy, "inspect", return_value=proof()), mock.patch.object(policy, "verified_cache_address", side_effect=ValueError("changed")), mock.patch("socket.socket") as socket, mock.patch.object(subprocess, "run") as run:
            exec(compile(RETRY, "<reviewed-apt-transport>", "exec"), namespace)
            with self.assertRaises(ValueError):
                namespace["select_transport"]()
        socket.assert_not_called()
        run.assert_not_called()


class TransportSelectionTest(unittest.TestCase):
    def invoke(self, error=None, inspection=None, command_error=None):
        namespace = {"__name__": "fixture"}
        sock = mock.MagicMock()
        sock.connect.side_effect = error
        with mock.patch.object(policy, "verified_cache_address", return_value=("192.0.2.1", 3128)), mock.patch.object(sys, "argv", ["fixture", str(ROOT), '["libarchive-tools"]']), mock.patch.object(sys, "path", sys.path.copy()), mock.patch.object(policy, "inspect", side_effect=inspection or [proof(), proof(), proof()]), mock.patch("socket.socket", return_value=sock), mock.patch.object(subprocess, "run", side_effect=command_error) as run:
            exec(compile(RETRY, "<reviewed-apt-retry>", "exec"), namespace)
            value = namespace["select_transport"]()
        return value, run.call_args_list, sock

    def test_unverified_policy_does_not_probe_or_retry(self):
        value = proof()
        value.update(status="not_eligible", stage="owner_identity", helper_matches=False)
        result, calls, sock = self.invoke(inspection=[value])
        self.assertEqual(result["status"], "not_selected")
        self.assertEqual(calls, [])
        sock.connect.assert_not_called()

    def test_reachable_cache_preserves_the_existing_route(self):
        result, calls, sock = self.invoke()
        self.assertEqual(result["cache_connection"], "reachable")
        self.assertEqual(result["status"], "not_selected")
        self.assertEqual(calls, [])
        sock.close.assert_called_once()

    def test_unrelated_socket_error_does_not_enable_retry(self):
        result, calls, _ = self.invoke(OSError(errno.EACCES, "fixture"))
        self.assertEqual(result["cache_connection"], "other_error")
        self.assertEqual(calls, [])

    def test_cache_timeout_selects_one_bounded_strict_update_then_install(self):
        result, calls, sock = self.invoke(TimeoutError())
        self.assertEqual(result["status"], "direct_success")
        self.assertEqual(result["completed_actions"], ["update", "install"])
        sock.settimeout.assert_called_once_with(3)
        sock.connect.assert_called_once_with(("192.0.2.1", 3128))
        self.assertEqual(len(calls), 2)
        for call in calls:
            argv = call.args[0]
            self.assertEqual(argv[:5], ["/usr/bin/timeout", "--signal=TERM", "--kill-after=10s", "300s", "/usr/bin/apt-get"])
            self.assertIn("Acquire::http::Proxy::deb.debian.org=DIRECT", argv)
            self.assertIn("Acquire::https::Proxy::deb.debian.org=DIRECT", argv)
            self.assertNotIn("--allow-unauthenticated", argv)
        self.assertIn("APT::Update::Error-Mode=any", calls[0].args[0])
        self.assertEqual(calls[1].args[0][-4:], ["install", "-y", "--no-install-recommends", "libarchive-tools"])

    def test_connection_refusal_and_unreachable_are_the_only_other_retry_causes(self):
        for number in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH):
            value, calls, _ = self.invoke(OSError(number, "fixture"))
            self.assertEqual(value["status"], "direct_success")
            self.assertEqual(len(calls), 2)

    def test_signature_or_update_failure_stops_without_install_or_further_fallback(self):
        result, calls, _ = self.invoke(TimeoutError(), command_error=subprocess.CalledProcessError(100, ["fixture"]))
        self.assertEqual(result["status"], "direct_failed")
        self.assertEqual(result["completed_actions"], [])
        self.assertEqual(result["exit_code"], 100)
        self.assertEqual(len(calls), 1)

    def test_changed_policy_before_retry_prevents_any_apt_action(self):
        changed = copy.deepcopy(proof())
        changed["files"]["policy"]["sha256"] = "b" * 64
        result, calls, _ = self.invoke(TimeoutError(), [proof(), changed])
        self.assertEqual(result["status"], "direct_failed")
        self.assertEqual(calls, [])

    def test_changed_policy_after_update_prevents_install(self):
        changed = copy.deepcopy(proof())
        changed["files"]["sources"]["sha256"] = "b" * 64
        result, calls, _ = self.invoke(TimeoutError(), [proof(), proof(), changed])
        self.assertEqual(result["status"], "direct_failed")
        self.assertEqual(result["completed_actions"], ["update"])
        self.assertEqual(len(calls), 1)

    def test_direct_apt_selects_only_verified_source_and_empty_temporary_parts(self):
        roots = []
        def inspect_command(argv, **unused):
            options = dict(value.split("=", 1) for value in argv if value.startswith("Dir::"))
            self.assertEqual(options["Dir::Etc::sourcelist"], str(policy.SOURCE))
            parts = Path(options["Dir::Etc::sourceparts"])
            roots.append(parts.parent)
            self.assertEqual(list(parts.iterdir()), [])
            self.assertEqual(Path(options["Dir::State::lists"]).parent, parts.parent)
            self.assertEqual(Path(options["Dir::Cache::archives"]).parent, parts.parent)
            self.assertEqual(options["Dir::Cache::pkgcache"], "")
            self.assertEqual(options["Dir::Cache::srcpkgcache"], "")
            self.assertEqual(stat.S_IMODE(parts.stat().st_mode), 0o755)
        result, calls, _ = self.invoke(TimeoutError(), command_error=inspect_command)
        self.assertEqual(result["status"], "direct_success")
        self.assertEqual(len(calls), 2)
        self.assertEqual(roots[0], roots[1])
        self.assertFalse(roots[0].exists())

    def test_temporary_indexes_are_removed_after_install_failure(self):
        roots = []
        def fail_install(argv, **unused):
            options = dict(value.split("=", 1) for value in argv if value.startswith("Dir::"))
            lists = Path(options["Dir::State::lists"])
            roots.append(lists.parent)
            (lists / "fixture-index").write_text("fixture index")
            if "install" in argv:
                raise subprocess.CalledProcessError(100, argv)
        result, calls, _ = self.invoke(TimeoutError(), command_error=fail_install)
        self.assertEqual(result["status"], "direct_failed")
        self.assertEqual(result["completed_actions"], ["update"])
        self.assertEqual(result["exit_code"], 100)
        self.assertEqual(len(calls), 2)
        self.assertFalse(roots[0].exists())

    def test_temporary_allocation_failure_never_retries_the_old_route(self):
        with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=OSError(errno.ENOSPC, "fixture")):
            result, calls, _ = self.invoke(TimeoutError())
        self.assertEqual(result["status"], "direct_failed")
        self.assertEqual(result["error_class"], "OSError")
        self.assertEqual(calls, [])


class PreparationContractTest(unittest.TestCase):
    def functions(self, names, **values):
        nodes = [node for node in WORKER_TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
        namespace = dict(json=json, os=policy.os, sys=sys, re=re, subprocess=subprocess,
                         native_apt_cache_policy=policy, ROOT=ROOT, APT_OWNED_CACHE_TRANSPORT=RETRY,
                         safe_json=lambda value: None, **values)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<reviewed-tools-owner>", "exec"), namespace)
        return namespace

    def owner(self, selected=False, selection_error=None, normal_error=None):
        selector = mock.Mock(return_value=selected, side_effect=selection_error)
        normal = mock.Mock(side_effect=normal_error)
        diagnostics = mock.Mock()
        namespace = self.functions({"require", "prepare_validation_tools"},
            shutil=SimpleNamespace(which=mock.Mock(side_effect=[None] * 4 + ["present"] * 4)),
            select_tools_cache_transport=selector, run=normal, output=mock.Mock(return_value="fixture\t1"),
            prepare_execution_network=diagnostics)
        return namespace["prepare_validation_tools"], selector, normal, diagnostics

    def test_not_selected_preserves_the_two_original_apt_commands(self):
        owner, selector, normal, diagnostics = self.owner()
        with tempfile.TemporaryDirectory() as directory:
            owner(Path(directory))
        selector.assert_called_once()
        self.assertEqual(normal.call_count, 2)
        self.assertEqual(normal.call_args_list[0].args[0],
                         ["sudo", "-n", "apt-get", "-o", "Acquire::EnableSrvRecords=false", "update"])
        diagnostics.assert_not_called()

    def test_direct_success_does_not_execute_the_old_route(self):
        owner, selector, normal, diagnostics = self.owner(selected=True)
        with tempfile.TemporaryDirectory() as directory:
            owner(Path(directory))
        selector.assert_called_once()
        normal.assert_not_called()
        diagnostics.assert_not_called()

    def test_direct_failure_is_terminal_without_old_route_retry(self):
        error = subprocess.CalledProcessError(100, ["apt-get", "update"])
        owner, _, normal, diagnostics = self.owner(selection_error=error)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(subprocess.CalledProcessError) as caught:
            owner(Path(directory))
        self.assertIs(caught.exception, error)
        normal.assert_not_called()
        diagnostics.assert_called_once()

    def test_indeterminate_child_outcome_is_terminal(self):
        error = subprocess.TimeoutExpired(["fixture"], 660)
        owner, _, normal, _ = self.owner(selection_error=error)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(subprocess.TimeoutExpired):
            owner(Path(directory))
        normal.assert_not_called()

    def test_original_route_failure_does_not_call_selection_again(self):
        error = subprocess.CalledProcessError(100, ["apt-get", "update"])
        owner, selector, normal, diagnostics = self.owner(normal_error=error)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(subprocess.CalledProcessError):
            owner(Path(directory))
        selector.assert_called_once()
        normal.assert_called_once()
        diagnostics.assert_called_once()

    def test_direct_failure_metadata_and_actual_exit_are_retained(self):
        value = {"schema": 1, "status": "direct_failed", "policy": proof(), "completed_actions": [],
                 "cache_connection": "refused", "error_class": "CalledProcessError", "exit_code": 100}
        namespace = self.functions({"require", "select_tools_cache_transport"},
            selected_worker_pool=lambda: "sonic-slave-container-k3s", output=mock.Mock(return_value=json.dumps(value)))
        with mock.patch.dict(policy.os.environ, {}, clear=True), tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                namespace["select_tools_cache_transport"](["libarchive-tools"], Path(directory))
            self.assertEqual(caught.exception.returncode, 100)
            self.assertEqual(json.loads((Path(directory) / "worker-apt-transport-selection.json").read_text()), value)

    def test_malformed_child_report_does_not_fall_back_to_old_route(self):
        namespace = self.functions({"require", "select_tools_cache_transport"},
            selected_worker_pool=lambda: "sonic-slave-container-k3s", output=mock.Mock(return_value="not JSON"))
        with mock.patch.dict(policy.os.environ, {}, clear=True), tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                namespace["select_tools_cache_transport"](["libarchive-tools"], Path(directory))


if __name__ == "__main__":
    unittest.main()
