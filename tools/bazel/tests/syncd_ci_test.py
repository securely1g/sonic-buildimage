"""Exercise the syncd contract runner and execution gate without running Bazel."""

import contextlib
from functools import partial
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("syncd_ci", ROOT / "dockers/docker-syncd-vs/bazel/ci.py")
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class ContractRunnerTest(unittest.TestCase):
    """Keep the audited target set, private query output and failure evidence intact."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "dockers/docker-syncd-vs/bazel"
        self.owner.mkdir(parents=True)
        for path in (self.root / "MODULE.bazel", self.owner.parent / "BUILD.bazel",
                     self.owner / "BUILD.bazel", self.owner / "apt.lock.json",
                     self.owner / "apt_inputs.MODULE.bazel", self.owner / "select_apt_payloads.py", self.owner / "prepare_packages.py",
                     self.owner / "runtime_package_state.json"):
            path.write_text("checked input\n")
        (self.root / ".bazelversion").write_text("8.5.1\n")
        for variant in ("docker-syncd-vs", "docker-syncd-vs-dbg"):
            manifest = self.root / "target/bazel-manifests" / variant / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n")
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
    print("bazel 8.5.1")
elif command == "aquery":
    print(json.dumps({"actions": [{"outputIds": [1], "environmentVariables": [
        {"key": "PRIVATE", "value": "private-action-data"}]}],
        "artifacts": [{"id": 1, "pathFragmentId": 1}],
        "pathFragments": [{"id": 1, "label": "unsafe.deb" if mode == "deb" else "safe.tar"}]}))
    print("action query diagnostic", file=sys.stderr)
    if mode == "query-failure": sys.exit(7)
elif command == "test":
    if mode == "test-failure": sys.exit(9)
    events = []
    for target in sys.argv[2:]:
        if not target.startswith("//"): continue
        package, name = target[2:].split(":")
        # The default symlink cannot locate tests with a Python transition.
        configuration = "default" if name == "manifest_labels_test" else "python-3.11"
        directory = root / "configured testlogs" / configuration / package / name
        directory.mkdir(parents=True)
        outputs = []
        for filename in ("test.log", "test.xml"):
            if mode == "missing-evidence" and name == "package_state_layer_test" and filename == "test.xml":
                continue
            (directory / filename).write_text("passed\\n")
            outputs.append({"name": filename, "uri": (directory / filename).as_uri()})
        events.append({"id": {"testResult": {"label": target, "configuration": {"id": configuration}}},
                       "testResult": {"testActionOutput": outputs}})
    event_path = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--build_event_json_file="))
    Path(event_path).write_text("".join(json.dumps(event) + "\\n" for event in events))
    (root / "MODULE.bazel.lock").write_text("{}\\n")
else:
    raise SystemExit("unexpected command: " + command)
''')
        self.bazel.chmod(0o755)
        self.artifacts = self.root / "artifacts"

    def run_ci(self, mode):
        (self.root / "mode").write_text(mode)
        console = io.StringIO()
        with mock.patch.object(ci, "ROOT", self.root), mock.patch.object(ci, "OWNER", self.owner), \
             mock.patch.object(ci, "execute", partial(ci.command_log.execute, cwd=self.root)), \
             mock.patch.object(ci.resolution, "collect"), mock.patch.object(ci, "check_versions"), \
             mock.patch.object(sys, "argv", ["ci.py", "--bazel", str(self.bazel),
                                            "--bazel-arg=--jobs=2", "--artifacts", str(self.artifacts)]), \
             contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            if mode == "pass":
                ci.main()
            else:
                with self.assertRaises(SystemExit) as failure:
                    ci.main()
                self.assertEqual(failure.exception.code, 1)
        self.assertFalse((self.artifacts / "actions.raw.json").exists())
        self.assertFalse((self.artifacts / "tests.raw.json").exists())
        self.assertNotIn("private-action-data", console.getvalue())
        for path in self.artifacts.rglob("*"):
            if path.is_file():
                self.assertNotIn("private-action-data", path.read_text())
        return json.loads((self.artifacts / "receipt.json").read_text())

    def test_success_retains_the_audited_tests_and_command_receipts(self):
        receipt = self.run_ci("pass")
        self.assertEqual(receipt["status"], "passed")
        commands = [json.loads(line) for line in (self.root / "commands.jsonl").read_text().splitlines()]
        self.assertEqual([command[0] for command in commands], ["--version", "aquery", "test"])
        self.assertEqual(commands[1][2], "deps(set(" + " ".join(ci.TARGETS) + "))")
        self.assertEqual(commands[2][-len(ci.TARGETS):], ci.TARGETS)
        self.assertIn("--jobs=2", commands[1])
        self.assertIn("--jobs=2", commands[2])
        report = json.loads((self.artifacts / "report.json").read_text())
        self.assertEqual(report["targets"], ci.TARGETS)
        self.assertEqual(len(report["test_outputs"]), 2 * len(ci.TARGETS))
        self.assertEqual(len(receipt["commands"]), 3)

    def test_deb_action_blocks_test_execution(self):
        receipt = self.run_ci("deb")
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("DEB or packaging wrapper", receipt["error"])
        self.assertEqual(len(receipt["commands"]), 2)
        self.assertFalse((self.artifacts / "report.json").exists())

    def test_failed_query_retains_exit_status_without_executing_tests(self):
        receipt = self.run_ci("query-failure")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual([item["returncode"] for item in receipt["commands"]], [0, 7])
        self.assertEqual((self.artifacts / "actions.log").read_text(), "action query diagnostic\n")

    def test_failed_test_retains_exit_status_without_success_report(self):
        receipt = self.run_ci("test-failure")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual([item["returncode"] for item in receipt["commands"]], [0, 0, 9])
        self.assertFalse((self.artifacts / "report.json").exists())

    def test_missing_configured_output_fails_without_success_report(self):
        receipt = self.run_ci("missing-evidence")
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("package_state_layer_test/test.xml", receipt["error"])
        self.assertFalse((self.artifacts / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
