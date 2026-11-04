#!/usr/bin/env python3
"""Exercise the existing Sysmgr jobs' optional reviewed-source selection."""
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/sysmgr-review-source-inputs"
MODE = "sysmgr-hosted"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", repository="securely1g/sonic-buildimage",
             image="none", registry="b" * 40):
    expression = " ".join(expression.splitlines()).strip()
    if expression.startswith("${{"):
        expression = expression[3:-2].strip()
    github = SimpleNamespace(event_name=event, head_ref=branch, repository=repository,
        ref="refs/heads/master", event=SimpleNamespace(pull_request=SimpleNamespace(
            number=999, head=SimpleNamespace(ref=branch, repo=SimpleNamespace(full_name=head_repo)))))
    inputs = SimpleNamespace(source_image=image, registry_revision=registry, skip_vs=True,
                             source_inputs_run="", source_revision="a" * 40)
    needs = SimpleNamespace(source_image_inputs=SimpleNamespace(result="skipped"))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return eval(expression, {"__builtins__": {}}, {
        "github": github, "inputs": inputs, "needs": needs,
        "fromJSON": json.loads, "contains": lambda items, value: value in items,
        "always": lambda: True, "cancelled": lambda: False})


def step_script(workflow, name):
    step = workflow.split("      - name: " + name + "\n", 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def job_gate(workflow, name):
    job = re.split(r"\n  [a-z_]+:\n", workflow.split("\n  " + name + ":\n", 1)[1], maxsplit=1)[0]
    # Job-level fields are indented four spaces; nested fields remain deeper.
    match = re.search(r"^    if: (.+)$", job, re.MULTILINE)
    if match is None:
        return "True"
    if match.group(1) == ">-":
        return re.split(r"\n    \S", job[match.end():], maxsplit=1)[0]
    return match.group(1)


class SysmgrWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()

    def test_malformed_or_unpaired_sha_strings_fail_before_checkout(self):
        script = step_script(self.workflow, "Validate the optional reviewed source and registry selection")
        cases = [("a" * 40, "b" * 40, "amd64", True),
                 ("master", "b" * 40, "amd64", False),
                 ("a" * 40, "", "amd64", False), ("", "b" * 40, "amd64", False),
                 ("A" * 40, "b" * 40, "amd64", False),
                 ("a" * 39, "b" * 40, "amd64", False),
                 ("a" * 40, "b" * 40, "arm64", False), ("", "", "amd64", False)]
        for source, registry, architecture, valid in cases:
            with self.subTest(source=source, registry=registry, architecture=architecture):
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source,
                         "REGISTRY_REVISION": registry, "SELECTED_ARCHITECTURE": architecture})
                self.assertEqual(result.returncode, 0 if valid else 2, result.stderr)
        self.assertLess(self.workflow.index("Validate the optional reviewed source"),
                        self.workflow.index("      - name: Checkout repository"))

    def test_selected_manual_matrix_and_other_modes_keep_original_architectures(self):
        expression = re.search(r"^        include: (.+)$", self.workflow, re.MULTILINE).group(1)
        selected = evaluate(expression, event="workflow_dispatch", image=MODE)
        self.assertEqual([row["architecture"] for row in selected], ["amd64"])
        self.assertEqual(selected[0]["runner"], "ubuntu-24.04")
        for event, image in (("pull_request", "none"), ("push", "none"),
                             ("workflow_dispatch", "none"), ("workflow_dispatch", "sysmgr"),
                             ("workflow_dispatch", "preflight")):
            original = evaluate(expression, event=event, image=image)
            self.assertEqual([row["architecture"] for row in original], ["amd64", "arm64"])
            self.assertEqual(original[0], selected[0])

    def test_selected_manual_route_has_no_dpool_or_vs_jobs(self):
        expected = {"archive": True, "source_image_inputs": False, "source_image": False,
                    "source_worker_preflight": False, "checks": False, "vs": False, "vs_container": False}
        for job, selected in expected.items():
            with self.subTest(job=job):
                self.assertEqual(bool(evaluate(job_gate(self.workflow, job),
                    event="workflow_dispatch", image=MODE)), selected)
        runtime = self.workflow.split("\n  sysmgr_runtime:\n", 1)[1].split("\n  checks:", 1)[0]
        self.assertIn("needs: archive", runtime)
        self.assertIn("python3 -B tools/bazel/ci/sysmgr_loaded.py --artifacts artifacts/sysmgr", runtime)

    def test_native_and_loaded_use_same_selected_source(self):
        ref = "ref: ${{ inputs.source_image == 'sysmgr-hosted' && inputs.source_revision || github.sha }}"
        self.assertEqual(self.workflow.count(ref), 2)
        expression = re.search(r"^      SONIC_BAZEL_REGISTRY_REVISION: (.+)$", self.workflow, re.MULTILINE).group(1)
        self.assertEqual(evaluate(expression, event="workflow_dispatch", image=MODE), "b" * 40)
        for image in ("none", "preflight", "sysmgr", "orchagent"):
            self.assertEqual(evaluate(expression, event="workflow_dispatch", image=image), "")
        self.assertIn("tools/bazel/ci/review_bazel.sh", self.workflow)
        self.assertIn('sysmgr_image.py build --bazel "$BAZEL"', self.workflow)
        self.assertIn('sysmgr_image.py incremental --bazel "$BAZEL"', self.workflow)
        self.assertIn("submodule update --init -- src/sonic-sysmgr/gnoi", self.workflow)

    def test_exact_owned_no_deb_guards_preserve_foreign_and_suffix_events(self):
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", self.workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(evaluate(guard))
            self.assertTrue(evaluate(guard, event="workflow_dispatch", image=MODE))
            for changes in ({"branch": BRANCH + "-suffix"}, {"branch": "ordinary"},
                            {"head_repo": "other/fork"}, {"repository": "other/fork", "head_repo": "other/fork"},
                            {"event": "push"}):
                self.assertFalse(evaluate(guard, **changes))
        self.assertFalse(evaluate(job_gate(self.workflow, "vs_container")))
        self.assertTrue(evaluate(job_gate(self.workflow, "vs_container"), branch="ordinary"))
        kernel = (ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text()
        self.assertIn("if: github.event_name != 'pull_request' || github.event.pull_request.number == 6", kernel)

    def test_hosted_space_admission_rejects_low_capacity(self):
        script = step_script(self.workflow, "Allocate source-build space on this disposable runner")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, contents in {
                "find": "#!/bin/sh\nexit 0\n",
                "du": "#!/bin/sh\nexit 0\n",
                "df": '#!/bin/sh\nprintf "Available\\n%s\\n" "$AVAILABLE_BYTES"\n',
            }.items():
                path = root / name
                path.write_text(contents)
                path.chmod(0o755)
            for environment, gib, accepted in (("github-hosted", 46, True),
                                                ("github-hosted", 44, False),
                                                ("self-hosted", 46, False)):
                with self.subTest(environment=environment, gib=gib):
                    result = subprocess.run(["/bin/bash", "-e", "-c", script], capture_output=True,
                        env={"PATH": str(root) + ":/usr/bin:/bin", "RUNNER_ENVIRONMENT": environment,
                             "GITHUB_WORKSPACE": str(root), "AVAILABLE_BYTES": str(gib * 1024**3)})
                    self.assertEqual(result.returncode == 0, accepted)


    def test_real_completed_receipt_and_loaded_tampering(self):
        workflow = self.workflow
        create = step_script(workflow, "Bind the completed Sysmgr artifacts to the reviewed source")
        verify = step_script(workflow, "Verify the selected source against the downloaded Sysmgr evidence")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "tools/bazel/ci/sysmgr_loaded.py"
            loaded.parent.mkdir(parents=True)
            loaded.write_text("# exact selected driver\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Workflow fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "fixture"], cwd=root, check=True)
            source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            artifacts = root / "artifacts/sysmgr"
            artifacts.mkdir(parents=True)
            for name, status in [("build-receipt.json", "built"), ("validation.json", "passed"),
                                 ("public-receipt.json", "built")]:
                (artifacts / name).write_text(json.dumps({"revision": source, "status": status, "invocation": {"review_registry_revision": "b" * 40}}))
            env = {"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source, "REGISTRY_REVISION": "b" * 40, "GITHUB_SHA": "c" * 40}
            def run(script):
                return subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True).returncode
            self.assertEqual(run(create), 0)
            self.assertEqual(run(verify), 0)
            build = artifacts / "build-receipt.json"
            original_build = build.read_text()
            altered = json.loads(original_build)
            altered["invocation"]["review_registry_revision"] = "d" * 40
            build.write_text(json.dumps(altered))
            self.assertNotEqual(run(create), 0)
            build.write_text(original_build)
            for name, field in (("public-receipt.json", "revision"), ("validation.json", "status")):
                receipt = artifacts / name
                original_receipt = receipt.read_text()
                altered = json.loads(original_receipt)
                altered[field] = "mismatched"
                receipt.write_text(json.dumps(altered))
                self.assertNotEqual(run(verify), 0)
                receipt.write_text(original_receipt)
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
