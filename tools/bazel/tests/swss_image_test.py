#!/usr/bin/env python3
"""Check SWSS inheritance failures and the installed-file probe without Docker."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "swss_verify_image", ROOT / "dockers/docker-orchagent/bazel/verify_image.py")
subject = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(subject)


class VerificationWorkflowTest(unittest.TestCase):
    def test_private_verification_keeps_build_store_and_prepares_cgroups_first(self):
        workflow = (ROOT / ".github/workflows/bazel-swss-oci.yml").read_text()
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
            invocations = []
            for step in ("Verify inherited Common in the final SWSS images",
                         "Verify P4RT packaged split debug symbols"):
                block = workflow.split("      - name: " + step + "\n", 1)[1].split("\n      - ", 1)[0]
                script = textwrap.dedent(block.split("        run: |\n", 1)[1])
                subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                               cwd=directory, env=environment, check=True, capture_output=True)
                arguments = json.loads(captured.read_text())
                self.assertIn("sonic-slave-run", arguments)
                invocations.append(dict(argument.split("=", 1) for argument in arguments if "=" in argument))
            roots = [Path(invocation["DOCKER_ROOT"]) for invocation in invocations]
            self.assertEqual(len(set(roots)), 2)
            for root in roots:
                self.assertEqual(root.parent, directory)
                self.assertNotEqual(root.name, "fsroot.docker.trixie")
            common = invocations[0]
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


class InheritanceTest(unittest.TestCase):
    def setUp(self):
        self.images = [{"Os": "linux", "Architecture": "amd64", "RootFS": {"Layers": layers}}
                       for layers in (["base"], ["base", "swss"], ["base", "swss", "debug"])]
        self.reports = [copy.deepcopy({"var_run": "/run",
                        "common": {"sha256": "common", "mode": 0o555, "uid": 0, "gid": 0},
                        "database": {"sha256": "config", "mode": 0o644, "uid": 0, "gid": 0}})
                        for _ in self.images]

    def test_both_variants_retain_source_base(self):
        subject.verify_inheritance(self.images, self.reports)

    def test_wrong_base_or_debug_parent_fails(self):
        for index in (1, 2):
            images = copy.deepcopy(self.images)
            images[index]["RootFS"]["Layers"][0] = "unrelated-image"
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "parent's exact layers"):
                subject.verify_inheritance(images, self.reports)

    def test_reintroduced_common_or_database_files_fail(self):
        for index in (1, 2):
            for key in ("common", "database"):
                reports = copy.deepcopy(self.reports)
                reports[index][key]["sha256"] = "overwritten"
                with self.subTest(index=index, key=key), self.assertRaisesRegex(ValueError, "SWSS changed inherited"):
                    subject.verify_inheritance(self.images, reports)

    def test_database_mode_and_directory_alias_regressions_fail(self):
        for field, value in (("mode", 0o755), ("uid", 1000), ("gid", 1000)):
            reports = copy.deepcopy(self.reports)
            reports[1]["database"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "root-owned 0644"):
                subject.verify_inheritance(self.images, reports)
        self.reports[1]["var_run"] = "../elsewhere"
        with self.assertRaisesRegex(ValueError, "/var/run"):
            subject.verify_inheritance(self.images, self.reports)

    def test_real_probe_rejects_var_run_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (subject.COMMON, subject.DATABASE):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"installed payload")
            (root / "var").mkdir()
            (root / "var/run").symlink_to("/run")
            arguments = [sys.executable, "-B", "-c", subject.PROBE, str(root), subject.COMMON, subject.DATABASE]
            report = json.loads(subprocess.check_output(arguments, text=True))
            self.assertEqual(report["var_run"], "/run")
            self.assertEqual(report["common"]["sha256"], subject.validation.sha(root / subject.COMMON))
            (root / "var/run").unlink()
            (root / "var/run").mkdir()
            result = subprocess.run(arguments, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


@unittest.skipUnless(platform.machine() == "x86_64" and all(shutil.which(tool) for tool in
                        ("cc", "readelf", "objcopy")), "native AMD64 C compiler/binutils required")
class InstalledSymbolsTest(unittest.TestCase):
    setUp = InheritanceTest.setUp

    def test_actual_copied_elf_and_symbols_are_verified_and_containers_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "common.c"
            source.write_text("int inherited_common(void) { return 42; }\n")
            binary = root / "common.so"
            command = subject.command
            command(["cc", "-g", "-shared", "-fPIC", "-Wl,--build-id=sha1", str(source), "-o", str(binary)])
            identifier = re.search(r"Build ID: ([0-9a-f]+)", command(["readelf", "-n", str(binary)]))[1]
            symbols = root / (identifier[2:] + ".debug")
            command(["objcopy", "--only-keep-debug", str(binary), str(symbols)])
            command(["objcopy", "--strip-debug", "--add-gnu-debuglink=" + str(symbols), str(binary)])
            for report in self.reports:
                report["common"]["sha256"] = subject.validation.sha(binary)
            archives = [root / (tag + ".gz") for tag in subject.TAGS]
            for archive in archives:
                archive.write_bytes(b"standard Docker owns parsing")
            created = []

            def docker(arguments):
                if arguments[0] != "docker":
                    return command(arguments)
                operation = arguments[1]
                if operation == "load":
                    return "loaded"
                if operation == "image":
                    index = subject.TAGS.index(arguments[-1].removesuffix(":latest"))
                    return json.dumps([dict(self.images[index], Id="image-" + str(index))])
                if operation == "create":
                    for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                                 "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=256m"):
                        self.assertIn(flag, arguments)
                    created.append("container-" + str(len(created)))
                    return created[-1]
                index = created.index(arguments[-1]) if operation in ("start", "inspect") else None
                if operation == "start":
                    return json.dumps(self.reports[index])
                if operation == "inspect":
                    return json.dumps([{"State": {"ExitCode": 0}}])
                if operation == "cp":
                    shutil.copyfile(symbols if "/usr/lib/debug/" in arguments[2] else binary, arguments[3])
                    return ""
                self.fail(arguments)

            actual_run = subprocess.run

            def cleanup_or_tool(arguments, **kwargs):
                if arguments[:3] == ["docker", "rm", "--force"]:
                    return subprocess.CompletedProcess(arguments, 0)
                return actual_run(arguments, **kwargs)

            with mock.patch.object(subject, "command", side_effect=docker), \
                    mock.patch.object(subject.subprocess, "run", side_effect=cleanup_or_tool) as run:
                result = subject.verify(archives)
            self.assertEqual(result["debug_pairs"][0]["build_id"], identifier)
            cleaned = [call.args[0][-1] for call in run.call_args_list
                       if call.args[0][:3] == ["docker", "rm", "--force"]]
            self.assertEqual(cleaned, created)
            # Preserve the ELF/build ID but change the actual copied symbol bytes.
            # The image acceptance must reject the stale debuglink CRC and clean up.
            symbols.write_bytes(symbols.read_bytes() + b"changed debug image payload")
            created.clear()
            with mock.patch.object(subject, "command", side_effect=docker), \
                    mock.patch.object(subject.subprocess, "run", side_effect=cleanup_or_tool) as run, \
                    self.assertRaisesRegex(ValueError, "debuglink CRC differs"):
                subject.verify(archives)
            self.assertEqual([call.args[0][-1] for call in run.call_args_list
                              if call.args[0][:3] == ["docker", "rm", "--force"]], created)


if __name__ == "__main__":
    unittest.main()
