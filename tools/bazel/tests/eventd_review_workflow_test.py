#!/usr/bin/env python3
"""Exercise the hosted Eventd selection, receipt binding and CI boundary."""
import contextlib
import hashlib
import io
import sys
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/eventd-review-source-inputs"
WORKFLOW = ROOT / ".github/workflows/bazel-eventd-image.yml"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", image="none", source="a" * 40,
             registry="b" * 40, number=999):
    expression = " ".join(expression.splitlines()).strip()
    if expression.startswith("${{"):
        expression = expression[3:-2].strip()
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.event.pull_request.number": str(number),
        "github.event.pull_request.head.ref": repr(branch),
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
    return eval(expression, {"__builtins__": {}}, {"fromJSON": json.loads, "always": lambda: True, "contains": lambda values, item: item in values})


def step_script(workflow, name):
    step = workflow.split("      - name: " + name + "\n", 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class EventdReviewWorkflowTest(unittest.TestCase):
    def test_paired_immutable_inputs(self):
        script = step_script(WORKFLOW.read_text(), "Validate the optional reviewed Eventd source selection")
        for source, registry, valid in [("a" * 40, "b" * 40, True),
                ("master", "b" * 40, False), ("a" * 40, "", False),
                ("", "b" * 40, False), ("A" * 40, "b" * 40, False),
                ("a" * 39, "b" * 40, False), ("", "", True)]:
            with self.subTest(source=source, registry=registry):
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                    env={"PATH": "/usr/bin:/bin", "SOURCE_REVISION": source, "REGISTRY_REVISION": registry})
                self.assertEqual(result.returncode, 0 if valid else 2)

    def test_original_amd64_and_optional_registry_environment(self):
        workflow = WORKFLOW.read_text()
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("matrix:", workflow)
        expression = re.search(r"^      BAZEL: (.+)$", workflow, re.MULTILINE).group(1)
        self.assertEqual(evaluate(expression, event="workflow_dispatch"), "tools/bazel/ci/review_bazel.sh")
        self.assertEqual(evaluate(expression, registry="", source=""), "bazel")
        ref = re.search(r"^          ref: (.+)$", workflow, re.MULTILINE).group(1)
        self.assertEqual(evaluate(ref), "a" * 40)
        self.assertEqual(evaluate(ref, source="", registry=""), "c" * 40)

    def test_both_checkouts_and_all_original_eventd_gates(self):
        workflow = WORKFLOW.read_text()
        ref = "ref: ${{ inputs.source_revision || github.sha }}"
        self.assertEqual(workflow.count(ref), 2)
        for phase in ["build", "validate", "incremental"]:
            self.assertIn("tools/bazel/ci/eventd_image.py " + phase, workflow)
        self.assertIn("tools/bazel/tests/eventd_make_test.py", workflow)
        self.assertIn("tools/bazel/ci/eventd_loaded.py --artifacts artifacts/eventd", workflow)
        self.assertIn("needs: [native, runtime]", workflow)
        self.assertIn('test "$BUILD_RESULT" = success', workflow)
        self.assertIn('test "$RUNTIME_RESULT" = success', workflow)
        self.assertIn("artifacts/eventd/reviewed-selection.json", workflow)

    def test_real_completed_receipt_and_loaded_tampering(self):
        workflow = WORKFLOW.read_text()
        create = step_script(workflow, "Bind the completed Eventd artifacts to the reviewed source")
        verify = step_script(workflow, "Verify the selected source against the downloaded Eventd evidence")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = root / "tools/bazel/ci/eventd_loaded.py"
            loaded.parent.mkdir(parents=True)
            loaded.write_text("# exact selected driver\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Workflow fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "fixture"], cwd=root, check=True)
            source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            artifacts = root / "artifacts/eventd"
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

    def test_exact_owned_no_deb_preserves_original_scope(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(evaluate(guard))
            self.assertTrue(evaluate(guard, branch="codex/eventd-oci-amd64"))
            self.assertTrue(evaluate(guard, branch="codex/config-engine-amd64"))
            for changes in [{"branch": BRANCH + "-suffix"}, {"branch": "unrelated"},
                            {"head_repo": "other/fork"}, {"event": "push"}, {"event": "workflow_dispatch"}]:
                self.assertFalse(evaluate(guard, **changes))
        job = workflow.split("\n  vs_container:\n", 1)[1]
        gate = textwrap.dedent(job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0])
        self.assertFalse(evaluate(gate))
        self.assertTrue(evaluate(gate, branch="unrelated"))

    def test_original_kernel_pr6_guard(self):
        workflow = (ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text()
        gate = re.search(r"^    if: (.+)$", workflow, re.MULTILINE).group(1)
        self.assertEqual(gate, "github.event_name != 'pull_request' || github.event.pull_request.number == 6")
        self.assertFalse(evaluate(gate))
        self.assertTrue(evaluate(gate, number=6))
        self.assertTrue(evaluate(gate, event="push"))
        self.assertTrue(evaluate(gate, event="workflow_dispatch"))


class ReviewedDownloadTest(unittest.TestCase):
    STEP = "Prepare the reviewed libpcre2-dev download"
    SOURCE = "a5a1f8dfa22b0b99226968f8d11dfe9d654e9a97"
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
        valid = {"event": "workflow_dispatch", "source": self.SOURCE, "registry": self.REGISTRY}
        self.assertTrue(evaluate(gate, **valid))
        for changed in ({"event": "pull_request"}, {"event": "push"}, {"source": ""},
                        {"registry": ""}, {"source": "b" * 40}, {"registry": "c" * 40}):
            self.assertFalse(evaluate(gate, **{**valid, **changed}), changed)
        self.assertLess(workflow.index("Prepare the verified compression source and caches"), workflow.index(self.STEP))
        self.assertLess(workflow.index(self.STEP), workflow.index("Validate Make handoff and version inputs"))
        self.assertLess(workflow.index(self.STEP), workflow.index("Build runtime and debug images from owner TARs"))
        self.assertIn("artifacts/eventd/libpcre2-dev-download.json", workflow)
        self.assertIn("'libpcre2-dev-download.json'", workflow)

    def test_fixed_official_address_and_original_locked_identity(self):
        ns = self.implementation()
        self.assertEqual(ns["URL"], "https://snapshot.debian.org/file/648dd0b86870e854e02868553a2246b3470b96dd")
        self.assertEqual(ns["SHA256"], "b5cb023bfaad356053583af384f7b75caec3ac127a197f889164ef4d96397cef")
        self.assertEqual(ns["SIZE"], 852668)
        self.assertEqual(ns["FILENAME"], "libpcre2-dev_10.46-1~deb13u1_amd64.deb")

    @contextlib.contextmanager
    def fixture(self):
        # Plain bytes test download integrity; no Debian package is produced.
        payload = b"ordinary download test bytes"
        ns = self.implementation()
        ns["SIZE"] = len(payload)
        ns["SHA256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            destination = Path("artifacts/source-downloads") / ns["FILENAME"]
            receipt = Path("artifacts/eventd/libpcre2-dev-download.json")
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
