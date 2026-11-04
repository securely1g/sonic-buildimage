#!/usr/bin/env python3
"""Exercise reviewed OTEL selection, transport, and native-to-loaded binding."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/otel-oci-amd64"
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", image="none", source="a" * 40,
             registry="b" * 40, skip_vs=True, number=999):
    expression = " ".join(expression.splitlines()).strip()
    if expression.startswith("${{"):
        expression = expression[3:-2].strip()
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.event.pull_request.number": str(number),
        "github.repository": repr("securely1g/sonic-buildimage"),
        "github.event_name": repr(event), "github.head_ref": repr(branch),
        "github.ref": repr("refs/heads/review"), "github.sha": repr("c" * 40),
        "inputs.source_image": repr(image), "inputs.source_revision": repr(source),
        "inputs.registry_revision": repr(registry), "inputs.skip_vs": str(skip_vs),
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    expression = re.sub(r"\bfalse\b", "False", expression)
    return eval(expression, {"__builtins__": {}}, {"fromJSON": json.loads})


def step_script(name):
    block = WORKFLOW.read_text().split("      - name: " + name + "\n", 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(block.split("        run: |\n", 1)[1])


class OtelReviewWorkflowTest(unittest.TestCase):
    def test_paired_immutable_inputs(self):
        script = step_script("Validate the reviewed OTEL source selection")
        for source, registry, valid in [("a" * 40, "b" * 40, True),
                ("master", "b" * 40, False), ("a" * 40, "", False),
                ("", "b" * 40, False), ("A" * 40, "b" * 40, False),
                ("a" * 39, "b" * 40, False), ("", "", False)]:
            with self.subTest(source=source, registry=registry):
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source, "REGISTRY_REVISION": registry})
                self.assertEqual(result.returncode, 0 if valid else 2)

    def test_actual_matrix_registry_and_checkouts_preserve_defaults(self):
        workflow = WORKFLOW.read_text()
        expression = re.search(r"^        include: (.+)$", workflow, re.MULTILINE).group(1)
        selected = evaluate(expression, event="workflow_dispatch", image="otel-hosted")
        self.assertEqual([(r["architecture"], r["runner"]) for r in selected], [("amd64", "ubuntu-24.04")])
        for event in ["pull_request", "push", "workflow_dispatch"]:
            rows = evaluate(expression, event=event, image="none", source="", registry="")
            self.assertEqual([r["architecture"] for r in rows], ["amd64", "arm64"])
            self.assertEqual(rows[0], selected[0])
        expressions = re.findall(r"^\s+SONIC_BAZEL_REGISTRY_REVISION: (.+)$", workflow, re.MULTILINE)
        self.assertEqual(len(expressions), 2)
        for expression in expressions:
            self.assertEqual(evaluate(expression, event="workflow_dispatch", image="otel-hosted"), "b" * 40)
            self.assertEqual(evaluate(expression, image="none"), "f87ec1d97b0493d895e34733ea4aae92a90cf75d")
        refs = re.findall(r"^          ref: (.+)$", workflow, re.MULTILINE)
        selected_refs = [x for x in refs if "otel-hosted" in x]
        self.assertEqual(len(selected_refs), 2)
        for expression in selected_refs:
            self.assertEqual(evaluate(expression, event="workflow_dispatch", image="otel-hosted"), "a" * 40)
            self.assertEqual(evaluate(expression, image="none"), "c" * 40)

    def test_original_otel_phases_and_owner_targets_remain(self):
        workflow = WORKFLOW.read_text()
        for phase in ["build", "validate", "incremental"]:
            self.assertIn("tools/bazel/ci/otel_image.py " + phase, workflow)
        for target in ["@otelcol_contrib//distributions/otelcol-contrib:source_contract_test",
                       "@otelcol_contrib//bazel:components_output_test",
                       "@otelcol_contrib//distributions/otelcol-contrib:package_test"]:
            self.assertIn(target, workflow)
        self.assertIn("assert len(matches) == 3", workflow)
        self.assertIn("tools/bazel/ci/otel_loaded.py --artifacts artifacts/otel", workflow)
        self.assertIn("artifacts/otel/reviewed-selection.json", workflow)

    def test_real_completed_receipt_and_loaded_tampering(self):
        create = step_script("Bind the completed OTEL artifacts to the reviewed source")
        verify = step_script("Verify the selected source against the downloaded OTEL evidence")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "tools/bazel/ci/otel_loaded.py"
            loaded.parent.mkdir(parents=True)
            loaded.write_text("# exact selected driver\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Workflow fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "fixture"], cwd=root, check=True)
            source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            artifacts = root / "artifacts/otel"
            artifacts.mkdir(parents=True)
            for name, status in [("build-receipt.json", "built"), ("validation.json", "passed"),
                                 ("public-receipt.json", "built")]:
                (artifacts / name).write_text(json.dumps({"revision": source, "status": status,
                    "invocation": {"review_registry_revision": "b" * 40}}))
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
            build = artifacts / "build-receipt.json"
            value = json.loads(build.read_text())
            value['invocation']['review_registry_revision'] = 'e' * 40
            build.write_text(json.dumps(value))
            self.assertNotEqual(run(create), 0)
            for name in ['public-receipt.json', 'validation.json']:
                p = artifacts / name
                saved = p.read_text()
                value = json.loads(saved)
                value['status'] = 'failed'
                p.write_text(json.dumps(value))
                self.assertNotEqual(run(verify), 0)
                p.write_text(saved)
            loaded.write_text("# changed source\n")
            self.assertNotEqual(run(create), 0)
            self.assertNotEqual(run(verify), 0)

    def test_exact_owned_no_deb_and_manual_job_routing(self):
        workflow = WORKFLOW.read_text()
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(evaluate(guard))
            self.assertTrue(evaluate(guard, event="workflow_dispatch", image="otel-hosted"))
            for changes in [{"branch": BRANCH + "-suffix"}, {"branch": "unrelated"},
                            {"event": "push"}]:
                self.assertFalse(evaluate(guard, **changes))
            # The existing owner-branch fixture guard is deliberately unchanged.
            self.assertTrue(evaluate(guard, head_repo="other/fork"))
        checks = workflow.split("\n  checks:\n", 1)[1]
        gate = re.search(r"^    if: (.+)$", checks, re.MULTILINE).group(1)
        self.assertFalse(evaluate(gate, event="workflow_dispatch", image="otel-hosted"))
        self.assertTrue(evaluate(gate, event="workflow_dispatch", image="none"))
        self.assertTrue(evaluate(gate, event="pull_request"))
        vs = workflow.split("\n  vs_container:\n", 1)[1]
        gate = textwrap.dedent(vs.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0])
        self.assertFalse(evaluate(gate))
        self.assertTrue(evaluate(gate, branch="unrelated"))
        for skip in [False, True]:
            self.assertFalse(evaluate(gate, event="workflow_dispatch", image="otel-hosted", skip_vs=skip))
        kernel = (ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text()
        gate = re.search(r"^    if: (.+)$", kernel, re.MULTILINE).group(1)
        self.assertFalse(evaluate(gate))
        self.assertTrue(evaluate(gate, number=6))

    def test_transport_checksum_and_actual_rc_precedence(self):
        workflow = WORKFLOW.read_text()
        original = workflow.split('      - name: Configure the verified source mirror\n', 1)[1]
        condition = re.search(r'^        if: (.+)$', original, re.MULTILINE).group(1)
        self.assertFalse(evaluate(condition, event='workflow_dispatch', image='otel-hosted'))
        self.assertTrue(evaluate(condition, image='none'))
        script = step_script("Preserve caches and verified transport for the reviewed source")
        self.assertTrue(script.startswith("python3 -B tools/bazel/ci/lldp_draft_cache.py\n"))
        body = script.split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
        # Plain configuration bytes exercise the real checksum and publication code.
        # The immutable production URL and digest are separately fixed below.
        self.assertIn("b7b92bcfa5857685ee7cd7953fdbf55264989932/ci/source_mirrors.cfg", body)
        self.assertIn("9fbb19c33cc9564d439a86dc36e0833244ccb57cfc1511ac35a2923f908dfbc9", body)
        import hashlib
        payload = b"rewrite example.invalid/(.*) https://example.invalid/$1\n"
        fixture = body.replace("9fbb19c33cc9564d439a86dc36e0833244ccb57cfc1511ac35a2923f908dfbc9", hashlib.sha256(payload).hexdigest())
        spec = importlib.util.spec_from_file_location("review_rc", ROOT / "tools/bazel/ci/review_bazel_rc.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tools/bazel').mkdir(parents=True)
            (root / 'tools/bazel/common.bazelrc').write_text('common --enable_bzlmod\ntry-import %workspace%/.bazelrc.user\n')
            home = root / 'home'
            home.mkdir()
            (home / '.bazelrc').write_text('build --disk_cache=/kept/disk\ncommon --repository_cache=/kept/repo\n')
            rc = root / '.bazelrc.user'
            original = 'common --distdir=/kept/sources\ncommon --experimental_downloader_config=/kept/original.cfg\n'
            prefix = 'import io, urllib.request\nurllib.request.urlopen = lambda *args, **kwargs: io.BytesIO(' + repr(payload) + ')\n'
            for correct in [False, True]:
                rc.write_text(original)
                executable = fixture if correct else body
                result = subprocess.run(['python3', '-c', prefix + executable], cwd=root,
                    env=dict(os.environ, RUNNER_TEMP=str(root)), capture_output=True)
                self.assertEqual(result.returncode == 0, correct)
                config = root / 'reviewed-otel-downloads.cfg'
                if not correct:
                    self.assertFalse(config.exists())
                    self.assertEqual(rc.read_text(), original)
                else:
                    self.assertEqual(config.read_bytes(), payload)
                    self.assertTrue(rc.read_text().startswith(original))
                    selected = module.configuration(root, home)
                    for setting in ['--disk_cache=/kept/disk', '--repository_cache=/kept/repo', '--distdir=/kept/sources']:
                        self.assertIn(setting, selected)
                    options = [x for x in selected.splitlines() if '--experimental_downloader_config=' in x]
                    self.assertEqual(options[-1], 'common --experimental_downloader_config=' + str(config))

    def test_disposable_space_refuses_self_hosted_and_never_touches_caches(self):
        script = step_script("Allocate source-build space on this disposable runner")
        result = subprocess.run(['bash', '-e', '-c', script], capture_output=True,
            env={'PATH': '/usr/bin:/bin', 'RUNNER_ENVIRONMENT': 'self-hosted'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('find /runner-unused/android /runner-unused/dotnet /runner-unused/ghc', script)
        self.assertIn('-mindepth 1 -maxdepth 1 -exec rm -rf -- {} +', script)
        self.assertIn('available_bytes >= 45 * 1024 * 1024 * 1024', script)
        for forbidden in ['rm -rf /home', 'rm -rf /tmp', 'rm -rf /opt/hostedtoolcache', 'bazel clean']:
            self.assertNotIn(forbidden, script)


if __name__ == '__main__':
    unittest.main()
