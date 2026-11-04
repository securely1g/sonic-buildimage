#!/usr/bin/env python3
"""Check immutable manual selections and the SNMP control PR's CI boundary."""

from pathlib import Path
import re
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/snmp-review-source-inputs"


def condition(expression, *, event="pull_request", branch=BRANCH,
              head_repo="securely1g/sonic-buildimage"):
    expression = " ".join(expression.splitlines())
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.repository": repr("securely1g/sonic-buildimage"),
        "github.event_name": repr(event), "github.head_ref": repr(branch),
        "github.ref": repr("refs/heads/review"), "inputs.skip_vs": "True",
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    return bool(eval(expression, {"__builtins__": {}}, {}))


class SnmpWorkflowTest(unittest.TestCase):
    def test_manual_selection_accepts_only_paired_immutable_shas(self):
        workflow = (ROOT / ".github/workflows/bazel-snmp.yml").read_text()
        marker = "      - name: Validate the optional reviewed source and registry selection\n"
        self.assertEqual(workflow.count(marker), 1)
        step = workflow.split(marker, 1)[1].split("\n      - ", 1)[0]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        cases = [("", "", True), ("a" * 40, "b" * 40, True),
                 ("master", "b" * 40, False), ("a" * 40, "main", False),
                 ("a" * 40, "", False), ("", "b" * 40, False),
                 ("A" * 40, "b" * 40, False), ("a" * 39, "b" * 40, False)]
        for source, registry, valid in cases:
            with self.subTest(source=source, registry=registry):
                result = subprocess.run(["bash", "-e", "-c", script],
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source,
                         "REGISTRY_REVISION": registry}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if valid else 2)

    def test_build_and_runtime_checkout_the_same_selected_source(self):
        workflow = (ROOT / ".github/workflows/bazel-snmp.yml").read_text()
        self.assertEqual(workflow.count("ref: ${{ inputs.source_revision || github.sha }}"), 2)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertIn("BAZEL: ${{ inputs.registry_revision && 'tools/bazel/ci/review_bazel.sh' || 'bazel' }}", workflow)
        self.assertIn("SONIC_BAZEL_REGISTRY_REVISION: ${{ inputs.registry_revision }}", workflow)
        for phase in ("build", "incremental"):
            self.assertIn("snmp_image.py " + phase + ' --artifacts artifacts/snmp --bazel "$BAZEL"', workflow)

    def test_only_the_owned_control_pr_skips_existing_deb_fixtures_and_vs(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(condition(guard))
            self.assertTrue(condition(guard, branch="codex/bazel-snmp"))
            for changes in ({"branch": BRANCH + "-suffix"}, {"branch": "unrelated"},
                            {"head_repo": "other/fork"}, {"event": "push"},
                            {"event": "workflow_dispatch"}):
                with self.subTest(changes=changes):
                    self.assertFalse(condition(guard, **changes))
        job = workflow.split("\n  vs_container:\n", 1)[1]
        gate = textwrap.dedent(job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0])
        self.assertFalse(condition(gate))
        self.assertTrue(condition(gate, branch=BRANCH + "-suffix"))
        self.assertTrue(condition(gate, branch="unrelated"))
        self.assertFalse(condition(gate, head_repo="other/fork"))


if __name__ == "__main__":
    unittest.main()
