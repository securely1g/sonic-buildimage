#!/usr/bin/env python3
"""Exercise the hosted Database selection, receipt binding and CI boundary."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/database-review-source-inputs"
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", image="none", source="a" * 40,
             registry="b" * 40, matrix_architecture="amd64"):
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
        "matrix.architecture": repr(matrix_architecture),
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


class ReviewedDownloadTest(unittest.TestCase):
    STEP = "Prepare the reviewed libaudit-common download"
    SOURCE = "7d33e8732b6bea282799b5517248ee390813e354"
    REGISTRY = "6a69c2d89070d4d2ffc80b1e13626da387f7ea65"

    def implementation(self):
        script = step_script(WORKFLOW.read_text(), self.STEP)
        code = script.split("python3 - <<'PYDEB'\n", 1)[1].rsplit("\nPYDEB", 1)[0]
        namespace = {"__name__": "download_test"}
        with mock.patch.object(sys, "path", [str(ROOT), *sys.path]):
            exec(compile(code, str(WORKFLOW), "exec"), namespace)
        return namespace

    def test_exact_manual_graph_only_and_before_analysis(self):
        workflow = WORKFLOW.read_text()
        step = workflow.split("      - name: " + self.STEP + "\n", 1)[1].split("\n      - ", 1)[0]
        gate = textwrap.dedent(step.split("        if: >-\n", 1)[1].split("        run:", 1)[0])
        valid = {"event": "workflow_dispatch", "image": "database-hosted", "source": self.SOURCE, "registry": self.REGISTRY}
        self.assertTrue(evaluate(gate, **valid))
        for changed in ({"event": "pull_request"}, {"event": "push"}, {"source": ""},
                        {"registry": ""}, {"source": "a5a1f8dfa22b0b99226968f8d11dfe9d654e9a97"}, {"source": "b" * 40}, {"registry": "c" * 40},
                        {"matrix_architecture": "arm64"}, {"image": "none"}, {"image": "sysmgr"}):
            self.assertFalse(evaluate(gate, **{**valid, **changed}), changed)
        self.assertLess(workflow.index("Prepare the checksum-pinned pigz source archive"), workflow.index(self.STEP))
        self.assertLess(workflow.index(self.STEP), workflow.index("Check Make OCI bases and reproducible Docker archives"))
        self.assertLess(workflow.index(self.STEP), workflow.index("Build Database runtime and debug images from owner TARs"))
        self.assertIn("artifacts/database/libaudit-common-download.json", workflow)
        self.assertIn("'libaudit-common-download.json'", workflow)

    def test_fixed_official_address_and_original_locked_identity(self):
        ns = self.implementation()
        self.assertEqual(ns["URL"], "https://snapshot.debian.org/file/d4657962ca3a903c5f26f0d7f2b1a46b5d7ec6c0")
        self.assertEqual(ns["SHA256"], "b6edcd7aa36a2a5a1abd2c52966d69a99eebdb69c8f5cb33cbf415b0875775e3")
        self.assertEqual(ns["SIZE"], 12692)
        self.assertEqual(ns["FILENAME"], "libaudit-common_4.0.2-2_all.deb")

    @contextlib.contextmanager
    def fixture(self):
        # Plain bytes test download integrity; no Debian package is produced.
        payload = b"ordinary download test bytes"
        ns = self.implementation()
        ns["SIZE"] = len(payload)
        ns["SHA256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            destination = Path("artifacts/source-downloads") / ns["FILENAME"]
            receipt = Path("artifacts/database/libaudit-common-download.json")
            with contextlib.redirect_stdout(io.StringIO()):
                yield ns, payload, destination, receipt

    def response(self, payload, *, status=200):
        response = io.BytesIO(payload)
        response.status = status
        return response

    def test_verified_download_atomic_publication_and_reuse(self):
        with self.fixture() as (ns, payload, destination, receipt):
            fetch = mock.Mock(return_value=self.response(payload))
            ns["urlopen"] = fetch
            ns["prepare"]()
            fetch.assert_called_once_with(ns["URL"], timeout=60)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertFalse(json.loads(receipt.read_text())["reused_verified_file"])
            self.assertEqual(sorted(x.name for x in destination.parent.iterdir()), [ns["FILENAME"]])
            ns["urlopen"] = mock.Mock(side_effect=AssertionError("Verified cache must avoid network"))
            ns["prepare"]()
            self.assertTrue(json.loads(receipt.read_text())["reused_verified_file"])
            self.assertEqual(set(json.loads(receipt.read_text())), {"url", "sha256", "bytes", "filename", "reused_verified_file"})

    def test_corrupt_short_oversized_and_http_payloads_are_rejected(self):
        for mode in ("corrupt", "short", "oversized", "http"):
            with self.subTest(mode=mode), self.fixture() as (ns, payload, destination, receipt):
                bad = {"corrupt": b"X" * len(payload), "short": payload[:-1],
                       "oversized": payload + b"X", "http": payload}[mode]
                ns["urlopen"] = mock.Mock(return_value=self.response(bad, status=503 if mode == "http" else 200))
                with self.assertRaises(ValueError):
                    ns["prepare"]()
                self.assertFalse(destination.exists())
                self.assertFalse(receipt.exists())

    def test_interrupted_download_does_not_replace_existing_file(self):
        with self.fixture() as (ns, payload, destination, receipt):
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old unverified download")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.side_effect = OSError("interrupted transfer")
            ns["urlopen"] = mock.Mock(return_value=response)
            with self.assertRaises(OSError):
                ns["prepare"]()
            response.read.assert_called_once_with(len(payload) + 1)
            self.assertEqual(destination.read_bytes(), b"old unverified download")
            self.assertFalse(receipt.exists())

    def test_interrupted_atomic_replace_removes_only_temporary_file(self):
        with self.fixture() as (ns, payload, destination, receipt):
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old unverified download")
            ns["urlopen"] = mock.Mock(return_value=self.response(payload))
            with mock.patch.object(Path, "replace", side_effect=OSError("interrupted replace")):
                with self.assertRaises(OSError):
                    ns["prepare"]()
            self.assertEqual(destination.read_bytes(), b"old unverified download")
            self.assertEqual(list(destination.parent.iterdir()), [destination])
            self.assertFalse(receipt.exists())

    def test_symlink_destination_is_not_read_or_replaced(self):
        with self.fixture() as (ns, payload, destination, receipt):
            destination.parent.mkdir(parents=True)
            target = Path("unrelated")
            target.write_bytes(payload)
            destination.symlink_to(target.resolve())
            ns["urlopen"] = mock.Mock(side_effect=AssertionError("No download for unsafe path"))
            with self.assertRaises(ValueError):
                ns["prepare"]()
            self.assertTrue(destination.is_symlink())
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
