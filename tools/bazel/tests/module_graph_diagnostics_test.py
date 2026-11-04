"""Keep the actual failed graph command's evidence without accepting its failure."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def step(name):
    contents = WORKFLOW.read_text()
    marker = "      - name: " + name + "\n"
    if contents.count(marker) != 1:
        raise AssertionError("Expected exactly one workflow step: " + name)
    return contents.split(marker, 1)[1].split("\n      - ", 1)[0]


class ModuleGraphDiagnosticsTest(unittest.TestCase):
    def test_actual_collection_fails_and_retains_the_three_upload_inputs(self):
        collect = step("Retain the verified GitLab source downloads")
        upload = step("Retain failed module graph diagnostics")
        self.assertIn("        id: repository_inputs\n", collect)
        self.assertNotIn("continue-on-error", collect)
        script = textwrap.dedent(collect.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "tools/bazel/ci/resolution.py"
            helper.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "tools/bazel/ci/resolution.py", helper)
            shutil.copyfile(ROOT / ".bazelversion", root / ".bazelversion")
            lock = b'{"lockFileVersion":24,"registryFileHashes":{}}\n'
            (root / "MODULE.bazel.lock").write_bytes(lock)
            fake = root / "bazel"
            fake.write_text("#!/usr/bin/python3\n" + textwrap.dedent('''
                import json, sys
                from pathlib import Path
                args = sys.argv[1:]
                with Path("calls.jsonl").open("a") as output:
                    output.write(json.dumps(args) + "\\n")
                if args == ["--version"]:
                    print("bazel " + Path(".bazelversion").read_text().strip())
                elif args[0] == "fetch":
                    pass
                elif args[:2] == ["mod", "graph"]:
                    print(json.dumps({"key": "<root>", "root": True,
                                      "dependencies": [{"key": "fixture@1.0"}]}))
                    print("ERROR: fixture extension download failed", file=sys.stderr)
                    sys.exit(7)
                else:
                    sys.exit("Unexpected command after failed graph inspection")
            '''))
            fake.chmod(0o755)
            environment = dict(os.environ, BAZEL=str(fake), PATH="/usr/bin:/bin", PYTHONPATH=str(root))
            result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                                    cwd=root, env=environment, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unexpected module graph inspection failure", result.stderr)
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            self.assertEqual([call[0] for call in calls], ["--version", "fetch", "mod"])
            self.assertEqual((root / "MODULE.bazel.lock").read_bytes(), lock)
            artifacts = root / "artifacts/repository-inputs"
            self.assertFalse((artifacts / "receipt.json").exists())
            paths = re.search(r"          path: \|\n((?:            .+\n)+)", upload)[1]
            paths = [line.strip() for line in paths.splitlines()]
            self.assertEqual({Path(path).name for path in paths}, {
                "module-graph.json", "module-graph.stderr", "module-graph-inspection.json"})
            self.assertTrue(all((root / path).is_file() for path in paths))
            self.assertEqual(json.loads((artifacts / "module-graph.json").read_text())["dependencies"],
                             [{"key": "fixture@1.0"}])
            self.assertEqual((artifacts / "module-graph.stderr").read_text(),
                             "ERROR: fixture extension download failed\n")
            inspection = json.loads((artifacts / "module-graph-inspection.json").read_text())
            self.assertEqual(inspection, {"argv": [str(fake), *calls[-1]], "returncode": 7})

    def test_upload_runs_after_failure_only_without_replacing_verified_sources(self):
        upload = step("Retain failed module graph diagnostics")
        expression = re.search(r"^        if: (.+)$", upload, re.MULTILINE)[1]
        for outcome in ("success", "failure", "cancelled", "skipped"):
            with self.subTest(outcome=outcome):
                condition = expression.replace("always()", "True").replace(
                    "steps.repository_inputs.outcome", repr(outcome)).replace("&&", " and ")
                self.assertEqual(eval(condition, {"__builtins__": {}}, {}), outcome == "failure")
        self.assertIn("name: repository-inputs-module-graph-failure", upload)
        self.assertIn("if-no-files-found: warn", upload)
        self.assertNotIn("continue-on-error", upload)
        verified = step("Upload the source downloads for the VS job")
        self.assertIn("name: bazel-repository-inputs", verified)
        self.assertNotIn("if: always()", verified)


if __name__ == "__main__":
    unittest.main()
