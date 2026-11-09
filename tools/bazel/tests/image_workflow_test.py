"""Run the real image-verification workflow shell against recording commands."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class VerificationWorkflowTest(unittest.TestCase):
    def test_eventd_startup_diagnostics_probe_writes_and_preserve_bazel_failure(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/bazel-eventd.yml").read_text()
        )
        steps = workflow["jobs"]["native"]["steps"]
        diagnostic = next(
            step for step in steps
            if step.get("name") == "Diagnose Bazel startup permissions"
        )
        build = next(
            step for step in steps
            if step.get("name") == "Audit, build and run all original suites with private Redis"
        )
        self.assertLess(steps.index(diagnostic), steps.index(build))
        for status in (0, 36):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "home"
                cache = home / ".cache/bazel"
                cache.mkdir(parents=True)
                retained = cache / "retained-cache"
                retained.write_text("keep")
                wrapper = root / "tools/bazel/ci/libteam_draft_bazel.sh"
                wrapper.parent.mkdir(parents=True)
                wrapper.write_text(
                    "#!/bin/bash\n"
                    'test "$*" = "info output_base" || exit 99\n'
                    'echo "fixture Bazel startup probe"\n'
                    f"exit {status}\n"
                )
                wrapper.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", diagnostic["run"]],
                    cwd=root, env=dict(os.environ, HOME=str(home), RUNNER_TEMP=str(root),
                                       BAZEL_OUTPUT_USER_ROOT=str(root / "job startup")),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertIn("Bazel cache-parent write probe passed.", result.stdout)
                self.assertIn("fixture Bazel startup probe", result.stdout)
                self.assertIn(str(cache), result.stdout)
                self.assertEqual(list(cache.iterdir()), [retained])
                self.assertEqual(retained.read_text(), "keep")

    def test_self_hosted_base_images_do_not_delete_host_sdks(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/bazel-base-images.yml").read_text()
        )
        expected = {
            "amd64": ["self-hosted", "linux", "bazel-x64"],
            "arm64": ["self-hosted", "linux", "bazel-arm64"],
        }
        for name in ("native", "runtime"):
            with self.subTest(job=name):
                job = workflow["jobs"][name]
                runners = job["strategy"]["matrix"]["include"]
                self.assertEqual(len(runners), 2)
                self.assertEqual(
                    {entry["architecture"]: entry["runner"] for entry in runners},
                    expected,
                )
                self.assertEqual(job["runs-on"], "${{ matrix.runner }}")
        native = workflow["jobs"]["native"]
        self.assertFalse(native["container"].get("volumes"))
        self.assertNotIn(
            "Allocate source-build space on this disposable runner",
            [step.get("name") for step in native["steps"]],
        )
        for step in native["steps"]:
            self.assertNotIn("/runner-unused/", step.get("run", ""))

    def test_native_package_workflows_use_cache_reachable_runners(self):
        expected = {
            "amd64": "self-hosted, linux, bazel-x64",
            "arm64": "self-hosted, linux, bazel-arm64",
        }
        for name in ("bazel-eventd.yml", "bazel-supervisor-rust.yml", "bazel-yang-models.yml"):
            with self.subTest(workflow=name):
                workflow = (ROOT / ".github/workflows" / name).read_text()
                runners = re.findall(
                    r"^          - (?:architecture|arch): (\S+)\n"
                    r"            runner: \[([^\n]+)\]$",
                    workflow, re.MULTILINE,
                )
                self.assertEqual(len(runners), 2)
                self.assertEqual({arch.lower(): labels for arch, labels in runners}, expected)
                self.assertIn("runs-on: ${{ matrix.runner }}", workflow)

    def test_static_checks_match_make_configs_and_stop_publication_on_failure(self):
        verifier = (ROOT / "tools/bazel/ci/verify_vs_oci.sh").read_text()
        script = "set -euo pipefail\n" + verifier[verifier.index("# Match the archive recipes"):].split(
            "\npython3 -B tools/bazel/ci/verify_p4rt_debug.py", 1)[0]
        with tempfile.TemporaryDirectory(prefix="image configs ") as temporary:
            directory = Path(temporary)
            (directory / "artifacts/vs").mkdir(parents=True)
            for source in ("tools/bazel/build_helpers.py", "tools/bazel/cache/credentials.py"):
                destination = directory / source
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / source, destination)
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
        self.assertIn("runs-on: [self-hosted, linux, x64, sonic-slave-container]", active)
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
