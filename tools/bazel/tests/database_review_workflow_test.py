#!/usr/bin/env python3
"""Exercise the hosted Database selection, receipt binding and CI boundary."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/database-review-source-inputs"
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", image="none", source="a" * 40,
             registry="b" * 40):
    expression = " ".join(expression.splitlines()).strip()
    if expression.startswith("${{"):
        expression = expression[3:-2].strip()
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.event.pull_request.number": "999",
        "github.repository": repr("securely1g/sonic-buildimage"),
        "github.event_name": repr(event), "github.head_ref": repr(branch),
        "github.ref": repr("refs/heads/review"), "github.sha": repr("c" * 40),
        "inputs.source_image": repr(image), "inputs.source_revision": repr(source),
        "inputs.registry_revision": repr(registry), "inputs.skip_vs": "True",
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return eval(expression, {"__builtins__": {}}, {"fromJSON": json.loads, "always": lambda: True})


def step_script(workflow, name):
    step = workflow.split("      - name: " + name + "\n", 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class DatabaseReviewWorkflowTest(unittest.TestCase):
    def test_paired_immutable_inputs(self):
        script = step_script(WORKFLOW.read_text(), "Validate the reviewed Database source selection")
        for source, registry, valid in [("a" * 40, "b" * 40, True),
                ("master", "b" * 40, False), ("a" * 40, "", False),
                ("", "b" * 40, False), ("A" * 40, "b" * 40, False),
                ("a" * 39, "b" * 40, False), ("", "", False)]:
            with self.subTest(source=source, registry=registry):
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source, "REGISTRY_REVISION": registry})
                self.assertEqual(result.returncode, 0 if valid else 2)

    def test_actual_matrix_and_registry_environment_keep_other_modes(self):
        workflow = WORKFLOW.read_text()
        expression = re.search(r"^        include: (.+)$", workflow, re.MULTILINE).group(1)
        selected = evaluate(expression, event="workflow_dispatch", image="database-hosted")
        self.assertEqual([(r["architecture"], r["runner"]) for r in selected], [("amd64", "ubuntu-24.04")])
        for event, image in [("pull_request", "none"), ("push", "none"),
                             ("workflow_dispatch", "none"), ("workflow_dispatch", "sysmgr")]:
            rows = evaluate(expression, event=event, image=image)
            self.assertEqual([r["architecture"] for r in rows], ["amd64", "arm64"])
            self.assertEqual(rows[0], selected[0])
        expression = re.search(r"^      SONIC_BAZEL_REGISTRY_REVISION: (.+)$", workflow, re.MULTILINE).group(1)
        self.assertEqual(evaluate(expression, event="workflow_dispatch", image="database-hosted"), "b" * 40)
        for image in ["none", "preflight", "sysmgr"]:
            self.assertEqual(evaluate(expression, event="workflow_dispatch", image=image), "")

    def test_both_checkouts_and_all_original_database_gates(self):
        workflow = WORKFLOW.read_text()
        ref = "ref: ${{ inputs.source_image == 'database-hosted' && inputs.source_revision || github.sha }}"
        self.assertEqual(workflow.count(ref), 2)
        for phase in ["build", "validate", "incremental"]:
            self.assertIn("tools/bazel/ci/database_image.py " + phase, workflow)
        self.assertIn("tools/bazel/tests/database_make_test.py", workflow)
        self.assertIn("tools/bazel/ci/database_loaded.py --artifacts artifacts/database", workflow)
        self.assertIn("needs: [archive, database_runtime]", workflow)
        self.assertIn('test "$BUILD_RESULT" = success', workflow)
        self.assertIn('test "$RUNTIME_RESULT" = success', workflow)
        self.assertIn("artifacts/database/reviewed-selection.json", workflow)

    def test_real_completed_receipt_and_loaded_tampering(self):
        workflow = WORKFLOW.read_text()
        create = step_script(workflow, "Bind the completed Database artifacts to the reviewed source")
        verify = step_script(workflow, "Verify the selected source against the downloaded Database evidence")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "tools/bazel/ci/database_loaded.py"
            loaded.parent.mkdir(parents=True)
            loaded.write_text("# exact selected driver\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Workflow fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "fixture"], cwd=root, check=True)
            source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            artifacts = root / "artifacts/database"
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

    def test_exact_owned_no_deb_and_manual_job_routing(self):
        workflow = WORKFLOW.read_text()
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(evaluate(guard))
            self.assertTrue(evaluate(guard, event="workflow_dispatch", image="database-hosted"))
            for changes in [{"branch": BRANCH + "-suffix"}, {"branch": "unrelated"},
                            {"head_repo": "other/fork"}, {"event": "push"}]:
                self.assertFalse(evaluate(guard, **changes))
        for name in ["archive", "database_runtime", "database_acceptance", "checks"]:
            job = re.split(r"\n  [a-z_]+:\n", workflow.split("\n  " + name + ":\n", 1)[1], maxsplit=1)[0]
            gate = re.search(r"^    if: (.+)$", job, re.MULTILINE).group(1)
            self.assertEqual(bool(evaluate(gate, event="workflow_dispatch", image="database-hosted")), name != "checks")
            self.assertTrue(evaluate(gate, event="pull_request"))
        job = workflow.split("\n  vs_container:\n", 1)[1]
        gate = textwrap.dedent(job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0])
        self.assertFalse(evaluate(gate))
        self.assertTrue(evaluate(gate, branch="unrelated"))
        self.assertFalse(evaluate(gate, event="workflow_dispatch", image="database-hosted"))

    def test_kernel_guard_preserves_other_branches_and_events(self):
        workflow = (ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text()
        gate = textwrap.dedent(workflow.split("    if: >-\n", 1)[1].split("    name:", 1)[0])
        self.assertFalse(evaluate(gate))
        for changes in [{"branch": BRANCH + "-suffix"}, {"branch": "unrelated"},
                        {"head_repo": "other/fork"}, {"event": "push"}, {"event": "workflow_dispatch"}]:
            self.assertTrue(evaluate(gate, **changes))


if __name__ == "__main__":
    unittest.main()
