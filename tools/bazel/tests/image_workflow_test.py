"""Run the real image-verification workflow shell against recording commands."""

import ast
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[3]


class VerificationWorkflowTest(unittest.TestCase):
    def run_native_preflight(self, revision, *, marker_mode=stat.S_IFREG | 0o444,
                             memory=20 * 1024**3, cpu="600000 100000"):
        script = (ROOT / "tools/bazel/ci/prepare_vs_slave.sh").read_text()
        source = script.split('python3 - "$artifacts/slave-preparation.json" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
        tree = ast.parse(source)
        # Execute the actual preflight with explicit host fixtures. No real
        # image, cgroup, route, account, or receipt paths are accessed.
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        marker = {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner",
                  "work_root": "/actions/_work", "base_image": "sha256:" + "a" * 64,
                  "manager_revision": revision}
        files = {"/etc/sonic-slave-github-runner.json": json.dumps(marker),
                 "/proc/self/cgroup": "0::/\n",
                 "/sys/fs/cgroup/memory.max": str(memory),
                 "/sys/fs/cgroup/cpu.max": cpu}
        receipts = []

        class FixturePath:
            def __init__(self, path):
                self.path = str(path)

            def __truediv__(self, suffix):
                return FixturePath(self.path + "/" + suffix)

            def lstat(self):
                if self.path != "/etc/sonic-slave-github-runner.json":
                    raise AssertionError("Unexpected marker path")
                return SimpleNamespace(st_mode=marker_mode, st_uid=0)

            def read_text(self):
                return files[self.path]

            def write_text(self, text):
                if self.path != "receipt.json":
                    raise AssertionError("Unexpected receipt path")
                receipts.append(json.loads(text))

        def check_output(command, *, text):
            self.assertTrue(text)
            if command == ["findmnt", "-n", "-o", "FSTYPE", "-T", "/actions"]:
                return "ext4\n"
            self.assertEqual(command, ["ip", "-j", "-4", "route", "show"])
            return "[]"

        namespace = {"Path": FixturePath, "json": json, "stat": stat, "re": re,
                     "ipaddress": ipaddress, "sys": SimpleNamespace(argv=["preflight", "receipt.json"]),
                     "subprocess": SimpleNamespace(check_output=check_output),
                     "grp": SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=998)),
                     "os": SimpleNamespace(getgroups=lambda: [998], getuid=lambda: 1000,
                                           getgid=lambda: 1000, environ={"EXPECTED_MANAGER_REVISION": "0" * 40})}
        exec(compile(tree, "prepare_vs_slave.sh:preflight", "exec"), namespace)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["image"], marker)
        return receipts[0]

    def test_native_preflight_records_each_full_manager_revision_without_pinning_it(self):
        for revision in ("1" * 40, "abcdef12" * 5):
            with self.subTest(revision=revision):
                receipt = self.run_native_preflight(revision)
                self.assertEqual(receipt["image"]["manager_revision"], revision)
                self.assertEqual(receipt["memory_limit_bytes"], 20 * 1024**3)
                self.assertEqual(receipt["package_jobs"], 4)

    def test_native_preflight_records_fleet_cgroup_limits(self):
        receipt = self.run_native_preflight("a" * 40, memory=10 * 1024**3, cpu="400000 100000")
        self.assertEqual(receipt["memory_limit_bytes"], 10 * 1024**3)
        self.assertEqual((receipt["cpu_quota"], receipt["cpu_period"]), (400000, 100000))

    def test_native_preflight_still_rejects_invalid_provenance_and_capabilities(self):
        for revision in (None, 12, "short", "a" * 39, "g" * 40, "a" * 41):
            with self.subTest(revision=revision), self.assertRaisesRegex(SystemExit, "full manager source revision"):
                self.run_native_preflight(revision)
        with self.assertRaisesRegex(SystemExit, "root-owned, read-only"):
            self.run_native_preflight("a" * 40, marker_mode=stat.S_IFREG | 0o644)
        with self.assertRaisesRegex(SystemExit, "finite positive memory"):
            self.run_native_preflight("a" * 40, memory="max")
        with self.assertRaisesRegex(SystemExit, "finite CPU quota"):
            self.run_native_preflight("a" * 40, cpu="max 100000")

    def test_native_resource_receipt_sets_memory_and_rejects_preflight_mismatch(self):
        preparation = (ROOT / "tools/bazel/ci/prepare_vs_slave.sh").read_text()
        self.assertLess(preparation.index("\nstage=resources\n"),
                        preparation.index("\nstage=kernel-sandbox\n"))
        script = preparation.split("stage=resources\n", 1)[1].split("\nstage=kernel-sandbox\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="native-resource-profile-") as temporary:
            directory = Path(temporary)
            artifacts = directory / "artifacts"
            artifacts.mkdir()
            python = directory / "python3"
            python.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
                import json, os, pathlib, sys
                if sys.argv[1:3] == ["-B", os.environ["HELPER"]]:
                    assert sys.argv[3:5] == ["--from-cgroup", "--output"]
                    pathlib.Path(sys.argv[5]).write_text(os.environ["PROFILE_RECEIPT"])
                else:
                    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
            '''))
            python.chmod(0o755)
            for name, cores, memory_gib, local_memory_mb in (("pilot", 6, 20, 12288), ("fleet", 4, 10, 7168)):
                profile = {"name": name, "cpu_cores": cores, "memory_limit_bytes": memory_gib * 1024**3,
                           "sonic_build_memory": f"{memory_gib}g", "jobs": 4,
                           "local_memory_mb": local_memory_mb}
                receipt = {"status": "configured", "resource_profile": profile,
                           "resource_limits": {"jobs": 4, "local_memory_mb": local_memory_mb}}
                preflight = {"memory_limit_bytes": memory_gib * 1024**3,
                             "cpu_quota": cores * 100000, "cpu_period": 100000}
                path = artifacts / "slave-preparation.json"
                path.write_text(json.dumps(preflight))
                environment = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                                   HELPER=str(ROOT / "tools/bazel/ci/configure_runner_cache.py"),
                                   PROFILE_RECEIPT=json.dumps(receipt), workspace=str(ROOT), artifacts=str(artifacts))
                result = subprocess.run(["bash", "-e", "-c", script + '\nprintf "%s\\n" "$resource_memory"'],
                                        env=environment, check=True, text=True, capture_output=True)
                self.assertEqual(result.stdout.strip(), f"{memory_gib}g")
                self.assertEqual(json.loads(path.read_text())["resource_profile"], profile)
                preflight["memory_limit_bytes"] = (30 - memory_gib) * 1024**3
                path.write_text(json.dumps(preflight))
                result = subprocess.run(["bash", "-e", "-c", script], env=environment,
                                        check=False, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Resource profile differs", result.stderr)
                self.assertNotIn("resource_profile", json.loads(path.read_text()))
                preflight["memory_limit_bytes"] = memory_gib * 1024**3
                path.write_text(json.dumps(preflight))
                receipt["resource_limits"]["local_memory_mb"] += 1
                environment["PROFILE_RECEIPT"] = json.dumps(receipt)
                result = subprocess.run(["bash", "-e", "-c", script], env=environment,
                                        check=False, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Resource profile differs", result.stderr)
                self.assertNotIn("resource_profile", json.loads(path.read_text()))

    def test_native_source_archive_step_keeps_inputs_without_resource_overrides(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        active = workflow.split("\n  vs_container:\n", 1)[1]
        self.assertNotIn("EXPECTED_MANAGER_REVISION", active)
        self.assertNotIn("BAZEL_REMOTE_AUTH", active)
        self.assertNotIn("--remote_cache", active)
        self.assertNotIn("--credential_helper", active)
        arguments = active.split("      BAZEL_CONTAINER_ARGS: >-\n", 1)[1].split("\n    steps:", 1)[0]
        self.assertTrue(all(value.startswith("--distdir=") for value in arguments.split()))
        block = active.split("      - name: Prepare source archives\n", 1)[1].split(
            "\n      - ", 1)[0]
        script = textwrap.dedent(block.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory(prefix="native-resource-step-") as temporary:
            directory = Path(temporary)
            (directory / "artifacts/vs").mkdir(parents=True)
            calls = directory / "calls.jsonl"
            python = directory / "python3"
            python.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                              "with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                              "print('{}')\n")
            python.chmod(0o755)
            subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=directory,
                           env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                                    CALLS=str(calls)), check=True, capture_output=True)
            self.assertEqual([json.loads(line) for line in calls.read_text().splitlines()], [
                ["-B", "tools/bazel/gzip/source_archive.py", "--archives-dir", "artifacts/vs/repository-inputs",
                 "--output", "/bazel_cache/distdir"],
            ])
            self.assertEqual(json.loads((directory / "artifacts/vs/source-download-receipt.json").read_text()), {})

    def test_static_checks_match_make_configs_and_stop_publication_on_failure(self):
        verifier = (ROOT / "tools/bazel/ci/verify_vs_oci.sh").read_text()
        script = "set -euo pipefail\n" + verifier[verifier.index("# Match the archive recipes"):].split(
            "\npython3 -B tools/bazel/ci/verify_p4rt_debug.py", 1)[0]
        with tempfile.TemporaryDirectory(prefix="image configs ") as temporary:
            directory = Path(temporary)
            (directory / "artifacts/vs").mkdir(parents=True)
            (directory / "tools/bazel").mkdir(parents=True)
            shutil.copyfile(ROOT / "tools/bazel/build_helpers.py", directory / "tools/bazel/build_helpers.py")
            calls = directory / "calls.jsonl"
            policy = directory / "policy.json"
            policy.write_text("{}\n")
            bazel = directory / "bazel"
            bazel.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
                import json, os, sys
                from pathlib import Path
                args = sys.argv[1:]
                with Path(os.environ['CALLS']).open('a') as output:
                    output.write(json.dumps(args) + '\\n')
                config = 'dash' if '--config=syncd-dash' in args else 'default'
                logs = Path('testlogs') / config
                logs.mkdir(parents=True, exist_ok=True)
                link = Path('bazel-testlogs')
                link.unlink(missing_ok=True)
                link.symlink_to(logs, target_is_directory=True)
                if args[0] == 'cquery':
                    print(os.environ['POLICY'])
                if args[0] == 'test':
                    target = next(arg.split(':')[1] for arg in args if arg.startswith('//'))
                    status = int(os.environ['SYNCD_EXIT' if config == 'dash' else 'SWSS_EXIT'])
                    report = logs / 'tools/bazel/tests' / target
                    report.mkdir(parents=True)
                    contents = json.dumps({'target': target, 'config': config, 'exit': status}) + '\\n'
                    for filename in ('test.log', 'test.xml'):
                        (report / filename).write_text(contents)
                    if not status:
                        (report / 'test.outputs').mkdir()
                        (report / 'test.outputs/validation.json').write_text(contents)
                    sys.exit(status)
                '''))
            bazel.chmod(0o755)
            for swss_exit, syncd_exit in ((0, 0), (13, 0), (0, 17), (13, 17)):
                with self.subTest(swss_exit=swss_exit, syncd_exit=syncd_exit):
                    self.check_static_workflow(directory, script, bazel, calls, policy, swss_exit, syncd_exit)

    def check_static_workflow(self, directory, script, bazel, calls, policy, swss_exit, syncd_exit):
        calls.unlink(missing_ok=True)
        for name in ("testlogs", "artifacts/vs/swss-images", "artifacts/vs/syncd-p4-images"):
            shutil.rmtree(directory / name, ignore_errors=True)
        outputs = [directory / "artifacts/vs" / (family + "-installed-images-policy.json")
                   for family in ("swss", "syncd")]
        for output in outputs:
            output.unlink(missing_ok=True)
        result = subprocess.run(["bash", "-c", script], cwd=directory,
                                env=dict(os.environ, BAZEL=str(bazel), CALLS=str(calls),
                                         POLICY=str(policy), SWSS_EXIT=str(swss_exit), SYNCD_EXIT=str(syncd_exit)),
                                capture_output=True, text=True)
        failure = syncd_exit or swss_exit
        self.assertEqual(result.returncode, failure, result.stderr)
        arguments = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(len(arguments), 2 if failure else 6)
        for invocation in arguments:
            target = next(arg for arg in invocation if arg.startswith("//"))
            self.assertEqual("--config=syncd-dash" in invocation, "syncd" in target)
        for output in outputs:
            self.assertEqual(output.exists(), failure == 0)
        for family, target, config, status in (
                ("swss-images", "swss_images_test", "default", swss_exit),
                ("syncd-p4-images", "syncd_p4_images_test", "dash", syncd_exit)):
            expected = {"target": target, "config": config, "exit": status}
            report = directory / "artifacts/vs" / family
            for filename in ("test.log", "test.xml"):
                self.assertEqual(json.loads((report / filename).read_text()), expected)
            receipt = report / "validation.json"
            self.assertEqual(receipt.is_file(), status == 0)
            if not status:
                self.assertEqual(json.loads(receipt.read_text()), expected)

    def test_private_verification_keeps_build_store_and_prepares_cgroups_first(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        with tempfile.TemporaryDirectory(prefix="swss workflow ") as temporary:
            directory = Path(temporary)
            (directory / "artifacts/vs").mkdir(parents=True)
            commands = directory / "commands"
            commands.mkdir()
            captured = directory / "arguments.json"
            make = commands / "make"
            make.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                            "from pathlib import Path\n"
                            "Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n")
            make.chmod(0o755)
            environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                               CAPTURE=str(captured), SONIC_CI_CONTAINER_LABEL="test-job=swss")
            block = workflow.split("      - name: Verify installed base, SWSS and Syncd images\n", 1)[1].split(
                "\n      - ", 1)[0]
            script = textwrap.dedent(block.split("        run: |\n", 1)[1])
            subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                           cwd=directory, env=environment, check=True, capture_output=True)
            arguments = json.loads(captured.read_text())
            self.assertIn("sonic-slave-run", arguments)
            common = dict(argument.split("=", 1) for argument in arguments if "=" in argument)
            root = Path(common["DOCKER_ROOT"])
            self.assertEqual(root.parent, directory)
            self.assertNotEqual(root.name, "fsroot.docker.trixie")
            self.assertIn("--cgroupns=private", shlex.split(common["SONIC_BUILDER_EXTRA_CMDLINE"]))
            self.assertEqual(common["SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD"], "n")

            # Execute the actual nested shell chain with recording commands. A
            # failed cgroup setup must prevent both daemon startup and probing.
            calls = directory / "calls"
            record = (f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
                      "name = Path(sys.argv[0]).name\n"
                      "stage = 'daemon' if name == 'make' else ('prepare' if any("
                      "arg.endswith('/prepare_nested_docker.py') for arg in sys.argv) else 'probe')\n"
                      "with Path(os.environ['CALLS']).open('a') as output: output.write(stage + '\\n')\n"
                      "sys.exit(int(os.environ['PREPARE_EXIT']) if stage == 'prepare' else 0)\n")
            make.write_text(record)
            python = commands / "python3"
            python.write_text(record)
            python.chmod(0o755)
            sudo = commands / "sudo"
            sudo.write_text('#!/bin/sh\nexec "$@"\n')
            sudo.chmod(0o755)
            for failure, expected in (("0", ["prepare", "daemon", "probe"]), ("23", ["prepare"])):
                calls.unlink(missing_ok=True)
                result = subprocess.run(["bash", "-c", common["SONIC_RUN_CMDS"]], cwd=directory,
                                        env=dict(environment, CALLS=str(calls), PREPARE_EXIT=failure),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, int(failure), result.stderr)
                self.assertEqual(calls.read_text().splitlines(), expected)

    def test_active_slave_runs_shared_validation_and_retains_both_static_results(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        legacy, active = workflow.split("\n  vs_container:\n", 1)
        self.assertIn("if: ${{ false }}", legacy.split("\n  vs:\n", 1)[1])
        self.assertIn("runs-on: [self-hosted, linux, x64, sonic-slave-lgh100-bazel-transparency-once]", active)
        names = [line.split(": ", 1)[1] for line in active.splitlines()
                 if line.startswith("      - name: ")]
        self.assertLess(names.index("Validate the installer and retained container pairs"),
                        names.index("Verify installed base, SWSS and Syncd images"))
        self.assertLess(names.index("Verify installed base, SWSS and Syncd images"),
                        names.index("Publish the installer and matching container archives"))
        self.assertIn("bash tools/bazel/ci/verify_vs_oci.sh make -f slave.mk", active)
        installed = active.split("      - name: Verify installed base, SWSS and Syncd images\n", 1)[1].split(
            "\n      - ", 1)[0]
        script = textwrap.dedent(installed.split("        run: |\n", 1)[1])
        self.assertNotIn("sonic-slave-run", script)
        self.assertNotIn("dockerd", script)
        with tempfile.TemporaryDirectory(prefix="active-image-validation-") as temporary:
            directory = Path(temporary)
            (directory / "artifacts/vs").mkdir(parents=True)
            (directory / "sonic-vs-slave.env").write_text("export DOCKER_HOST=unix:///worker-private.sock\n")
            python = directory / "python3"
            python.write_text(f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                              "Path('docker-call.json').write_text(json.dumps({'args': sys.argv[1:], "
                              "'docker_host': os.environ.get('DOCKER_HOST')}))\n"
                              "sys.exit(int(os.environ['PROBE_EXIT']))\n")
            python.chmod(0o755)
            # Bind the workflow's /sonic location to this test checkout.
            shell = 'cd() { test "$1" = /sonic; builtin cd "$TEST_WORKSPACE"; }\n' + script
            for status in (0, 19):
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell], cwd=directory,
                    env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                             RUNNER_TEMP=str(directory), TEST_WORKSPACE=str(directory), PROBE_EXIT=str(status)),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                called = json.loads((directory / "docker-call.json").read_text())
                self.assertEqual(called["docker_host"], "unix:///worker-private.sock")
                self.assertEqual(called["args"], ["-B", "tools/bazel/ci/validate_runtime.py",
                    "--images-policy", "artifacts/vs/swss-installed-images-policy.json",
                    "--images-policy", "artifacts/vs/syncd-installed-images-policy.json", "--target", "target",
                    "--output", "artifacts/vs/installed-images.json"])
        retained = active.split("      - name: Retain shared image validation evidence\n", 1)[1].split(
            "\n      - ", 1)[0]
        self.assertIn("if: always()", retained)
        for path in ("artifacts/vs/swss-images/", "artifacts/vs/syncd-p4-images/",
                     "artifacts/vs/*-installed-images-policy.json", "artifacts/vs/installed-images.json"):
            self.assertIn(path, retained)


if __name__ == "__main__":
    unittest.main()
