"""Pure cache-evidence fixtures; no Bazel, daemon, compiler or package execution."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.ci import remote_cache_smoke as smoke


def spawn(version="8.5.1", cached=True, camel=False):
    value = {"mnemonic": "Genrule", "target_label": "//:cache_probe",
             "exit_code": 0, "status": "", "runner": "remote cache hit" if cached else "linux-sandbox",
             "remote_cache_hit" if version == "6.4.0" else "cache_hit": cached,
             "digest": {"hash": "a" * 64},
             "actual_outputs": [{"path": "bazel-out/k8-fastbuild/bin/cache-probe.txt"}],
             "listed_outputs": ["bazel-out/k8-fastbuild/bin/cache-probe.txt"]}
    if camel:
        value = {key.split("_")[0] + "".join(p.title() for p in key.split("_")[1:]): data
                 for key, data in value.items()}
    return value


def child_result(status="CAPTURED", reason=None):
    return {"status": status, "returncode": 0 if status == "CAPTURED" else 1,
            "reason": reason, "leader_reaped": True, "group_closed": True,
            "stdout_bytes": 0, "stderr_bytes": 0,
            "stdout_sha256": smoke.sha(b""), "stderr_sha256": smoke.sha(b"")}


class RemoteCacheSmokeTest(unittest.TestCase):
    def test_real_version_command_and_optional_first_bazelisk_banner(self):
        for version in smoke.VERSIONS:
            for banner in (b"", b"Bazelisk version: fixture\n"):
                smoke.verify_version_output(banner + ("bazel " + version + "\n").encode(), version)
            for raw in (b"bazel 0.0.0\n", b"Bazelisk fixture\nBazelisk second\n", b"extra\nbazel 8.5.1\n"):
                with self.assertRaises(smoke.ProbeFailure):
                    smoke.verify_version_output(raw, version)

    def test_public_child_projection_drops_raw_argv_paths_and_secret_values(self):
        value = child_result("FAILED_STOP_NO_RETRY", "CLIENT_DEADLINE")
        value.update(argv=["private"], stderr="fixture-secret", path="/private/path", environment={"TOKEN": "fixture-secret"})
        public = smoke.project_child_result(value)
        self.assertEqual(public["control_reason"], "CLIENT_DEADLINE")
        self.assertNotIn("fixture-secret", json.dumps(public))
        self.assertNotIn("/private", json.dumps(public))
        for key, bad in (("reason", "fixture-secret"), ("returncode", "fixture-secret"),
                         ("stdout_sha256", "fixture-secret"), ("group_closed", "yes")):
            altered = dict(value, **{key: bad})
            with self.assertRaises(smoke.ProbeFailure):
                smoke.project_child_result(altered)

    def test_interrupted_command_retains_only_safe_existing_control_evidence(self):
        observations = []
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def failed(*args, **kwargs):
                smoke.write_json(directory / "result.json", child_result("FAILED_STOP_NO_RETRY", "CLIENT_DEADLINE"))
                raise TimeoutError("fixture-private-URL")
            with patch.object(smoke, "observe_client", side_effect=failed):
                with self.assertRaises(TimeoutError):
                    smoke.run_command(directory, ["not-executed"], {}, cwd=directory, seconds=1, record=observations.append)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["control_reason"], "CLIENT_DEADLINE")
        self.assertTrue(observations[0]["leader_reaped"])
        self.assertTrue(observations[0]["group_closed"])
        self.assertNotIn("fixture-private", json.dumps(observations))

    def test_only_6_4_local_seed_may_omit_action_digest(self):
        for version, cached in (("6.4.0", False), ("6.4.0", True), ("8.5.1", False), ("8.5.1", True)):
            value = spawn(version, cached)
            del value["digest"]
            if version == "6.4.0" and not cached:
                result = smoke.spawn_evidence(json.dumps(value).encode(), version, cached)
                self.assertIsNone(result["action_sha256"])
            else:
                with self.assertRaises(ValueError):
                    smoke.spawn_evidence(json.dumps(value).encode(), version, cached)

    def test_version_specific_real_hit_fields_and_proto_json_spellings(self):
        for version in smoke.VERSIONS:
            for cached in (False, True):
                for camel in (False, True):
                    result = smoke.spawn_evidence(json.dumps(spawn(version, cached, camel)).encode(), version, cached)
                    self.assertEqual(result["cache_hit"], cached)
                    self.assertEqual(result["target"], "//:cache_probe")

    def test_disk_local_missing_and_wrong_version_hits_fail(self):
        for version in smoke.VERSIONS:
            for change in ({"runner": "disk cache hit"}, {"runner": "local"},
                           {"remote_cache_hit" if version == "6.4.0" else "cache_hit": False}):
                value = spawn(version)
                value.update(change)
                with self.assertRaises(ValueError):
                    smoke.spawn_evidence(json.dumps(value).encode(), version, True)
        with self.assertRaises(ValueError):
            smoke.spawn_evidence(json.dumps(spawn("6.4.0")).encode(), "8.5.1", True)

    def test_wrong_target_failed_action_and_missing_digest_fail(self):
        for change in ({"target_label": "//:other"}, {"mnemonic": "CppCompile"},
                       {"exit_code": 1}, {"exit_code": False}, {"status": "FAILED"}, {"digest": {}}):
            value = spawn()
            value.update(change)
            with self.assertRaises(ValueError):
                smoke.spawn_evidence(json.dumps(value).encode(), "8.5.1", True)

    def test_extra_truncated_duplicate_and_ambiguous_records_fail(self):
        raw = json.dumps(spawn()).encode()
        for invalid in (raw + raw, raw[:-1], b"[]", b'{"mnemonic":"Genrule","mnemonic":"other"}',
                        raw[:-1] + b',"cacheHit":true}', b" " * (smoke.LOG_CAP + 1)):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                smoke.spawn_evidence(invalid, "8.5.1", True)

    def test_unexpected_output_paths_and_sets_fail(self):
        for name in ("/tmp/cache-probe.txt", "bazel-out/../../cache-probe.txt", "bazel-out/other.txt"):
            value = spawn()
            value["actual_outputs"] = [{"path": name}]
            value["listed_outputs"] = [name]
            with self.assertRaises(ValueError):
                smoke.spawn_evidence(json.dumps(value).encode(), "8.5.1", True)
        value = spawn()
        value["listed_outputs"].append("second-output")
        with self.assertRaises(ValueError):
            smoke.spawn_evidence(json.dumps(value).encode(), "8.5.1", True)

    def test_selected_file_bounds_and_no_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "regular.bin").write_bytes(b"small")
            self.assertEqual(smoke.read_under(root, "regular.bin", 5), b"small")
            with self.assertRaises(ValueError):
                smoke.read_under(root, "regular.bin", 4)
            (root / "link.bin").symlink_to("regular.bin")
            (root / "directory").symlink_to(root, target_is_directory=True)
            for name in ("link.bin", "directory/regular.bin", "../regular.bin"):
                with self.assertRaises((ValueError, OSError)):
                    smoke.read_under(root, name, 10)

    def test_phase_arguments_disable_disk_and_remote_execution(self):
        for version in smoke.VERSIONS:
            for cached in (False, True):
                args = smoke.build_arguments(version, Path("/private/spawns.json"), cached, Path("/private/user-root"))
                self.assertIn("--batch", args)
                self.assertIn("--remote_executor=", args)
                self.assertIn("--disk_cache=", args)
                self.assertIn("--remote_accept_cached=" + str(cached).lower(), args)
                self.assertIn("--remote_upload_local_results=" + str(not cached).lower(), args)
                self.assertEqual("--enable_workspace=true" in args, version == "8.5.1")
                self.assertEqual([a for a in args if a.startswith("//")], ["//:cache_probe"])

    def test_auth_only_reaches_client_environment_not_action_flags(self):
        secret = "fixture-not-a-credential"
        env = smoke.version_environment({"BAZEL_REMOTE_AUTH": secret, "GITHUB_TOKEN": "never-forward",
                                         "BAZEL_OPTS": "unreviewed", "PATH": "/unexpected"}, Path("/private"), "8.5.1")
        self.assertEqual(env["BAZEL_REMOTE_AUTH"], secret)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("BAZEL_OPTS", env)
        self.assertNotIn(secret, json.dumps(smoke.build_arguments("8.5.1", Path("/private/log"), True, Path("/private/user-root"))))

    def test_two_fresh_bases_real_launcher_selection_and_safe_projection(self):
        configuration = {"endpoint": smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"],
                         "helper": "/selected/tools/bazel/cache/credentials.py",
                         "helper_sha256": smoke.worker.K3S_REMOTE_CACHE_POLICY["credential_helper_sha256"]}
        for version in smoke.VERSIONS:
            calls = []
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / version
                def observed(phase, argv, env, *, cwd, seconds):
                    calls.append((phase.name, argv, env.copy()))
                    if phase.name == "version":
                        self.assertEqual(argv[-2:], ["version", "--gnu_format"])
                        (phase / "stdout.data").write_bytes(("bazel " + version + "\n").encode())
                    else:
                        base = directory / (phase.name + "-output-base")
                        self.assertFalse(base.exists())
                        source = (phase / "bazel").read_text()
                        self.assertIn("--credential_helper=", source)
                        self.assertIn("--disk_cache=", source)
                        self.assertNotIn("fixture-not-a-credential", source)
                        if version == "6.4.0":
                            self.assertIn("USE_BAZEL_VERSION", source)
                        value = spawn(version, phase.name == "reuse")
                        (phase / "spawns.json").write_text(json.dumps(value))
                        out = base / "execroot/remote_cache_smoke" / value["actual_outputs"][0]["path"]
                        out.parent.mkdir(parents=True)
                        out.write_bytes((cwd / "input.txt").read_bytes())
                    return child_result()
                with patch.object(smoke, "observe_client", side_effect=observed):
                    result = smoke.qualify_version(directory, "/usr/bin/bazel", version, configuration,
                                                   {"BAZEL_REMOTE_AUTH": "fixture-not-a-credential"}, "unique-input")
                self.assertEqual([row[0] for row in calls], ["version", "seed", "reuse"])
                self.assertTrue(result["same_output"])
                self.assertEqual(result["action_digest_comparison"], "equal")
                self.assertEqual(result["phases"][0]["output_sha256"], result["phases"][1]["output_sha256"])
                self.assertNotIn("fixture-not-a-credential", json.dumps(result))
                self.assertNotIn(str(directory), json.dumps(result))
                with patch.object(smoke, "observe_client") as again:
                    with self.assertRaises(FileExistsError):
                        smoke.qualify_version(directory, "/usr/bin/bazel", version, configuration, {}, "unique-input")
                    again.assert_not_called()



    def test_failed_command_stops_before_any_reuse(self):
        configuration = {"endpoint": smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"],
                         "helper": "/selected/tools/bazel/cache/credentials.py",
                         "helper_sha256": smoke.worker.K3S_REMOTE_CACHE_POLICY["credential_helper_sha256"]}
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            def observed(phase, argv, env, *, cwd, seconds):
                calls.append(phase.name)
                if phase.name == "version":
                    (phase / "stdout.data").write_bytes(b"bazel 8.5.1\n")
                    return child_result()
                return child_result("FAILED_STOP_NO_RETRY")
            with patch.object(smoke, "observe_client", side_effect=observed):
                with self.assertRaisesRegex(ValueError, "Bazel copy phase failed"):
                    smoke.qualify_version(Path(tmp) / "v8", "/usr/bin/bazel", "8.5.1", configuration, {}, "input")
        self.assertEqual(calls, ["version", "seed"])

    def test_output_or_action_difference_never_qualifies(self):
        configuration = {"endpoint": smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"],
                         "helper": "/selected/tools/bazel/cache/credentials.py",
                         "helper_sha256": smoke.worker.K3S_REMOTE_CACHE_POLICY["credential_helper_sha256"]}
        for mismatch in ("output", "action"):
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / "v8"
                def observed(phase, argv, env, *, cwd, seconds):
                    if phase.name == "version":
                        self.assertEqual(argv[-2:], ["version", "--gnu_format"])
                        (phase / "stdout.data").write_bytes(b"bazel 8.5.1\n")
                    else:
                        value = spawn(cached=phase.name == "reuse")
                        if mismatch == "action" and phase.name == "reuse":
                            value["digest"]["hash"] = "b" * 64
                        (phase / "spawns.json").write_text(json.dumps(value))
                        output = directory / (phase.name + "-output-base") / "execroot/remote_cache_smoke" / value["actual_outputs"][0]["path"]
                        output.parent.mkdir(parents=True)
                        data = (cwd / "input.txt").read_bytes()
                        output.write_bytes(b"different" if mismatch == "output" and phase.name == "reuse" else data)
                    return child_result()
                with patch.object(smoke, "observe_client", side_effect=observed):
                    with self.assertRaises(ValueError):
                        smoke.qualify_version(directory, "/usr/bin/bazel", "8.5.1", configuration, {}, "input")


    def test_failed_credential_helper_receipt_survives_returncode_guard(self):
        endpoint = smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"]
        for error in ({"error_type": "HTTPError", "status": 401}, {"error_type": "URLError"},
                      {"error_type": "ValueError"}):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                access = dict(endpoint=endpoint, authenticated=False, **error)
                root = Path(tmp)
                (root / "access.json").write_text(json.dumps(access))
                failed = child_result("FAILED_STOP_NO_RETRY")
                receipt = {"status": "FAILED_NOT_QUALIFIED", "versions": [], "commands": [failed]}
                with self.assertRaisesRegex(smoke.ProbeFailure, "Credential probe command failed"):
                    smoke.check_credential_probe_receipt(root, "access.json", endpoint, failed, receipt)
                self.assertEqual(receipt["credential_probe"], access)
                self.assertEqual(receipt["commands"][0]["returncode"], 1)
                self.assertEqual(receipt["status"], "FAILED_NOT_QUALIFIED")
                self.assertEqual(receipt["versions"], [])
                self.assertNotIn("authenticated_probe", receipt)

    def test_successful_credential_probe_requires_both_child_and_helper_success(self):
        endpoint = smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            success = {"endpoint": endpoint, "authenticated": True, "status": 200}
            (root / "access.json").write_text(json.dumps(success))
            receipt = {}
            smoke.check_credential_probe_receipt(root, "access.json", endpoint, child_result(), receipt)
            self.assertEqual(receipt["credential_probe"], success)
            with self.assertRaisesRegex(smoke.ProbeFailure, "Credential probe command failed"):
                smoke.check_credential_probe_receipt(root, "access.json", endpoint,
                                                     child_result("FAILED_STOP_NO_RETRY"), {})
            (root / "access.json").write_text(json.dumps(
                {"endpoint": endpoint, "authenticated": False, "error_type": "URLError"}))
            with self.assertRaisesRegex(smoke.ProbeFailure, "Credential probe did not pass"):
                smoke.check_credential_probe_receipt(root, "access.json", endpoint, child_result(), {})

    def test_credential_probe_rejects_extra_fields_untrusted_classes_and_status_types(self):
        endpoint = smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"]
        base = {"endpoint": endpoint, "authenticated": False, "error_type": "HTTPError", "status": 401}
        bad_values = [dict(base, Authorization="fixture-private-value"),
                      dict(base, error_type="fixture-private-value"), dict(base, status=True),
                      dict(base, status="fixture-private-value"), dict(base, status=999),
                      dict(base, authenticated=1), dict(base, endpoint="https://fixture.invalid"),
                      {k: v for k, v in base.items() if k != "status"},
                      dict(base, error_type="URLError")]
        for access in bad_values:
            with self.subTest(access=access), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "access.json").write_text(json.dumps(access))
                receipt = {}
                with self.assertRaises(smoke.ProbeFailure):
                    smoke.check_credential_probe_receipt(root, "access.json", endpoint,
                                                         child_result("FAILED_STOP_NO_RETRY"), receipt)
                self.assertNotIn("credential_probe", receipt)
                self.assertNotIn("fixture-private-value", json.dumps(receipt))

    def test_credential_probe_missing_receipt_never_qualifies(self):
        endpoint = smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"]
        with tempfile.TemporaryDirectory() as tmp:
            for result in (child_result(), child_result("FAILED_STOP_NO_RETRY")):
                receipt = {"status": "FAILED_NOT_QUALIFIED"}
                with self.assertRaises(smoke.ProbeFailure):
                    smoke.check_credential_probe_receipt(Path(tmp), "missing.json", endpoint, result, receipt)
                self.assertNotIn("credential_probe", receipt)
                self.assertEqual(receipt["status"], "FAILED_NOT_QUALIFIED")

    def test_credential_probe_keeps_duplicate_key_and_selected_file_guards(self):
        endpoint = smoke.worker.K3S_REMOTE_CACHE_POLICY["endpoint"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "access.json"
            path.write_text('{"authenticated":false,"authenticated":true}')
            with self.assertRaises(ValueError):
                smoke.check_credential_probe_receipt(root, path.name, endpoint, child_result(), {})
            path.write_text("x" * 4097)
            with self.assertRaises(smoke.ProbeFailure):
                smoke.check_credential_probe_receipt(root, path.name, endpoint, child_result(), {})
            path.unlink()
            (root / "other.json").write_text(json.dumps(
                {"endpoint": endpoint, "authenticated": True, "status": 200}))
            path.symlink_to(root / "other.json")
            with self.assertRaises(OSError):
                smoke.check_credential_probe_receipt(root, path.name, endpoint, child_result(), {})


if __name__ == "__main__":
    unittest.main()
