#!/usr/bin/env python3
"""Exercise native CI ownership and Make handoff without Docker or a build."""

import argparse
from contextlib import ExitStack, redirect_stdout
import copy
import errno
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import native_build


class NativeBuildTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        (self.workspace / "rules").mkdir()
        self.image = "sha256:" + "c" * 64
        self.container = "d" * 64
        self.args = argparse.Namespace(
            workspace=self.workspace, state=self.root / "state",
            artifacts=self.workspace / "artifacts/native",
            output=self.workspace / "artifacts/native-receipt.json",
            worker_spec=self.root / "worker-spec.json",
            source_commit="b" * 40, invocation="a" * 16,
        )
        self.spec = {"worker_image": self.image, "platform": "linux/amd64", "distribution": "trixie"}
        self.args.worker_spec.write_text(json.dumps(self.spec))
        self.info = {
            "Id": self.container,
            "Config": {"Image": self.image, "Labels": {native_build.LABEL: self.args.invocation}},
            "HostConfig": {"Privileged": True, "CgroupnsMode": "private"},
            "Mounts": [{"Type": "bind", "Source": str(self.root), "Destination": str(self.root), "RW": True}],
            "State": {"Running": False, "ExitCode": 0},
        }

    def validate(self):
        with mock.patch.object(native_build, "capture", return_value=self.args.source_commit):
            return native_build.validate(self.args)

    def test_validate_requires_exact_revision_and_immutable_amd64_trixie_worker(self):
        self.assertEqual(self.validate()[-2:], (self.root, self.spec))
        with mock.patch.object(native_build, "capture", return_value="e" * 40):
            with self.assertRaisesRegex(ValueError, "revision changed"):
                native_build.validate(self.args)
        for key, value in (("worker_image", "worker:latest"), ("platform", "linux/arm64"),
                           ("distribution", "bookworm")):
            with self.subTest(key=key):
                self.args.worker_spec.write_text(json.dumps(dict(self.spec, **{key: value})))
                with self.assertRaises(ValueError):
                    self.validate()

    def test_state_and_evidence_cannot_escape_the_dedicated_build_area(self):
        cases = (
            ("state", self.root), ("state", self.workspace / "state"),
            ("state", self.root.parent / "outside-state"),
            ("artifacts", self.root.parent / "outside-artifacts"),
            ("output", self.root.parent / "outside-receipt.json"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value), mock.patch.object(self.args, name, value):
                with self.assertRaises(ValueError):
                    self.validate()
        escape = self.workspace / "external"
        escape.symlink_to(self.root.parent, target_is_directory=True)
        with mock.patch.object(self.args, "artifacts", escape / "evidence"):
            with self.assertRaises(ValueError):
                self.validate()

    def test_git_worktree_file_and_symlink_are_rejected(self):
        git = self.workspace / ".git"
        git.rmdir()
        git.write_text("gitdir: /unmounted/worktree\n")
        with self.assertRaisesRegex(ValueError, "standalone"):
            self.validate()
        git.unlink()
        external_git = self.root / "external-git"
        external_git.mkdir()
        git.symlink_to(external_git, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "standalone"):
            self.validate()

    def test_preexisting_target_including_dangling_symlink_is_rejected(self):
        target = self.workspace / "target"
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "target"):
            self.validate()
        target.rmdir()
        target.write_bytes(b"old target")
        with self.assertRaisesRegex(ValueError, "target"):
            self.validate()
        target.unlink()
        target.symlink_to(self.root / "missing-old-build", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "target"):
            self.validate()

    def test_foreign_worker_identity_image_and_mounts_are_rejected(self):
        mutations = [
            lambda info: info.update(Id="e" * 64),
            lambda info: info["Config"]["Labels"].update({native_build.LABEL: "foreign"}),
            lambda info: info["Config"].update(Image="sha256:" + "e" * 64),
            lambda info: info["HostConfig"].update(Privileged=False),
            lambda info: info["HostConfig"].update(CgroupnsMode="host"),
            lambda info: info["Mounts"][0].update(Source="/"),
            lambda info: info["Mounts"][0].update(Destination="/other-build"),
            lambda info: info["Mounts"][0].update(RW=False),
            lambda info: info["Mounts"][0].update(Type="volume"),
            lambda info: info["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock",
                                               "Destination": "/var/run/docker.sock", "RW": True}),
        ]
        for index, mutate in enumerate(mutations):
            info = copy.deepcopy(self.info)
            mutate(info)
            with self.subTest(case=index), mock.patch.object(native_build, "capture", return_value=json.dumps([info])):
                with self.assertRaises(ValueError):
                    native_build.assert_owned(self.container, self.args.invocation, self.image, self.root)

    def run_build(self, *, info=None, failure=None, provenance="current", cleanup_failure=False):
        self.commands, self.captures = [], []

        def capture(argv):
            argv = [str(value) for value in argv]
            self.captures.append(argv)
            if argv[0] == "git":
                return self.args.source_commit
            if argv[:2] == ["docker", "create"]:
                return self.container
            self.assertEqual(argv, ["docker", "inspect", self.container])
            return json.dumps([self.info if info is None else info])

        def command(argv, **kwargs):
            argv = [str(value) for value in argv]
            self.commands.append((argv, kwargs))
            if argv[:2] == ["docker", "start"]:
                if failure:
                    raise failure
                if provenance is not None:
                    path = self.workspace / "target/bazel-native/provenance.json"
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps({"source_commit": self.args.source_commit
                                               if provenance == "current" else "e" * 40}))
            if cleanup_failure and argv[:2] == ["docker", "rm"]:
                raise RuntimeError("fixture remove failed")

        with mock.patch.object(native_build, "capture", side_effect=capture), \
                mock.patch.object(native_build, "command", side_effect=command), \
                mock.patch.object(native_build.os, "chown"), redirect_stdout(io.StringIO()):
            result = native_build.build(self.args)
        return result, json.loads(self.args.output.read_text())

    def test_success_requires_matching_provenance_and_only_mounts_build_area(self):
        result, receipt = self.run_build()
        self.assertEqual((result, receipt["status"], receipt["worker_removed"]), (0, "passed", True))
        create = next(argv for argv in self.captures if argv[:2] == ["docker", "create"])
        self.assertEqual(create[create.index("--mount") + 1], "type=bind,src=" + str(self.root) + ",dst=" + str(self.root))
        self.assertEqual(create.count("--mount"), 1)
        self.assertIn(self.image, create)
        self.assertIn("--cgroupns=private", create)
        self.assertIn("SONIC_NATIVE_CI_INVOCATION=" + self.args.invocation, create)
        self.assertFalse(any("docker.sock" in item or item in ("-v", "--volume", "--network=host", "--pid=host") for item in create))
        self.assertEqual([argv for argv, _ in self.commands], [
            ["docker", "start", "--attach", self.container], ["docker", "rm", self.container]])

    def test_failed_build_stops_and_removes_only_its_exact_worker(self):
        running = copy.deepcopy(self.info)
        running["State"]["Running"] = True
        failure = subprocess.CalledProcessError(2, ["docker", "start", "--attach", self.container])
        result, receipt = self.run_build(info=running, failure=failure)
        self.assertEqual((result, receipt["status"], receipt["worker_removed"]), (1, "failed", True))
        self.assertEqual(receipt["error_type"], "CalledProcessError")
        self.assertEqual(self.commands[-2:], [
            (["docker", "stop", "--time", "45", self.container], {"timeout": 60}),
            (["docker", "rm", self.container], {"timeout": 30})])
        self.assertFalse((self.workspace / "target").exists())

    def test_foreign_worker_is_never_started_stopped_or_removed(self):
        foreign = copy.deepcopy(self.info)
        foreign["Config"]["Labels"][native_build.LABEL] = "someone-else"
        result, receipt = self.run_build(info=foreign)
        self.assertEqual((result, receipt["status"]), (1, "failed"))
        self.assertEqual(self.commands, [])
        self.assertNotIn("worker_removed", receipt)
        self.assertIn("unrelated", receipt["worker_cleanup_error"])

    def test_nonzero_worker_exit_cannot_publish_success(self):
        failed = copy.deepcopy(self.info)
        failed["State"]["ExitCode"] = 3
        result, receipt = self.run_build(info=failed)
        self.assertEqual((result, receipt["status"]), (1, "failed"))
        self.assertIn("did not complete", receipt["error"])
        self.assertTrue(receipt["worker_removed"])

    def test_missing_provenance_cannot_publish_success(self):
        result, receipt = self.run_build(provenance=None)
        self.assertEqual(result, 1)
        self.assertIn("omitted its source provenance", receipt["error"])

    def test_wrong_provenance_cannot_publish_success(self):
        result, receipt = self.run_build(provenance="old")
        self.assertEqual(result, 1)
        self.assertIn("different source commit", receipt["error"])

    def test_cleanup_failure_overrides_build_success(self):
        result, receipt = self.run_build(cleanup_failure=True)
        self.assertEqual((result, receipt["status"]), (1, "failed"))
        self.assertIn("fixture remove failed", receipt["worker_cleanup_error"])
        self.assertNotIn("worker_removed", receipt)

    def run_inside(self, *, fail_stage=None, daemon_exit=None, wait_timeout=False, extra_env=None,
                   trust_bundle=None, preflight_failure=None):
        self.args.state.mkdir()
        self.args.artifacts.mkdir(parents=True)
        fake_root = self.root / "worker-root"
        (fake_root / "etc/sudoers.d").mkdir(parents=True)
        (fake_root / ".dockerenv").touch()
        if trust_bundle is not None:
            trust = fake_root / "usr/local/share/sonic-build-trust"
            trust.mkdir(parents=True)
            (trust / "ca-bundle.pem").write_bytes(trust_bundle)
            (trust / "receipt.json").write_text(json.dumps({
                "enabled": True, "sha256": "e" * 64, "certificate_count": 1,
                "installed_bundle_sha256": hashlib.sha256(trust_bundle).hexdigest(),
            }))
        self.make_calls = []
        daemon = mock.Mock()
        daemon.poll.return_value = daemon_exit
        if wait_timeout:
            daemon.wait.side_effect = [subprocess.TimeoutExpired("dockerd", 30), 0]

        def worker_path(value):
            # Only rewrite worker-internal absolute paths. No test touches the
            # real machine's /run or /etc, even when executed by root in CI.
            path = Path(value)
            if path.is_absolute():
                return fake_root / path.relative_to("/")
            return path

        def command(argv, **kwargs):
            self.make_calls.append((argv, kwargs))
            if argv[-1] == fail_stage:
                raise subprocess.CalledProcessError(2, argv)

        environment = {"SONIC_NATIVE_CI_INVOCATION": self.args.invocation, **(extra_env or {})}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(native_build.os.environ, environment, clear=True))
            stack.enter_context(mock.patch.object(native_build.os, "geteuid", return_value=0))
            stack.enter_context(mock.patch.object(native_build.os, "chown"))
            stack.enter_context(mock.patch.object(native_build, "Path", side_effect=worker_path))
            stack.enter_context(mock.patch.object(native_build, "prepare_cgroups", return_value={"version": 2}))
            self.preflight = stack.enter_context(mock.patch.object(
                native_build, "daemon_preflight", side_effect=preflight_failure))
            stack.enter_context(mock.patch.object(native_build.pwd, "getpwuid", return_value=SimpleNamespace(pw_name=native_build.USER)))
            popen = stack.enter_context(mock.patch.object(native_build.subprocess, "Popen", return_value=daemon))
            probe = stack.enter_context(mock.patch.object(native_build.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
            stack.enter_context(mock.patch.object(native_build, "capture", return_value="1704067200"))
            stack.enter_context(mock.patch.object(native_build, "command", side_effect=command))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if fail_stage or preflight_failure:
                with self.assertRaises(subprocess.CalledProcessError):
                    native_build.inside(self.args)
            elif daemon_exit is not None:
                with self.assertRaisesRegex(RuntimeError, "exited during startup"):
                    native_build.inside(self.args)
            else:
                native_build.inside(self.args)
        return daemon, popen, probe, fake_root

    def test_private_daemon_and_source_build_recipe_are_used_for_all_stages(self):
        daemon, popen, probe, fake_root = self.run_inside()
        socket = "unix://" + str(fake_root / "run/sonic-native-ci/docker.sock")
        self.assertIn("--host=" + socket, popen.call_args.args[0])
        self.assertIn("--data-root=" + str(self.args.state / "docker-data"), popen.call_args.args[0])
        self.assertIn("--exec-opt=native.cgroupdriver=cgroupfs", popen.call_args.args[0])
        self.preflight.assert_called_once_with(self.args, probe.call_args.kwargs["env"])
        self.assertEqual(probe.call_args.kwargs["env"]["DOCKER_HOST"], socket)
        self.assertEqual([argv[-1] for argv, _ in self.make_calls], ["init", "configure", "bazel-vs-native-inputs"])
        flags = {
            "BLDENV=trixie", "PLATFORM_ARCH=amd64", "USERNAME=admin",
            "BAZEL_MIN_READINESS=bazel_disabled", "KERNEL_PROCURE_METHOD=build",
            "ENABLE_DOCKER_BASE_PULL=n", "SONIC_DPKG_CACHE_METHOD=none",
            "DEFAULT_CONTAINER_REGISTRY=docker.io",
            "MIRROR_URLS=http://deb.debian.org/debian/",
            "MIRROR_SECURITY_URLS=http://deb.debian.org/debian-security/",
            "SONIC_DPKG_CACHE_SOURCE=" + str(self.args.state / "native-cache"),
            "SONIC_DPKG_CACHE_METHOD_OVERRIDE=none", "SONIC_CONFIG_USE_DOCKER_CACHE=n",
            "SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=n", "ENABLE_SBOM=n",
            "ENABLE_IMAGE_SIGNATURE=n", "SONIC_BUILD_JOBS=1",
            "SOURCE_DATE_EPOCH=1704067200", "BUILD_TIMESTAMP=20240101.000000", "BUILD_NUMBER=0",
            "SONIC_BUILD_SLAVE_CA_BUNDLE=",
        }
        for argv, kwargs in self.make_calls:
            self.assertEqual(argv[:8], ["runuser", "--preserve-environment", "--user", native_build.USER, "--", "make", "-f", "Makefile.work"])
            self.assertTrue(flags.issubset(argv), flags - set(argv))
            # Only configure receives PLATFORM. The native goal consumes the
            # resulting .platform file through the existing Make contract.
            self.assertEqual([value for value in argv if value.startswith("PLATFORM=")],
                             ["PLATFORM=vs" if argv[-1] == "configure" else "PLATFORM="])
            self.assertEqual(kwargs["cwd"], self.workspace)
            self.assertEqual(kwargs["env"]["DOCKER_HOST"], socket)
            self.assertEqual(kwargs["env"]["HOME"], "/home/" + native_build.USER)
        self.assertEqual((self.workspace / "rules/config.user").read_text(),
                         "SONIC_CONFIG_MAKE_JOBS = 2\n"
                         "export MONIT_SOURCE_METHOD = debian\n"
                         "export RASDAEMON_SOURCE_METHOD = debian\n")
        daemon.terminate.assert_called_once_with()
        daemon.wait.assert_called_once_with(timeout=30)
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)

    def test_inherited_docker_context_cannot_redirect_private_daemon_clients(self):
        foreign = {"DOCKER_CONTEXT": "host-context", "DOCKER_HOST": "tcp://foreign:2375",
                   "DOCKER_CONFIG": "/foreign/config", "DOCKER_TLS_VERIFY": "1",
                   "DOCKER_CERT_PATH": "/foreign/certs", "DOCKER_API_VERSION": "1.12",
                   "DOCKER_DEFAULT_PLATFORM": "linux/arm64"}
        foreign.update({key: "/foreign/certs" for key in (
            "SONIC_BUILD_SLAVE_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "CURL_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE", "PIP_CERT", "WGETRC")})
        _, _, probe, _ = self.run_inside(extra_env=foreign)
        for environment in [probe.call_args.kwargs["env"], *[kwargs["env"] for _, kwargs in self.make_calls]]:
            self.assertTrue(environment["DOCKER_HOST"].startswith("unix://"))
            for key in foreign.keys() - {"DOCKER_HOST"}:
                self.assertNotIn(key, environment)

    def test_declared_worker_trust_is_forwarded_only_to_slave_option(self):
        bundle = b"fixture combined trust bytes"
        _, _, _, fake_root = self.run_inside(trust_bundle=bundle)
        bundle_path = fake_root / "usr/local/share/sonic-build-trust/ca-bundle.pem"
        for argv, _ in self.make_calls:
            self.assertIn("SONIC_BUILD_SLAVE_CA_BUNDLE=" + str(bundle_path), argv)
            self.assertNotIn(bundle.decode(), " ".join(argv))
        receipt = json.loads((self.args.artifacts / "execution-trust.json").read_text())
        self.assertEqual(receipt["installed_bundle_sha256"], hashlib.sha256(bundle).hexdigest())
        self.assertNotIn(bundle.decode(), json.dumps(receipt))

    def test_worker_trust_rejects_unrecorded_or_modified_bundle(self):
        trust = self.root / "trust"
        trust.mkdir()
        bundle, receipt = trust / "ca-bundle.pem", trust / "receipt.json"
        bundle.write_bytes(b"unexpected trust")
        with mock.patch.object(native_build, "Path", return_value=trust):
            with self.assertRaisesRegex(ValueError, "no receipt"):
                native_build.execution_trust()
            receipt.write_text(json.dumps({"enabled": False}))
            with self.assertRaisesRegex(ValueError, "disabled"):
                native_build.execution_trust()
            receipt.write_text(json.dumps({"enabled": True, "sha256": "a" * 64,
                                          "installed_bundle_sha256": "b" * 64,
                                          "certificate_count": 1}))
            with self.assertRaisesRegex(ValueError, "differs"):
                native_build.execution_trust()

    def test_failed_make_stops_private_daemon_and_does_not_start_later_stages(self):
        daemon, popen, _, _ = self.run_inside(fail_stage="configure", wait_timeout=True)
        self.assertEqual([argv[-1] for argv, _ in self.make_calls], ["init", "configure"])
        daemon.terminate.assert_called_once_with()
        daemon.kill.assert_called_once_with()
        self.assertEqual(daemon.wait.call_args_list, [mock.call(timeout=30), mock.call()])
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)

    def test_dead_private_daemon_never_runs_make(self):
        daemon, _, probe, _ = self.run_inside(daemon_exit=1)
        self.assertEqual(self.make_calls, [])
        probe.assert_not_called()
        self.preflight.assert_not_called()
        daemon.terminate.assert_called_once_with()

    def test_preflight_failure_stops_daemon_before_any_source_configuration(self):
        failure = subprocess.CalledProcessError(125, ["docker", "run"])
        daemon, _, _, _ = self.run_inside(preflight_failure=failure)
        self.assertEqual(self.make_calls, [])
        self.assertFalse((self.workspace / "rules/config.user").exists())
        daemon.terminate.assert_called_once_with()
        daemon.wait.assert_called_once_with(timeout=30)

    def exercise_preflight(self, fail_at=None):
        self.args.state.mkdir()
        self.args.artifacts.mkdir(parents=True)
        calls = []

        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[0] == "ldd":
                return SimpleNamespace(stdout="libc.so.6 => /lib/libc.so.6 (0x1)\n/lib64/ld.so (0x2)\n")
            if len(calls) - 1 == fail_at:
                raise subprocess.CalledProcessError(1, argv)
            if "run" in argv:
                (self.args.state / "docker-preflight/scratch/result").write_text("native-preflight-ok\n")
            return SimpleNamespace(stdout="")

        with mock.patch.object(native_build, "command", side_effect=command), \
                mock.patch.object(native_build.os, "chown"), \
                mock.patch.object(native_build.shutil, "copy2"), \
                mock.patch.object(Path, "is_file", return_value=True):
            if fail_at:
                with self.assertRaises(subprocess.CalledProcessError):
                    native_build.daemon_preflight(self.args, {"DOCKER_HOST": "unix:///private/docker.sock"})
            else:
                native_build.daemon_preflight(self.args, {"DOCKER_HOST": "unix:///private/docker.sock"})
        return calls

    def test_preflight_uses_nonroot_private_daemon_and_actual_slave_limits(self):
        calls = self.exercise_preflight()
        docker = [argv for argv, _ in calls[1:]]
        for argv, kwargs in calls[1:]:
            self.assertEqual(argv[:6], ["runuser", "--preserve-environment", "--user", "sonicnative", "--", "docker"])
            self.assertEqual(kwargs["env"], {"DOCKER_HOST": "unix:///private/docker.sock"})
            self.assertLessEqual(kwargs["timeout"], 180)
        self.assertEqual(docker[0][-2:], ["buildx", "version"])
        self.assertIn("--load", docker[1])
        self.assertIn("--network=none", docker[1])
        context = self.args.state / "docker-preflight"
        self.assertEqual((context / "Dockerfile").read_text(),
                         'FROM scratch\nCOPY rootfs/ /\nRUN ["/bin/busybox", "true"]\n')
        nested = docker[2]
        for flag in ("--privileged", "--init", "--memory=14g", "--memory-swap=14g", "nofile=524288:524288"):
            self.assertIn(flag, nested)
        self.assertEqual(nested[nested.index("--mount") + 1],
                         "type=bind,src=" + str(context / "scratch") + ",dst=/probe")
        self.assertEqual(docker[-1][-3:], ["image", "rm", "sonic-native-preflight:" + self.args.invocation])
        receipt = json.loads((self.args.artifacts / "docker-preflight.json").read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["buildkit_run"] and receipt["writable_bind"])

    def test_missing_buildx_fails_without_build_or_nested_run(self):
        calls = self.exercise_preflight(fail_at=1)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.args.artifacts / "docker-preflight.json").exists())

    def test_buildkit_execution_failure_cannot_reach_nested_run(self):
        calls = self.exercise_preflight(fail_at=2)
        self.assertEqual(len(calls), 3)
        self.assertFalse((self.args.artifacts / "docker-preflight.json").exists())

    def test_nested_cgroup_failure_cannot_publish_preflight_success(self):
        calls = self.exercise_preflight(fail_at=3)
        self.assertEqual(len(calls), 4)
        self.assertFalse((self.args.artifacts / "docker-preflight.json").exists())


class CgroupDelegationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cgroup = self.root / "sys/fs/cgroup"
        self.cgroup.mkdir(parents=True)
        (self.root / "proc/self").mkdir(parents=True)
        (self.root / "proc/self/cgroup").write_text("0::/\n")
        for name, content in {"cgroup.type": "domain\n", "cgroup.controllers": "cpu memory pids\n",
                              "cgroup.procs": "1\n7\n", "cgroup.subtree_control": ""}.items():
            (self.cgroup / name).write_text(content)

    def prepare(self, *, busy=0):
        original_write = Path.write_text
        events = []

        def write(path, data, *args, **kwargs):
            nonlocal busy
            events.append((path.name, data))
            if path.name == "cgroup.subtree_control" and busy:
                busy -= 1
                raise OSError(errno.EBUSY, "fixture root process race")
            return original_write(path, data, *args, **kwargs)

        with mock.patch.object(native_build, "Path", side_effect=lambda name: self.root / str(name).lstrip("/")), \
                mock.patch.object(Path, "write_text", new=write), \
                mock.patch.object(native_build.time, "sleep"):
            result = native_build.prepare_cgroups()
        return result, events

    def test_processes_move_to_leaf_before_domain_controllers_are_enabled(self):
        result, events = self.prepare()
        self.assertEqual(events, [("cgroup.procs", "1"), ("cgroup.procs", "7"),
                                  ("cgroup.subtree_control", "+cpu +memory +pids")])
        self.assertEqual(result, {"version": 2, "controllers": ["cpu", "memory", "pids"],
                                  "process_leaf": "sonic-native-init"})

    def test_shared_or_already_threaded_cgroup_is_rejected_before_writes(self):
        for file, content in (("proc/self/cgroup", "0::/host.slice\n"),
                              ("sys/fs/cgroup/cgroup.type", "domain threaded\n")):
            path = self.root / file
            original = path.read_text()
            path.write_text(content)
            with self.subTest(file=file), self.assertRaisesRegex(ValueError, "fresh private domain"):
                self.prepare()
            self.assertFalse((self.cgroup / "sonic-native-init").exists())
            path.write_text(original)

    def test_missing_memory_delegation_is_rejected_before_writes(self):
        (self.cgroup / "cgroup.controllers").write_text("cpu pids\n")
        with self.assertRaisesRegex(ValueError, "lacks delegated"):
            self.prepare()
        self.assertFalse((self.cgroup / "sonic-native-init").exists())

    def test_transient_process_race_retries_but_persistent_busy_is_bounded(self):
        _, events = self.prepare(busy=1)
        self.assertEqual(sum(name == "cgroup.subtree_control" for name, _ in events), 2)

    def test_persistent_root_process_race_fails_after_five_attempts(self):
        with self.assertRaises(OSError) as raised:
            self.prepare(busy=5)
        self.assertEqual(raised.exception.errno, errno.EBUSY)

    def test_v1_hierarchy_is_left_unchanged(self):
        (self.cgroup / "cgroup.controllers").unlink()
        result, events = self.prepare()
        self.assertEqual(result, {"version": 1})
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
