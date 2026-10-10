"""Exercise shared container CI gates and evidence using a controlled Bazel process."""

import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import command_log, container


class ContainerRunnerTest(unittest.TestCase):
    """Reject unsafe or incomplete runs while retaining useful failure evidence."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".bazelversion").write_text("8.5.1\n")
        (self.root / "checked-input.json").write_text("{}\n")
        self.bazel = self.root / "bazel"
        self.bazel.write_text("#!" + sys.executable + "\n" + '''
import json
from pathlib import Path
import sys
root = Path.cwd()
mode = (root / "mode").read_text()
command = sys.argv[1]
with (root / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if command == "--version":
    print("bazel 7.0.0" if mode == "wrong-version" else "bazel 8.5.1")
elif command == "aquery":
    action = {"outputIds": [1], "environmentVariables": [
        {"key": "PRIVATE", "value": "private-action-data"}]}
    if mode == "wrapper": action["arguments"] = ["make", "package"]
    print(json.dumps({"actions": [action],
        "artifacts": [{"id": 1, "pathFragmentId": 1}],
        "pathFragments": [{"id": 1, "label": "unsafe.deb" if mode == "deb" else "safe.tar"}]}))
    print("action query diagnostic", file=sys.stderr)
    if mode == "query-failure": sys.exit(7)
elif command == "cquery":
    target = next(arg for arg in sys.argv[2:] if arg.startswith("@"))
    if target.startswith("@sonic_swss//"):
        target = "@@sonic-swss+//" + target.split("//", 1)[1]
    print(target)
elif command == "test":
    if mode == "test-failure": sys.exit(9)
    events = []
    for target in sys.argv[2:]:
        if not target.startswith(("//", "@")): continue
        name = target.rsplit(":", 1)[1]
        configuration = "default" if name == "first_test" else "python-3.11"
        directory = root / "configured testlogs" / configuration / name
        directory.mkdir(parents=True)
        outputs = []
        for filename in ("test.log", "test.xml"):
            if mode == "missing-evidence" and name == "second_test" and filename == "test.xml":
                continue
            (directory / filename).write_text("passed\\n")
            outputs.append({"name": filename, "uri": (directory / filename).as_uri()})
        event_label = target
        if target.startswith("@sonic_swss//"):
            event_label = "@@sonic-swss+//" + target.split("//", 1)[1]
        events.append({"id": {"testResult": {"label": event_label, "configuration": {"id": configuration}}},
                       "testResult": {"testActionOutput": outputs}})
    event_path = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--build_event_json_file="))
    Path(event_path).write_text("".join(json.dumps(event) + "\\n" for event in events))
    (root / "MODULE.bazel.lock").write_text("{}\\n")
    if mode == "source-change": (root / "checked-input.json").write_text("changed\\n")
else:
    raise SystemExit("unexpected command: " + command)
''')
        self.bazel.chmod(0o755)
        self.artifacts = self.root / "artifacts"
        self.config = container.Config(
            name="sample-container", scope="Sample contract tests without image production.",
            tests=("//sample:first_test", "//sample:second_test"),
            source_files=("checked-input.json",),
        )
        self.executed = []

    def execute(self, command, directory, receipt, name, **kwargs):
        self.executed.append(list(command))
        if command[0] in ("git", "make"):
            receipt["commands"].append({"argv": command, "returncode": 0,
                                        "elapsed_seconds": 0, "log": name + ".log"})
            result = "a" * 40 + "\n" if command[0] == "git" else "prepared\n"
            (directory / (name + ".log")).write_text(result)
            return result
        return self.original_execute(command, directory, receipt, name, **kwargs)

    def run_ci(self, mode="pass", *, config=None, error=None, resolution_error=None, machine="x86_64"):
        (self.root / "mode").write_text(mode)
        console = io.StringIO()
        self.original_execute = command_log.execute
        with mock.patch.object(container.platform, "machine", return_value=machine), \
             mock.patch.object(container.platform, "freedesktop_os_release", return_value={"VERSION_CODENAME": "trixie"}), \
             mock.patch.object(command_log, "execute", side_effect=self.execute), \
             mock.patch.object(container.resolution, "collect", side_effect=resolution_error, return_value={}), \
             mock.patch.object(container, "check_versions"), \
             contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            if error is None:
                container.run(config or self.config, workspace=self.root, artifacts=self.artifacts,
                              bazel=str(self.bazel), options=["--jobs=2"])
            else:
                with self.assertRaises(error):
                    container.run(config or self.config, workspace=self.root, artifacts=self.artifacts,
                                  bazel=str(self.bazel), options=["--jobs=2"])
        self.assertFalse((self.artifacts / "actions.raw.json").exists())
        self.assertFalse((self.artifacts / "tests.raw.json").exists())
        self.assertNotIn("private-action-data", console.getvalue())
        for path in self.artifacts.rglob("*"):
            if path.is_file():
                self.assertNotIn("private-action-data", path.read_text())
        return json.loads((self.artifacts / "receipt.json").read_text())

    def bazel_commands(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_failed_without_report(self, receipt):
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse((self.artifacts / "report.json").exists())

    def test_success_retains_configured_test_outputs_and_source_identity(self):
        """Collect Python-transition evidence and bind success to the tested sources and lock."""
        receipt = self.run_ci()
        self.assertEqual(receipt["status"], "passed")
        commands = self.bazel_commands()
        self.assertEqual([command[0] for command in commands], ["--version", "aquery", "test"])
        for command in commands[1:]:
            self.assertIn("--jobs=2", command)
        self.assertIn("deps(set(" + " ".join(self.config.tests) + "))", commands[1])
        self.assertEqual(commands[2][-len(self.config.tests):], list(self.config.tests))
        self.assertIn("--nocache_test_results", commands[2])
        report = json.loads((self.artifacts / "report.json").read_text())
        self.assertEqual(report["targets"], list(self.config.tests))
        self.assertEqual(len(report["test_outputs"]), 2 * len(self.config.tests))
        self.assertEqual(report["source_hashes"]["checked-input.json"], container.sha(self.root / "checked-input.json"))
        self.assertEqual(report["module_lock_sha256"], container.sha(self.root / "MODULE.bazel.lock"))
        self.assertFalse((self.artifacts / "test-events.jsonl").exists())

    def test_deb_output_blocks_execution(self):
        """A DEB-producing graph must fail before any selected test or archive runs."""
        self.assert_failed_without_report(self.run_ci("deb", error=ValueError))
        self.assertEqual([item[0] for item in self.bazel_commands()], ["--version", "aquery"])

    def test_packaging_wrapper_blocks_execution(self):
        """Calling Make in a Bazel action must fail even when no output is named .deb."""
        self.assert_failed_without_report(self.run_ci("wrapper", error=ValueError))
        self.assertEqual([item[0] for item in self.bazel_commands()], ["--version", "aquery"])

    def test_failed_query_keeps_stderr_and_exit_status(self):
        """Retain query diagnostics while removing private graph stdout on failure."""
        receipt = self.run_ci("query-failure", error=subprocess.CalledProcessError)
        self.assert_failed_without_report(receipt)
        self.assertEqual(receipt["commands"][-1]["returncode"], 7)
        self.assertEqual((self.artifacts / "actions.log").read_text(), "action query diagnostic\n")

    def test_failed_test_does_not_publish_success(self):
        """An unsuccessful Bazel test command must leave a failed receipt and no report."""
        receipt = self.run_ci("test-failure", error=subprocess.CalledProcessError)
        self.assert_failed_without_report(receipt)
        self.assertEqual(receipt["commands"][-1]["returncode"], 9)

    def test_missing_configured_evidence_does_not_publish_success(self):
        """A zero exit code cannot replace required logs/XML for every selected test."""
        receipt = self.run_ci("missing-evidence", error=ValueError)
        self.assert_failed_without_report(receipt)
        self.assertIn("second_test/test.xml", receipt["error"])

    def test_changed_checked_source_does_not_publish_success(self):
        """A test that changes a checked source must invalidate the otherwise passing run."""
        self.assert_failed_without_report(self.run_ci("source-change", error=ValueError))

    def test_resolution_failure_does_not_publish_success(self):
        """Incomplete dependency evidence must prevent publication of a success report."""
        receipt = self.run_ci(error=ValueError, resolution_error=ValueError("incomplete graph"))
        self.assert_failed_without_report(receipt)
        self.assertIn("incomplete graph", receipt["error"])

    def test_wrong_bazel_version_blocks_action_execution(self):
        """The runner must reject a tool version different from the workspace pin."""
        self.assert_failed_without_report(self.run_ci("wrong-version", error=ValueError))
        self.assertEqual([item[0] for item in self.bazel_commands()], ["--version"])

    def test_preexisting_module_lock_blocks_action_execution(self):
        """CI must resolve dependencies from a clean generated-lock state."""
        (self.root / "MODULE.bazel.lock").write_text("{}\n")
        self.assert_failed_without_report(self.run_ci(error=ValueError))
        self.assertFalse(any(item[0] in ("aquery", "test") for item in self.bazel_commands()))

    def test_unsupported_machine_blocks_all_commands(self):
        """These native AMD64 profiles must reject execution on another architecture."""
        receipt = self.run_ci(machine="aarch64", error=ValueError)
        self.assert_failed_without_report(receipt)
        self.assertFalse(self.executed)

    def test_missing_manifest_blocks_bazel_actions(self):
        """A successful preparation command is insufficient unless every manifest exists."""
        config = replace(self.config, make_args=("MANIFEST_METADATA=sample.mk",),
                         manifests=("docker-sample", "docker-sample-dbg"))
        receipt = self.run_ci(config=config, error=ValueError)
        self.assert_failed_without_report(receipt)
        self.assertIn("missing prepared Make manifest", receipt["error"])
        self.assertTrue(any(command[0] == "make" for command in self.executed))
        self.assertFalse(any(item[0] in ("aquery", "test") for item in self.bazel_commands()))

    def test_archive_targets_share_the_execution_audit(self):
        """Audit every archive alongside tests before dispatching the shared build helper."""
        callback = mock.Mock(return_value={"programs": ["usr/bin/example"], "debug_pairs": []})
        config = replace(self.config, archives={"runtime.tar": "@owner//dist:runtime"},
                         validate_archives=callback, retain_test_events=True)
        paths = {"runtime.tar": self.artifacts / "runtime.tar"}
        with mock.patch.object(container.build, "collect_archives", return_value=paths) as collect:
            receipt = self.run_ci(config=config)
        query = next(command for command in self.bazel_commands() if command[0] == "aquery")
        self.assertIn("deps(set(" + " ".join([*config.tests, *config.archives.values()]) + "))", query)
        self.assertEqual(collect.call_args.args[:2], (self.root, self.artifacts))
        self.assertEqual(collect.call_args.args[3], config.archives)
        self.assertEqual(callback.call_args.args[:3], (self.root, paths, self.artifacts))
        self.assertEqual(receipt["validation"]["programs"], ["usr/bin/example"])
        self.assertTrue((self.artifacts / "test-events.jsonl").is_file())

    def test_nonempty_artifact_directory_is_preserved(self):
        """Refuse to overwrite evidence from an earlier run before executing a command."""
        self.artifacts.mkdir()
        original = self.artifacts / "receipt.json"
        original.write_text("previous evidence\n")
        with self.assertRaises(ValueError):
            container.run(self.config, workspace=self.root, artifacts=self.artifacts,
                          bazel=str(self.bazel), options=[])
        self.assertEqual(original.read_text(), "previous evidence\n")
        self.assertFalse(self.bazel_commands())

    def test_failed_owner_validation_does_not_publish_success(self):
        """Passing tests cannot hide invalid archives rejected by the owner's contract."""
        config = replace(self.config, archives={"runtime.tar": "//sample:runtime"},
                         validate_archives=mock.Mock(side_effect=ValueError("wrong runtime payload")))
        with mock.patch.object(container.build, "collect_archives", return_value={}):
            receipt = self.run_ci(config=config, error=ValueError)
        self.assert_failed_without_report(receipt)
        self.assertIn("wrong runtime payload", receipt["error"])

    def prepare_owner_profile(self, owner):
        config = container.load_config(ROOT / "dockers" / owner / "bazel/ci_config.py")
        for name in config.source_files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("checked source\n")
        for name in config.manifests:
            manifest = self.root / "target/bazel-manifests" / name / "manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n")
        return config

    def test_syncd_profile_prepares_manifests_and_runs_only_contracts(self):
        """The actual Syncd profile keeps DASH/FIPS inputs and never builds archives."""
        config = self.prepare_owner_profile("docker-syncd-vs")
        with mock.patch.object(container.build, "collect_archives") as collect:
            receipt = self.run_ci(config=config)
        self.assertEqual(receipt["status"], "passed")
        collect.assert_not_called()
        make = next(command for command in self.executed if command[0] == "make")
        for argument in ("MANIFEST_METADATA=platform/vs/docker-syncd-vs.mk",
                         "DEBS_PATH=target/debs/trixie", "INCLUDE_VS_DASH_SAI=y", "INCLUDE_FIPS=y"):
            self.assertIn(argument, make)
        commands = self.bazel_commands()
        self.assertEqual(commands[-1][-len(config.tests):], list(config.tests))
        report = json.loads((self.artifacts / "report.json").read_text())
        self.assertEqual(len(report["test_outputs"]), 16)
        self.assertEqual(set(report["source_hashes"]), set(config.source_files))
        self.assertFalse((self.artifacts / "test-events.jsonl").exists())

    def test_swss_profile_audits_archives_and_retains_public_collector_inputs(self):
        """The actual SWSS profile still audits all five archives and retains 12 test results."""
        config = self.prepare_owner_profile("docker-orchagent")
        validation = {"programs": ["usr/bin/orchagent"], "debug_pairs": [], "source_contract": {}}
        config = replace(config, validate_archives=mock.Mock(return_value=validation))
        paths = {name: self.artifacts / name for name in config.archives}
        with mock.patch.object(container.build, "collect_archives", return_value=paths) as collect:
            receipt = self.run_ci(config=config)
        audit = json.loads((self.artifacts / "execution-gate-audit.json").read_text())
        self.assertEqual(audit["targets"], [*config.tests, *config.archives.values()])
        self.assertEqual(len(config.tests), 12)
        self.assertEqual(set(config.archives), {
            "swss.tar", "protobuf.tar", "rdeps.tar", "config.tar", "debug-symbols.tar"})
        self.assertEqual(collect.call_args.args[3], config.archives)
        self.assertEqual(receipt["validation"], validation)
        self.assertEqual(receipt["revision"], "a" * 40)
        self.assertEqual(receipt["tests"], dict.fromkeys(config.tests, "passed"))
        self.assertTrue((self.artifacts / "test-events.jsonl").is_file())
        commands = self.bazel_commands()
        self.assertEqual([command[0] for command in commands], ["--version", "aquery", "cquery", "test"])
        self.assertIn("@sonic_swss//crates/countersyncd:common_rust_test", commands[2])
        self.assertIn("--jobs=2", commands[2])
        events = (self.artifacts / "test-events.jsonl").read_text()
        self.assertIn("@@sonic-swss+//crates/countersyncd:common_rust_test", events)
        report = json.loads((self.artifacts / "report.json").read_text())
        self.assertIn("tests/common_rust_test/test.xml", report["test_outputs"])


class OwnerConfigLoadingTest(unittest.TestCase):
    """Keep container configuration imports independent of process-global module names."""

    def test_owner_configs_load_in_either_order_without_bare_contract_imports(self):
        """SWSS must use its own package contract even if another owner was loaded first."""
        paths = [ROOT / "dockers" / name / "bazel/ci_config.py"
                 for name in ("docker-orchagent", "docker-syncd-vs")]
        sentinel = types.ModuleType("package_contract")
        sentinel.owner = "unrelated container"
        for order in (paths, paths[::-1]):
            with self.subTest(first=order[0].parent.parent.name), \
                 mock.patch.dict(sys.modules, {"package_contract": sentinel}):
                before = list(sys.path)
                modules = [container.load_module(path) for path in order]
                swss = next(module for module in modules if module.CONFIG.archives)
                self.assertEqual(Path(swss.package_contract.__file__).resolve(),
                                 paths[0].with_name("package_contract.py"))
                self.assertTrue(callable(swss.package_contract.swss_contract))
                self.assertIs(sys.modules["package_contract"], sentinel)
                self.assertEqual(sys.path, before)


class TestEvidenceTest(unittest.TestCase):
    """Resolve test evidence from BEP without accepting ambiguous or remote files."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = "//sample:test"
        self.events = self.root / "events.jsonl"
        self.outputs = self.root / "artifacts"

    def event(self, filename, uri):
        return {"id": {"testResult": {"label": self.target}},
                "testResult": {"testActionOutput": [{"name": filename, "uri": uri}]}}

    def test_nonlocal_test_output_is_rejected(self):
        """Evidence from HTTP or a file URI with a hostname must not be copied locally."""
        for uri in ("https://example.test/test.log", "file://remote/tmp/test.log"):
            with self.subTest(uri=uri):
                self.events.write_text(json.dumps(self.event("test.log", uri)) + "\n")
                with self.assertRaisesRegex(ValueError, "not a local file"):
                    container.collect_test_outputs(self.events, self.outputs, (self.target,))

    def test_two_configurations_cannot_silently_replace_evidence(self):
        """Different configured outputs for one test must fail instead of choosing one."""
        self.events.write_text("\n".join(json.dumps(self.event("test.log", (self.root / name).as_uri()))
                                         for name in ("first.log", "second.log")) + "\n")
        with self.assertRaisesRegex(ValueError, "ambiguous configured"):
            container.collect_test_outputs(self.events, self.outputs, (self.target,))

    def external_events(self, canonical):
        events = []
        for filename in ("test.log", "test.xml"):
            output = self.root / filename
            output.write_text("passed\n")
            event = self.event(filename, output.as_uri())
            event["id"]["testResult"]["label"] = canonical
            events.append(event)
        self.events.write_text("".join(json.dumps(event) + "\n" for event in events))

    def test_canonical_external_label_uses_queried_repository_identity(self):
        """Match Bazel's canonical BEP label to the requested apparent repository name."""
        target = "@sonic_swss//crates/countersyncd:common_rust_test"
        canonical = "@@sonic-swss+//crates/countersyncd:common_rust_test"
        self.external_events(canonical)
        outputs = container.collect_test_outputs(self.events, self.outputs, (target,),
                                                 labels={canonical: target})
        self.assertEqual(outputs, ["tests/common_rust_test/test.log", "tests/common_rust_test/test.xml"])

    def test_matching_suffix_from_another_repository_cannot_supply_evidence(self):
        """A foreign repository's equally named test cannot satisfy the requested SWSS test."""
        target = "@sonic_swss//crates/countersyncd:common_rust_test"
        canonical = "@@sonic-swss+//crates/countersyncd:common_rust_test"
        self.external_events("@@foreign+//crates/countersyncd:common_rust_test")
        with self.assertRaisesRegex(ValueError, "missing required test evidence"):
            container.collect_test_outputs(self.events, self.outputs, (target,),
                                           labels={canonical: target})


if __name__ == "__main__":
    unittest.main()
