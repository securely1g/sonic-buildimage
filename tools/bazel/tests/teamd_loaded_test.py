"""Guard the original Teamd loaded checks and the worker's read-only prerequisites."""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import teamd_loaded as loaded


class TeamdKernelTest(unittest.TestCase):
    def test_only_preloaded_modules_pass_without_running_commands(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(loaded.subprocess, "run") as run:
            root = Path(temporary)
            for name in ("team", "team_mode_loadbalance"):
                with self.assertRaisesRegex(ValueError, "preloaded kernel modules"):
                    loaded.kernel_preflight(root)
                (root / name).mkdir()
            result = loaded.kernel_preflight(root)
            self.assertTrue(result["read_only"])
            self.assertEqual(result["preloaded_modules"], ["team", "team_mode_loadbalance"])
            run.assert_not_called()

    def test_regular_files_do_not_satisfy_kernel_module_presence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("team", "team_mode_loadbalance"):
                (root / name).write_text("not a loaded module")
            with self.assertRaises(ValueError):
                loaded.kernel_preflight(root)

    def test_original_container_limits_capabilities_and_mounts_are_preserved(self):
        command = loaded.probe_command("unique:tag", "own-container", Path("/receipts"), Path("/redis"), debug=True)
        for option in ("--network=none", "--cap-drop=ALL", "--cap-add=NET_ADMIN", "--cap-add=NET_RAW",
                       "--security-opt=no-new-privileges", "--read-only", "--pids-limit=128",
                       "--memory=4g", "--cpus=2", "--debug"):
            self.assertIn(option, command)
        self.assertEqual([x for x in command if x.startswith("--cap-add")],
                         ["--cap-add=NET_ADMIN", "--cap-add=NET_RAW"])
        self.assertFalse(any(x.startswith("--user") or "privileged" in x or "modprobe" in x for x in command))
        mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
        self.assertIn("type=volume,dst=/run/redis", mounts)
        self.assertEqual(len(mounts), 5)
        self.assertTrue(all(x.endswith(",readonly") for x in mounts if x.startswith("type=bind")))
        self.assertIn("/run/teamd:rw,nosuid,nodev,noexec,size=16m", command)
        self.assertIn("/var/warmboot:rw,nosuid,nodev,noexec,size=16m", command)
        self.assertIn("/tmp:rw,nosuid,nodev,noexec,size=256m", command)
        self.assertEqual(command[-4:], ["/probe/runtime_probe.py", "--validation", "/probe/validation.json", "--debug"])


class TeamdLoadedTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity = {"revision": "a" * 40, "tested_revision_parents": []}
        artifacts = {}
        for name in loaded.driver.TARGETS:
            path = self.root / name
            path.write_bytes(("retained " + name).encode())
            artifacts[name] = {"target": loaded.driver.TARGETS[name],
                               "sha256": loaded.check.sha(path), "bytes": path.stat().st_size}
        self.report = {"status": "passed", "architecture": "amd64", "revision": self.identity["revision"],
                       "tested_revision_parents": self.identity["tested_revision_parents"],
                       "native": {"debug-elf": {}, "runtime-elf": {}},
                       "runtime_native": {"runtime-elf": {}}, "artifacts": artifacts}
        self.write_report()
        self.digest = "sha256:" + "b" * 64
        self.config = {"os": "linux", "architecture": "amd64", "config": {"Entrypoint": ["original"]},
                       "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "c" * 64]}}
        self.actual = {"Id": self.digest, "Config": self.config["config"], "Os": "linux", "Architecture": "amd64",
                       "RootFS": {"Type": "layers", "Layers": self.config["rootfs"]["diff_ids"]}}
        for name in loaded.driver.IMAGES:
            (self.root / (name + ".validation.json")).write_text(json.dumps({
                "status": "passed", "config_digest": self.digest, "rootfs": self.config["rootfs"]}))
        self.commands = []

    def write_report(self):
        (self.root / "validation.json").write_text(json.dumps(self.report))

    def probe(self, *args, **kwargs):
        self.commands.append(list(args))
        debug = "--debug" in args
        return json.dumps({"status": "passed", "source_elf_count": 2 if debug else 1,
                           "loader_checks": 2 if debug else 1, "debug_checks": 3 if debug else 0,
                           "teamd_control": True, "tlm_teamd": True, "startup_template": True,
                           "teamsyncd_mtu": "1450", "team_device": "PortChannel42",
                           "teamnl_kernel_mode": "loadbalance"})

    def environment(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(loaded.platform, "machine", return_value="x86_64"))
        self.fence = stack.enter_context(mock.patch.object(loaded.driver, "clean_source"))
        self.source = stack.enter_context(mock.patch.object(loaded.driver, "source_identity", return_value=self.identity))
        self.modules = stack.enter_context(mock.patch.object(loaded, "kernel_preflight", return_value={
            "status": "passed", "preloaded_modules": ["team", "team_mode_loadbalance"], "read_only": True}))
        self.load = stack.enter_context(mock.patch.object(loaded.validate_runtime, "load_archive",
                                                        return_value=(self.actual, self.config, "")))
        self.docker = stack.enter_context(mock.patch.object(loaded.validate_runtime, "docker", side_effect=self.probe))
        self.run = stack.enter_context(mock.patch.object(loaded.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)))

    def test_both_original_probes_bind_exact_images_and_cleanup_private_names(self):
        self.environment()
        result = loaded.loaded(self.root)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result["images"]), set(loaded.driver.IMAGES))
        self.assertEqual(self.fence.call_count, 2)
        self.assertEqual(self.source.call_count, 2)
        self.assertEqual(self.load.call_count, 2)
        tags = [call.args[1] for call in self.load.call_args_list]
        self.assertEqual(len(set(tags)), 2)
        self.assertTrue(all(tag.startswith("sonic-teamd-validation-") for tag in tags))
        self.assertTrue(all(command[command.index("--name") + 1].startswith("teamd-probe-") for command in self.commands))
        cleanup = [call.args[0] for call in self.run.call_args_list if call.args[0][0] == "docker"]
        self.assertEqual(len(cleanup), 4)
        self.assertEqual({tuple(command[1:3]) for command in cleanup}, {("rm", "-f"), ("image", "rm")})
        self.assertEqual(json.loads((self.root / "loaded-validation.json").read_text()), result)
        for name in loaded.driver.IMAGES:
            runtime = result["images"][name]
            self.assertTrue(runtime["config_and_layers_verified"])
            self.assertEqual(runtime["archive_sha256"], self.report["artifacts"][name + ".gz"]["sha256"])

    def test_any_retained_payload_tamper_is_rejected_before_docker(self):
        self.environment()
        (self.root / "teamd.source.tar").write_bytes(b"changed owner payload")
        with self.assertRaisesRegex(ValueError, "artifact (size )?changed"):
            loaded.loaded(self.root)
        self.load.assert_not_called()
        self.docker.assert_not_called()

    def test_artifact_target_and_size_metadata_are_bound_to_declared_payload(self):
        self.environment()
        name = "teamd.source.tar"
        original = self.report["artifacts"][name].copy()
        for change, message in (({"target": "//unrelated:payload"}, "target changed"),
                                ({"bytes": original["bytes"] + 1}, "size changed"),
                                ({"bytes": str(original["bytes"])}, "size changed")):
            with self.subTest(change=change):
                self.report["artifacts"][name] = {**original, **change}
                self.write_report()
                with self.assertRaisesRegex(ValueError, message):
                    loaded.loaded(self.root)
                self.load.assert_not_called()
                self.docker.assert_not_called()

    def test_missing_artifact_or_wrong_static_source_and_platform_are_rejected(self):
        for change in ({"revision": "d" * 40}, {"architecture": "arm64"}, {"status": "failed"}, {"artifacts": {}}):
            with self.subTest(change=change):
                report = {**self.report, **change}
                (self.root / "validation.json").write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    loaded.checked_report(self.root, self.identity)

    def test_symlinked_retained_payload_is_rejected(self):
        path = self.root / "teamd.source.tar"
        backing = self.root / "backing"
        path.rename(backing)
        path.symlink_to(backing)
        with self.assertRaisesRegex(ValueError, "invalid retained"):
            loaded.checked_report(self.root, self.identity)

    def test_missing_modules_fail_before_image_load_without_host_changes(self):
        self.environment()
        self.modules.side_effect = ValueError("preloaded kernel modules: team")
        with self.assertRaises(ValueError):
            loaded.loaded(self.root)
        self.load.assert_not_called()
        self.run.assert_not_called()

    def test_loaded_layer_mismatch_keeps_failure_receipt_and_removes_only_private_tag(self):
        self.environment()
        self.actual["RootFS"]["Layers"] = ["sha256:" + "e" * 64]
        with self.assertRaisesRegex(ValueError, "filesystem layers"):
            loaded.loaded(self.root)
        self.docker.assert_not_called()
        receipt = json.loads((self.root / "loaded-validation.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        cleanup = [call.args[0] for call in self.run.call_args_list if call.args[0][0] == "docker"]
        self.assertEqual(cleanup, [["docker", "image", "rm", self.load.call_args.args[1]]])

    def test_probe_failure_still_removes_own_container_and_image(self):
        self.environment()
        self.docker.side_effect = ValueError("original probe failed")
        with self.assertRaisesRegex(ValueError, "original probe failed"):
            loaded.loaded(self.root)
        commands = [call.args[0] for call in self.run.call_args_list]
        self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "image", "rm"] for command in commands))
        self.assertEqual(json.loads((self.root / "loaded-validation.json").read_text())["status"], "failed")

    def test_source_identity_is_checked_again_after_runtime(self):
        self.environment()
        self.source.side_effect = [self.identity, {**self.identity, "revision": "e" * 40}]
        with self.assertRaisesRegex(ValueError, "source changed"):
            loaded.loaded(self.root)

    def test_cleanup_rejects_daemon_failure_but_accepts_an_already_removed_container(self):
        self.environment()

        def missing(command, **kwargs):
            if command[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(command, 1, stderr="Error response from daemon: No such container: " + command[-1])
            return subprocess.CompletedProcess(command, 0)

        self.run.side_effect = missing
        self.assertEqual(loaded.loaded(self.root)["status"], "passed")
        self.run.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stderr="unavailable")
        with self.assertRaisesRegex(ValueError, "cleanup did not complete"):
            loaded.loaded(self.root)
        self.assertEqual(json.loads((self.root / "loaded-validation.json").read_text())["status"], "failed")

    def test_static_receipt_from_different_merge_parents_is_rejected(self):
        self.report["tested_revision_parents"] = ["e" * 40]
        (self.root / "validation.json").write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, "differs from built source"):
            loaded.checked_report(self.root, self.identity)

    def test_cleanup_timeout_retains_failure_receipt_and_attempts_other_private_cleanup(self):
        self.environment()

        def execute(command, **kwargs):
            if command[:3] == ["docker", "rm", "-f"]:
                raise subprocess.TimeoutExpired(command, 30)
            return subprocess.CompletedProcess(command, 0)

        self.run.side_effect = execute
        with self.assertRaisesRegex(ValueError, "cleanup did not complete"):
            loaded.loaded(self.root)
        result = json.loads((self.root / "loaded-validation.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertIn("cleanup_error", result)
        self.assertEqual(sum(call.args[0][:3] == ["docker", "image", "rm"]
                             for call in self.run.call_args_list), 2)

    def test_incomplete_loader_debug_or_service_proof_is_rejected(self):
        result = json.loads(self.probe("--debug"))
        for change in ({"loader_checks": 0}, {"debug_checks": 2}, {"teamd_control": False},
                       {"startup_template": False}, {"tlm_teamd": False}, {"teamsyncd_mtu": "1500"},
                       {"teamnl_kernel_mode": ""}, {"team_device": "host-interface"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                loaded.checked_probe({**result, **change}, self.report, debug=True)


if __name__ == "__main__":
    unittest.main()
