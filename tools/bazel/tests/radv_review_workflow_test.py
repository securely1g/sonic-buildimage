#!/usr/bin/env python3
"""Exercise the hosted Radv selection, receipt binding and CI boundary."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def evaluate(expression, *, event="workflow_dispatch", source_image="radv", source="a" * 40, registry="b" * 40):
    expression = " ".join(expression.splitlines()).strip()
    if expression.startswith("${{"):
        expression = expression[3:-2].strip()
    values = {
        "github.event_name": repr(event), "github.sha": repr("c" * 40),
        "inputs.source_image": repr(source_image),
        "inputs.source_revision": repr(source), "inputs.registry_revision": repr(registry),
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return eval(expression, {"__builtins__": {}}, {"fromJSON": json.loads})


def step_block(workflow, name):
    return workflow.split("      - name: " + name + "\n", 1)[1].split("\n      - ", 1)[0]


def step_script(workflow, name):
    return textwrap.dedent(step_block(workflow, name).split("        run: |\n", 1)[1])


class RadvReviewWorkflowTest(unittest.TestCase):
    def test_paired_immutable_inputs(self):
        script = step_script(WORKFLOW.read_text(), "Validate the reviewed Radv source selection")
        for source, registry, valid in [("a" * 40, "b" * 40, True),
                ("master", "b" * 40, False), ("a" * 40, "", False),
                ("", "b" * 40, False), ("A" * 40, "b" * 40, False),
                ("a" * 39, "b" * 40, False), ("", "", False)]:
            with self.subTest(source=source, registry=registry):
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source, "REGISTRY_REVISION": registry})
                self.assertEqual(result.returncode, 0 if valid else 2)

    def test_runner_mount_and_registry_selection(self):
        workflow = WORKFLOW.read_text()
        native = workflow.split("\n  radv_native:\n", 1)[1].split("\n  radv_runtime:\n", 1)[0]
        runner = re.search(r"^    runs-on: (.+)$", native, re.MULTILINE).group(1)
        self.assertEqual(evaluate(runner), ["ubuntu-24.04"])
        self.assertEqual(evaluate(runner, source_image="none", source="", registry=""), ["self-hosted", "linux", "bazel-x64"])
        options = re.search(r"^      options: (.+)$", native, re.MULTILINE).group(1)
        self.assertEqual(evaluate(options, source_image="none", source="", registry=""), "")
        self.assertEqual(shlex.split(evaluate(options)), [
            "--volume", "/usr/local/lib/android:/host-unused-sdk/android",
            "--volume", "/usr/share/dotnet:/host-unused-sdk/dotnet",
            "--volume", "/opt/ghc:/host-unused-sdk/ghc",
            "--volume", "/usr/local/.ghcup:/host-unused-sdk/ghcup",
        ])
        expression = re.search(r"^      BAZEL: (.+)$", native, re.MULTILINE).group(1)
        self.assertEqual(evaluate(expression, event="workflow_dispatch"), "tools/bazel/ci/review_bazel.sh")
        self.assertEqual(evaluate(expression, source_image="none", registry="", source=""), "bazel")
        ref = re.search(r"^          ref: (.+)$", native, re.MULTILINE).group(1)
        self.assertEqual(evaluate(ref), "a" * 40)
        self.assertEqual(evaluate(ref, source_image="none", source="", registry=""), "c" * 40)
        for name in ("Check space for source image validation", "Preserve setup-bazel caches for source checks",
                     "Restore default Bazel startup ownership", "Configure shared source download mirrors",
                     "Restrict shared cache writes to master"):
            condition = re.search(r"^        if: (.+)$", step_block(native, name), re.MULTILINE).group(1)
            self.assertTrue(evaluate(condition, source_image="none", source="", registry=""))
            self.assertFalse(evaluate(condition))
        for name in ("Allocate space on this disposable hosted runner",
                     "Preserve caches and verified transport for the reviewed source",
                     "Prepare the verified compression source for the reviewed source"):
            condition = re.search(r"^        if: (.+)$", step_block(native, name), re.MULTILINE).group(1)
            self.assertFalse(evaluate(condition, source_image="none", source="", registry=""))
            self.assertTrue(evaluate(condition))

    def test_both_checkouts_and_all_original_radv_gates(self):
        workflow = WORKFLOW.read_text()
        ref = "ref: ${{ inputs.source_image == 'radv' && inputs.source_revision || github.sha }}"
        self.assertEqual(workflow.count(ref), 2)
        for phase in ["build", "validate", "incremental"]:
            self.assertIn("tools/bazel/ci/radv_image.py " + phase, workflow)
        self.assertIn("tools/bazel/tests/radv_make_test.py", workflow)
        self.assertIn("tools/bazel/ci/radv_loaded.py --artifacts artifacts/radv", workflow)
        self.assertIn("needs: [radv_native, radv_runtime]", workflow)
        self.assertIn('test "$BUILD_RESULT" = success', workflow)
        self.assertIn('test "$RUNTIME_RESULT" = success', workflow)
        self.assertIn("artifacts/radv/reviewed-selection.json", workflow)
        native = workflow.split("\n  radv_native:\n", 1)[1].split("\n  radv_runtime:\n", 1)[0]
        gate = re.search(r"^    if: (.+)$", native, re.MULTILINE).group(1)
        self.assertTrue(evaluate(gate, event="pull_request", source_image=""))
        self.assertTrue(evaluate(gate, source_image="none"))
        self.assertTrue(evaluate(gate, source_image="radv"))
        self.assertFalse(evaluate(gate, source_image="sysmgr"))
        self.assertIn("13096d6a4cf3bb7c9bad16ebde272eb48eb92d3c/tools/bazel/debian-pool.cfg", native)
        self.assertNotIn("bazel-radv-image.yml", (ROOT / ".github/workflows/README.md").read_text())

    def test_real_completed_receipt_and_loaded_tampering(self):
        workflow = WORKFLOW.read_text()
        create = step_script(workflow, "Bind the completed Radv artifacts to the reviewed source")
        verify = step_script(workflow, "Verify the selected source against the downloaded Radv evidence")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "tools/bazel/ci/radv_loaded.py"
            loaded.parent.mkdir(parents=True)
            loaded.write_text("# exact selected driver\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Workflow fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "fixture"], cwd=root, check=True)
            source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            artifacts = root / "artifacts/radv"
            artifacts.mkdir(parents=True)
            for name, status in [("build-receipt.json", "built"), ("validation.json", "passed"),
                                 ("public-receipt.json", "built")]:
                (artifacts / name).write_text(json.dumps({"revision": source, "status": status}))
            env = dict(os.environ, SOURCE_REVISION=source, REGISTRY_REVISION="b" * 40, GITHUB_SHA="c" * 40)
            def run(script):
                return subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True).returncode
            self.assertEqual(run(create), 0)
            self.assertEqual(run(verify), 0)
            path = artifacts / "reviewed-selection.json"
            original = path.read_text()
            for field in ["source_revision", "registry_revision", "control_revision", "loaded_driver_sha256"]:
                with self.subTest(field=field):
                    value = json.loads(original)
                    value[field] = "d" * len(value[field])
                    path.write_text(json.dumps(value))
                    self.assertNotEqual(run(verify), 0)
            path.write_text(original)
            loaded.write_text("# changed source\n")
            self.assertNotEqual(run(create), 0)
            self.assertNotEqual(run(verify), 0)

if __name__ == "__main__":
    unittest.main()
