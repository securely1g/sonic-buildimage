#!/usr/bin/env python3
"""Exercise the manual parent-image selection and its no-DEB CI boundary."""
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
MODE = "swss"
BRANCH = "codex/swss-review-source-inputs"
WORKFLOW = ".github/workflows/bazel-oci.yml"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", image="none", architecture="all", registry="b" * 40):
    expression = " ".join(expression.splitlines()).strip()
    if expression.startswith("${{"):
        expression = expression[3:-2].strip()
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.event.pull_request.number": "999",
        "github.repository": repr("securely1g/sonic-buildimage"),
        "github.event_name": repr(event), "github.head_ref": repr(branch),
        "github.ref": repr("refs/heads/review"),
        "inputs.source_image": repr(image), "inputs.architecture": repr(architecture),
        "inputs.registry_revision": repr(registry),
        "inputs.skip_vs": "True",
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return eval(expression, {"__builtins__": {}}, {"fromJSON": json.loads})


def step_script(workflow, name):
    marker = "      - name: " + name + "\n"
    step = workflow.split(marker, 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class ParentWorkflowTest(unittest.TestCase):
    def test_manual_input_validation(self):
        workflow = (ROOT / WORKFLOW).read_text()
        script = step_script(workflow, "Validate the optional reviewed source and registry selection")
        cases = [("a" * 40, "b" * 40, "amd64", True),
                 ("master", "b" * 40, "amd64", False),
                 ("a" * 40, "", "amd64", False),
                 ("", "b" * 40, "amd64", False),
                 ("A" * 40, "b" * 40, "amd64", False),
                 ("a" * 39, "b" * 40, "amd64", False),
                 ("a" * 40, "b" * 40, "all", False),
                 ("", "", "all", MODE == "base")]
        for source, registry, architecture, valid in cases:
            with self.subTest(source=source, registry=registry, architecture=architecture):
                result = subprocess.run(["bash", "-e", "-c", script],
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source,
                         "REGISTRY_REVISION": registry, "SELECTED_ARCHITECTURE": architecture},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if valid else 2, result.stderr)

    def test_manual_amd64_and_original_automatic_matrix(self):
        workflow = (ROOT / WORKFLOW).read_text()
        expressions = re.findall(r"^        include: (.+)$", workflow, re.MULTILINE)
        self.assertEqual(len(expressions), 2 if MODE == "base" else 1)
        for expression in expressions:
            selected = evaluate(expression, event="workflow_dispatch", image="swss-layer-hosted", architecture="amd64")
            self.assertEqual([row["architecture"] for row in selected], ["amd64"])
            original = evaluate(expression)
            self.assertEqual([row["architecture"] for row in original], ["amd64", "arm64"])
            self.assertEqual(original[0], selected[0])
            self.assertEqual(selected[0]["runner"], "ubuntu-24.04")

    def test_same_source_in_native_and_loaded_jobs(self):
        workflow = (ROOT / WORKFLOW).read_text()
        ref = ("ref: ${{ inputs.source_revision || github.sha }}" if MODE == "base" else
               "ref: ${{ inputs.source_image == 'swss-layer-hosted' && inputs.source_revision || github.sha }}")
        self.assertEqual(workflow.count(ref), 2)
        self.assertIn("SONIC_BAZEL_REGISTRY_REVISION: ${{ inputs.source_image == 'swss-layer-hosted' && inputs.registry_revision || '' }}", workflow)
        self.assertIn("tools/bazel/ci/review_bazel.sh", workflow)
        self.assertIn("--bazel \"$BAZEL\"", workflow)
        self.assertIn("submodule update --init -- src/sonic-sysmgr/gnoi", workflow)
        if MODE == "swss":
            self.assertIn("'swss_rebuild.py' || 'swss_image_rebuild.py'", workflow)
            for label in ("//src/libteam:consumer_test", "//tools/bazel/tests:swss_common_rust_test",
                          "@sonic_swss//bazel:protobuf_runtime_test", "//tools/bazel/tests:swss_package_contract_test"):
                self.assertIn(label, workflow)
            self.assertIn('consumer-action-audit.json "${targets[@]}"', workflow)
            self.assertIn('consumer-events.jsonl" "${targets[@]}"', workflow)

    def test_registry_input_only_affects_the_matching_reviewed_checkout(self):
        workflow = (ROOT / WORKFLOW).read_text()
        expression = re.search(r"^      SONIC_BAZEL_REGISTRY_REVISION: (.+)$", workflow, re.MULTILINE).group(1)
        self.assertEqual(evaluate(expression, event="workflow_dispatch", image="swss-layer-hosted"), "b" * 40)
        for image in ("none", "preflight", "sysmgr"):
            self.assertEqual(evaluate(expression, event="workflow_dispatch", image=image), "")
        self.assertEqual(evaluate(expression), "")

    def test_exact_owned_no_deb_guards_and_manual_scope(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(evaluate(guard))
            for changes in ({"branch": BRANCH + "-suffix"}, {"branch": "unrelated"},
                            {"head_repo": "other/fork"}, {"event": "push"}):
                self.assertFalse(evaluate(guard, **changes))
            self.assertEqual(bool(evaluate(guard, event="workflow_dispatch", image="swss-layer-hosted")), MODE == "swss")
        job = workflow.split("\n  vs_container:\n", 1)[1]
        gate = textwrap.dedent(job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0])
        self.assertFalse(evaluate(gate))
        self.assertTrue(evaluate(gate, branch="unrelated"))
        self.assertFalse(evaluate(gate, event="workflow_dispatch", image="swss-layer-hosted"))


if __name__ == "__main__":
    unittest.main()
