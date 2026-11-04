#!/usr/bin/env python3
"""Exercise the manual parent-image selection and its no-DEB CI boundary."""
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
MODE = "base"
BRANCH = "codex/base-review-source-inputs"
WORKFLOW = ".github/workflows/bazel-base-images.yml"


def evaluate(expression, *, event="pull_request", branch=BRANCH,
             head_repo="securely1g/sonic-buildimage", image="none", architecture="all", source="", registry="", matrix_architecture="amd64"):
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
        "inputs.skip_vs": "True",
        "inputs.source_revision": repr(source), "inputs.registry_revision": repr(registry),
        "matrix.architecture": repr(matrix_architecture),
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
        self.assertIn("SONIC_BAZEL_REGISTRY_REVISION: ${{ inputs.registry_revision }}", workflow)
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

    def test_kernel_generation_stays_out_of_only_the_owned_control_pr(self):
        workflow = (ROOT / ".github/workflows/bazel-kernel-bundle.yml").read_text()
        job = workflow.split("  kernel-bundle:\n", 1)[1]
        gate = textwrap.dedent(job.split("    if: >-\n", 1)[1].split("    name:", 1)[0])
        self.assertFalse(evaluate(gate))
        for changes in ({"branch": "unrelated"}, {"branch": BRANCH + "-suffix"},
                        {"head_repo": "other/fork"}, {"event": "push"},
                        {"event": "workflow_dispatch"}):
            self.assertTrue(evaluate(gate, **changes))

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


class ReviewedDownloadTest(unittest.TestCase):
    STEP = "Prepare the reviewed libde265 download"
    SOURCE = "a5a1f8dfa22b0b99226968f8d11dfe9d654e9a97"
    REGISTRY = "6a69c2d89070d4d2ffc80b1e13626da387f7ea65"

    def implementation(self):
        script = step_script((ROOT / WORKFLOW).read_text(), self.STEP)
        code = script.split("python3 - <<'PYDEB'\n", 1)[1].rsplit("\nPYDEB", 1)[0]
        namespace = {"__name__": "download_test"}
        with mock.patch.object(sys, "path", [str(ROOT), *sys.path]):
            exec(compile(code, WORKFLOW, "exec"), namespace)
        return namespace

    def test_exact_manual_graph_only_and_before_analysis(self):
        workflow = (ROOT / WORKFLOW).read_text()
        step = workflow.split("      - name: " + self.STEP + "\n", 1)[1].split("\n      - ", 1)[0]
        gate = textwrap.dedent(step.split("        if: >-\n", 1)[1].split("        run:", 1)[0])
        valid = {"event": "workflow_dispatch", "source": self.SOURCE, "registry": self.REGISTRY}
        self.assertTrue(evaluate(gate, **valid))
        for changed in ({"event": "pull_request"}, {"event": "push"}, {"source": ""},
                        {"registry": ""}, {"source": "b" * 40}, {"registry": "c" * 40},
                        {"matrix_architecture": "arm64"}):
            self.assertFalse(evaluate(gate, **{**valid, **changed}), changed)
        self.assertLess(workflow.index("Prepare the checksum-pinned pigz source archive"), workflow.index(self.STEP))
        self.assertLess(workflow.index(self.STEP), workflow.index("Check clean dependency inputs"))
        self.assertLess(workflow.index(self.STEP), workflow.index("Build and inspect runtime images and matching symbols"))
        self.assertIn("artifacts/base-images/*.json", workflow)

    def test_fixed_official_address_and_original_locked_identity(self):
        ns = self.implementation()
        self.assertEqual(ns["URL"], "https://snapshot.debian.org/file/ec0ded8276cfdb68db5f5daad1469dc59d440627")
        self.assertEqual(ns["SHA256"], "d80d262c7a323fb594712354e4080c1ffe0c25e6cfbbd1192cda82536be123d4")
        self.assertEqual(ns["SIZE"], 189084)
        self.assertEqual(ns["FILENAME"], "libde265-0_1.0.15-1+b3_amd64.deb")

    @contextlib.contextmanager
    def fixture(self):
        # Plain bytes test download integrity; no Debian package is produced.
        payload = b"ordinary download test bytes"
        ns = self.implementation()
        ns["SIZE"] = len(payload)
        ns["SHA256"] = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            destination = Path("artifacts/source-downloads") / ns["FILENAME"]
            receipt = Path("artifacts/base-images/libde265-download.json")
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
