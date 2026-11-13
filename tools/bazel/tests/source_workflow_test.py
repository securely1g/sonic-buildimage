"""Execute source-validation workflow shells without compiling source packages."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-oci.yml"


def jobs(path=WORKFLOW):
    """Read top-level job blocks; the shell itself is executed without rewriting."""
    contents = path.read_text().split("\njobs:\n", 1)[1]
    return dict(re.findall(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
                           contents, re.MULTILINE | re.DOTALL))


def steps(job):
    return dict(re.findall(r"^      - name: ([^\n]+)\n(.*?)(?=^      - |\Z)",
                           job, re.MULTILINE | re.DOTALL))


def shell(step):
    match = re.search(r"^        run: (.*)\n?", step, re.MULTILINE)
    if match is None:
        return None
    if match[1] == "|":
        lines = []
        for line in step[match.end():].splitlines():
            if line and not line.startswith("          "):
                break
            lines.append(line)
        return textwrap.dedent("\n".join(lines))
    return match[1]


def step_blocks(job):
    """Expand the workflow's step aliases; accept either YAML list indentation."""
    body = job.split("    steps:\n", 1)[1]
    indent = re.search(r"^( +)- ", body, re.MULTILINE)[1]
    blocks = re.findall(r"^" + indent + r"- .*?(?=^" + indent + r"- |^    [a-z][\w-]*:|\Z)",
                        body, re.MULTILINE | re.DOTALL)
    anchors = dict(re.findall(r"^      - &(\w+)\n(.*?)(?=^      - |^  \w|\Z)",
                              WORKFLOW.read_text(), re.MULTILINE | re.DOTALL))
    result = []
    for block in blocks:
        block = textwrap.indent(textwrap.dedent(block), "      ")
        alias = re.fullmatch(r"      - \*(\w+)\s*", block)
        if alias:
            block = "      - " + anchors[alias[1]].lstrip()
        else:
            block = re.sub(r"^      - &\w+\n        ", "      - ", block)
        result.append(block)
    return result


def step_value(block, key, indent=8):
    """Read only the existing scalar forms; no third-party YAML dependency."""
    match = re.search(r"^" + " " * indent + re.escape(key) + r": (.*?)(?=^" +
                      r" {1," + str(indent) + r"}[\w-]+: |\Z)", block, re.MULTILINE | re.DOTALL)
    if match is None:
        return None
    value = match[1].strip()
    if value.startswith("|\n"):
        return textwrap.dedent(match[1].split("\n", 1)[1]).rstrip() + "\n"
    if value.startswith(('"', "'")):
        # YAML flow scalars fold physical newlines; quoted escapes stay intact.
        value = re.sub(r"\\\n[ \t]*", "", value)
        value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
        value = re.sub(r"\n+", lambda m: " " if len(m[0]) == 1 else "\n" * (len(m[0]) - 1), value)
        return json.loads(value) if value.startswith('"') else value[1:-1].replace("''", "'")
    return value


class NativeFamilyJobsTest(unittest.TestCase):
    receipt_families = ("p4rt", "syncd", "gearbox")
    families = ("swss_layer", "orchagent", "sflow", "otel", "dash_ha", "sysmgr",
                "gearbox", "dash_engine", "p4rt", "syncd", "teamd")

    def test_native_families_keep_separate_hosted_caches_and_manual_selection(self):
        workflow = jobs()
        archive = workflow["archive"].split("    steps:\n", 1)[0]
        row_sets = [json.loads(value) for value in re.findall(r"'(\[\{.*?\}\])'", archive)]
        self.assertEqual(len(row_sets), 2)
        self.assertEqual(row_sets[0], [row for row in row_sets[1] if row["architecture"] == "amd64"])
        self.assertEqual({row["architecture"] for row in row_sets[1]}, {"amd64", "arm64"})
        self.assertEqual(next(row["runner"] for row in row_sets[1] if row["architecture"] == "arm64"),
                         "ubuntu-24.04-arm")
        caches = set()
        for family in self.families:
            with self.subTest(family=family):
                job = workflow[family + "_native"]
                header = job.split("    steps:\n", 1)[0]
                if family in self.receipt_families:
                    self.assertRegex(header, r"runs-on: (?:\*receipt_metadata_runner|&receipt_metadata_runner \$\{\{)")
                else:
                    self.assertIn("runs-on: ubuntu-24.04\n", header)
                self.assertNotIn("    needs:", header)
                self.assertNotIn("matrix.", job)
                if family == "swss_layer":
                    self.assertIn('test "$(uname -m)" = "x86_64"', job)
                    self.assertIn("tools/bazel/ci/lldp_draft_cache.py", job)
                elif family not in self.receipt_families:
                    self.assertIn("      - *native_architecture\n", job)
                    self.assertIn("      - *native_caches\n", job)
                expanded = step_blocks(job)
                reference = step_blocks(workflow["swss_layer_native"])
                for name in ("Check native architecture and generated dependency state",
                             "Preserve configured caches for Draft validation",
                             "Configure the verified source mirror"):
                    selected = [b for b in expanded if b.startswith("      - name: " + name + "\n")]
                    original = [b for b in reference if b.startswith("      - name: " + name + "\n")]
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(len(original), 1)
                    expected_run = step_value(original[0], "run")
                    mirror_ref = "997796400cc3eefc1f0590b227cd9b74c2141424"
                    mirror_sha = "a937b05d4197544bac965bb2b96634e12423c7f82b0fd6b13d894bcd087bce3b"
                    if name == "Configure the verified source mirror" and family in self.receipt_families:
                        expected_run = expected_run.replace(mirror_ref, "97cc502556b7ec3435d2d67b105315bcc18bb47e").replace(
                            mirror_sha, "ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720").replace(
                            "# Load the checksum-verified pinned configuration. Archive integrity checks remain unchanged.",
                            "# Load the merged registry configuration. Bazel verifies each archive against its pinned checksum.")
                        mirror_ref = "97cc502556b7ec3435d2d67b105315bcc18bb47e"
                        mirror_sha = "ddd6f40504a30e138a62be0d728e1a352391d0f781c927eaf4ea45c44d8a0720"
                    self.assertEqual(step_value(selected[0], "run"), expected_run)
                    if name == "Configure the verified source mirror":
                        self.assertIn(mirror_ref + "/ci/source_mirrors.cfg", step_value(selected[0], "run"))
                        self.assertIn(mirror_sha, step_value(selected[0], "run"))
                    if family in self.receipt_families:
                        self.assertEqual(step_value(selected[0], "if"),
                                         "env." + family.upper() + "_WORKER_RECEIPT != 'true'")
                setup = [b for b in expanded if b.startswith("      - name: Set up Bazel\n")]
                self.assertEqual(len(setup), 1)
                self.assertEqual(step_value(setup[0], "uses").split()[0],
                                 "bazel-contrib/setup-bazel@c5acdfb288317d0b5c0bbd7a396a3dc868bb0f86")
                for option in ("bazelisk-cache", "repository-cache"):
                    self.assertEqual(step_value(setup[0], option, 10), "true")
                self.assertEqual(step_value(setup[0], "bazelisk-version", 10),
                                 "" if family not in self.receipt_families else "1.29.0")
                self.assertEqual(step_value(setup[0], "cache-version", 10), "8.5.1")
                cache = step_value(setup[0], "disk-cache", 10)
                self.assertNotIn(cache, caches)
                self.assertNotEqual(cache, "swss-layer-amd64")
                caches.add(cache)
                condition = re.search(r"^    if: (.+)$", header, re.MULTILINE)[1]
                self.assertEqual("${{ !cancelled() && (" + condition + ") }}",
                                 re.search(r"^    if: (.+)$", archive, re.MULTILINE)[1])
                for event, selected, expected in (("pull_request", "", True),
                                                  ("push", "", True),
                                                  ("workflow_dispatch", "none", True),
                                                  ("workflow_dispatch", "sflow", False)):
                    expression = condition.replace("github.event_name", repr(event)).replace(
                        "inputs.source_image", repr(selected)).replace("||", " or ")
                    self.assertEqual(eval(expression, {"__builtins__": {}}), expected)

    def test_required_archive_check_rejects_any_non_success_native_result(self):
        workflow = jobs()
        archive = workflow["archive"]
        required = re.search(r"^    needs: \[(.+)\]$", archive, re.MULTILINE)[1]
        self.assertEqual(set(required.split(", ")), {family + "_native" for family in self.families})
        gate = steps(archive)["Require every native image family to pass"]
        self.assertIn("if: always() && matrix.architecture == 'amd64'", gate)
        self.assertIn("NATIVE_RESULTS: ${{ join(needs.*.result, ',') }}", gate)
        script = shell(gate)
        cases = [("all successful", ["success"] * 11, True),
                 ("missing result", ["success"] * 10, False),
                 ("extra result", ["success"] * 12, False),
                 ("no results", [], False)]
        for status in ("failure", "cancelled", "skipped", ""):
            cases.append((status or "empty result", ["success"] * 5 + [status] + ["success"] * 5, False))
        for name, results, passed in cases:
            with self.subTest(case=name):
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                        env=dict(os.environ, NATIVE_RESULTS=",".join(results)),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, passed, result.stdout + result.stderr)

    def test_runtime_jobs_wait_for_every_downloaded_native_artifact(self):
        workflow = jobs()

        def artifacts(job, action):
            blocks = [block for block in step_blocks(job)
                      if "uses: actions/" + action + "-artifact@" in block]
            diagnostics = [block for block in blocks
                           if step_value(block, "name", 10) == "dash-engine-incremental-amd64"]
            self.assertEqual(len(diagnostics), 1 if job == workflow["dash_engine_native"] and action == "upload" else 0)
            for block in diagnostics:
                self.assertIn("uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", block)
                self.assertEqual(step_value(block, "if"),
                                 "always() && steps.dash_engine_public.outcome == 'success' && "
                                 "hashFiles('artifacts/dash-engine/incremental.json') != ''")
                self.assertEqual(step_value(block, "path", 10), "artifacts/dash-engine/incremental.json")
                self.assertEqual(step_value(block, "if-no-files-found", 10), "error")
                self.assertEqual(step_value(block, "retention-days", 10), "14")
                self.assertEqual(step_value(block, "compression-level", 10), "1")
                self.assertLess(workflow["dash_engine_native"].index("id: dash_engine_public"),
                                workflow["dash_engine_native"].index("name: dash-engine-incremental-amd64"))
            return [block for block in blocks if block not in diagnostics]

        def split_uploads(family, kind):
            uploads = artifacts(workflow[family + "_" + kind], "upload")
            # Context is a separate JSON sidecar, never image/request evidence.
            context_name = ("source-context-${{ github.job }}-"
                            "${{ strategy.job-index || 0 }}-${{ github.run_attempt }}")
            contexts = [block for block in uploads
                        if step_value(block, "name", 10) == context_name]
            self.assertEqual(len(contexts), 0 if family in ("swss_layer", "teamd") else 1)
            for block in contexts:
                self.assertIn("        uses: actions/upload-artifact@"
                              "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n", block)
                outcomes = "steps.source_context.outcome == 'success'"
                if family in self.receipt_families:
                    outcomes += " || steps.source_context_fallback.outcome == 'success'"
                self.assertEqual(step_value(block, "if"),
                                 "always() && github.event_name == 'pull_request' && (" + outcomes + ")")
                context_path = "artifacts/source-context/context.json"
                if family in self.receipt_families and kind == "native":
                    context_path = "${{ env.SONIC_RECEIPT_WORKSPACE || github.workspace }}/" + context_path
                self.assertEqual(step_value(block, "path", 10), context_path)
                self.assertEqual(step_value(block, "if-no-files-found", 10), "error")
                self.assertEqual(step_value(block, "retention-days", 10), "14")
            uploads = [block for block in uploads if block not in contexts]
            if family not in self.receipt_families:
                return uploads
            flag = "env." + family.upper() + "_WORKER_RECEIPT"
            scratch = "${{ env.SONIC_RECEIPT_SCRATCH }}" if kind == "native" else "${{ runner.temp }}"
            receipt_names = {
                family + "-ci-" + family + "_" + kind + "-${{ github.run_id }}-${{ github.run_attempt }}":
                    (flag + " == 'true'", scratch + "/" + family + "-ci-verified.json"),
            }
            if kind == "native":
                receipt_names["${{ steps." + family + "_request.outputs.request_name }}"] = (
                    flag + " == 'true' && steps." + family + "_request.outputs.new_request == 'true'",
                    scratch + "/" + family + "-ci-request/request.json")
            native = []
            for block in uploads:
                name = step_value(block, "name", 10)
                if name in receipt_names:
                    guard, path = receipt_names.pop(name)
                    self.assertEqual(step_value(block, "if"), guard)
                    self.assertEqual(step_value(block, "path", 10), path)
                    self.assertEqual(step_value(block, "if-no-files-found", 10), "error")
                else:
                    self.assertIn(flag + " != 'true'", step_value(block, "if"))
                    native.append(block)
            self.assertFalse(receipt_names, "Missing guarded request or verified summary upload")
            return native

        producers = {}
        for family in self.families:
            job = family + "_native"
            uploads = split_uploads(family, "native")
            self.assertEqual(len(uploads), 1)
            name = step_value(uploads[0], "name", 10)
            if family in self.receipt_families:
                expected = {"p4rt": "p4rt-images-amd64-${{ github.sha }}", "syncd": "syncd-source-amd64",
                            "gearbox": "gbsyncd-source-amd64"}
                self.assertEqual(name, expected[family])
            self.assertNotIn(name, producers)
            producers[name] = job
        evidence_names = set()
        for family in self.families:
            with self.subTest(family=family):
                job = workflow[family + "_runtime"]
                needed = re.search(r"^    needs: (.+)$", job, re.MULTILINE)[1]
                needed = set(needed.strip("[]").replace(" ", "").split(","))
                downloads = artifacts(job, "download")
                if family in self.receipt_families:
                    flag = "env." + family.upper() + "_WORKER_RECEIPT"
                    receipt_downloads = [b for b in downloads if step_value(b, "artifact-ids", 10)]
                    self.assertEqual(len(receipt_downloads), 2)
                    expected = {
                        "${{ steps." + family + "_request.outputs.request_artifact }}":
                            (flag + " == 'true' && steps." + family + "_request.outputs.new_request == 'false'",
                             "${{ steps." + family + "_request.outputs.request_run }}"),
                        "${{ steps." + family + "_acceptance.outputs.artifact }}":
                            (flag + " == 'true'", "${{ steps." + family + "_acceptance.outputs.worker_run }}"),
                    }
                    for block in receipt_downloads:
                        artifact_id = step_value(block, "artifact-ids", 10)
                        self.assertIn(artifact_id, expected)
                        guard, run_id = expected.pop(artifact_id)
                        self.assertEqual(step_value(block, "if"), guard)
                        self.assertEqual(step_value(block, "run-id", 10), run_id)
                        self.assertEqual(step_value(block, "repository", 10), "securely1g/sonic-buildimage")
                    self.assertFalse(expected)
                    downloads = [b for b in downloads if b not in receipt_downloads]
                    self.assertEqual(len(downloads), 1)
                    self.assertEqual(step_value(downloads[0], "if"), flag + " != 'true'")
                names = [step_value(block, "name", 10) for block in downloads]
                self.assertTrue(names)
                self.assertEqual(needed, {producers[name] for name in names})
                for block in split_uploads(family, "runtime"):
                    name = step_value(block, "name", 10)
                    self.assertNotIn(name, evidence_names, "Runtime jobs must not overwrite each other's evidence")
                    evidence_names.add(name)



class ScopedWorkflowRoutingTest(unittest.TestCase):
    @staticmethod
    def evaluate(expression, values):
        expression = re.sub(r"(?:github|inputs)\.[A-Za-z0-9_.]+",
                            lambda item: repr(values[item[0]]), expression)
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression)
        return eval(" ".join(expression.split()), {"__builtins__": {}, "fromJSON": json.loads})


    def test_source_context_upload_follows_the_selected_checkout(self):
        with tempfile.TemporaryDirectory(prefix="receipt workspace ") as temporary:
            workspace = Path(temporary)
            for receipt_checkout in (str(workspace / "fresh-child"), str(workspace), ""):
                expected_root = Path(receipt_checkout or str(workspace))
                expected = expected_root / "artifacts/source-context/context.json"
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text('{"retained":"original-context"}\n')

                def expand(value):
                    return re.sub(r"\$\{\{ (.*?) \}\}", lambda match: self.evaluate(
                        match[1].replace("env.SONIC_RECEIPT_WORKSPACE", repr(receipt_checkout)),
                        {"github.workspace": str(workspace)}), value)

                for family in NativeFamilyJobsTest.receipt_families:
                    blocks = step_blocks(jobs()[family + "_native"])
                    upload = next(block for block in blocks if block.startswith(
                        "      - name: Retain the tested PR source context\n"))
                    context_id = "source_context" if receipt_checkout else "source_context_fallback"
                    source = next(block for block in blocks if step_value(block, "id") == context_id)
                    writer = Path(expand(step_value(source, "working-directory") or str(workspace)))
                    directory = re.search(r"--directory ([^\s]+)", shell(source))[1]
                    produced = writer / directory / "context.json"
                    retained = Path(expand(step_value(upload, "path", 10)))
                    with self.subTest(family=family, receipt_checkout=receipt_checkout):
                        self.assertEqual(produced, expected)
                        self.assertEqual(retained, produced)
                        self.assertEqual(retained.read_text(), '{"retained":"original-context"}\n')

    def test_pr206_and_parent_pr196_narrow_the_four_shared_matrices(self):
        seen = 0
        for filename in ("bazel-oci.yml", "bazel-base-images.yml", "runtime-image-validation.yml"):
            for job in jobs(ROOT / ".github/workflows" / filename).values():
                header = job.split("    steps:\n", 1)[0]
                if "include: >-" not in header or "github.event.pull_request.number == 206" not in header:
                    continue
                expression = re.search(r"include: >-\n\s*\$\{\{ (.*?) \}\}", header, re.DOTALL)[1]
                both = json.loads(re.findall(r"'(\[\{.*?\}\])'", expression)[1])
                amd64 = [row for row in both if row.get("architecture", row.get("arch")) == "amd64"]
                self.assertEqual(len(both), 2)
                self.assertEqual(len(amd64), 1)
                for event, repo, number, expected in (
                    ("pull_request", "securely1g/sonic-buildimage", 206, amd64),
                    ("pull_request", "securely1g/sonic-buildimage", 196, amd64),
                    ("pull_request", "securely1g/sonic-buildimage", 201, both),
                    ("pull_request", "example/sonic-buildimage", 206, both),
                    ("workflow_dispatch", "securely1g/sonic-buildimage", 206, both),
                    ("push", "securely1g/sonic-buildimage", 206, both),
                ):
                    with self.subTest(workflow=filename, event=event, repository=repo, number=number):
                        self.assertEqual(self.evaluate(expression, {
                            "github.event_name": event, "github.repository": repo,
                            "github.event.pull_request.number": number,
                        }), expected)
                seen += 1
        self.assertEqual(seen, 4)

    def test_only_trusted_hub_requests_use_k3s_without_a_job_container(self):
        workflow = WORKFLOW.read_text()
        expressions = {key: re.search(r"^    " + key + r": &receipt_metadata_\w+ \$\{\{ (.*?) \}\}",
                                     workflow, re.MULTILINE)[1]
                       for key in ("runs-on", "container")}
        for family in NativeFamilyJobsTest.receipt_families:
            header = jobs()[family + "_native"].split("    steps:\n", 1)[0]
            for key in expressions:
                self.assertRegex(header, re.escape(key) + r": (?:\*receipt_metadata_|&receipt_metadata_)")
        original_container = json.loads(re.search(r"fromJSON\('([^']+)'\)", expressions["container"])[1])
        for number, branch in ((196, "codex/dash-engine-review-catalog"),
                               (206, "codex/final23-integration-staging")):
            original = {
                "github.event_name": "pull_request", "github.repository": "securely1g/sonic-buildimage",
                "github.event.pull_request.head.repo.full_name": "securely1g/sonic-buildimage",
                "github.actor": "securely1g", "github.triggering_actor": "securely1g",
                "github.event.pull_request.number": number, "github.head_ref": branch,
            }
            variations = ({}, {"github.event_name": "workflow_dispatch"},
                          {"github.event.pull_request.head.repo.full_name": "example/sonic-buildimage"},
                          {"github.actor": "example"}, {"github.triggering_actor": "example"},
                          {"github.event.pull_request.number": 999}, {"github.head_ref": branch + "-extra"})
            for variation in variations:
                values = dict(original, **variation)
                with self.subTest(number=number, variation=variation):
                    self.assertEqual(self.evaluate(expressions["runs-on"], values),
                                     ["ubuntu-24.04"] if variation else ["sonic-slave-container-k3s"])
                    self.assertEqual(self.evaluate(expressions["container"], values),
                                     original_container if variation else "")

    def test_only_pr196_narrows_existing_architecture_matrices(self):
        seen = 0
        for filename in ('bazel-oci.yml', 'bazel-base-images.yml', 'runtime-image-validation.yml', 'bazel-eventd.yml', 'bazel-supervisor-rust.yml', 'bazel-yang-models.yml', 'bazel-libteam.yml'):
            for job in jobs(ROOT / ".github/workflows" / filename).values():
                header = job.split("    steps:\n", 1)[0]
                if "include: >-" not in header or "github.event.pull_request.number == 196" not in header:
                    continue
                expression = re.search(r"include: >-\n\s*\$\{\{ (.*?) \}\}", header, re.DOTALL)[1]
                both = json.loads(re.findall(r"'(\[\{.*?\}\])'", expression)[1])
                amd64 = [row for row in both if row.get("architecture", row.get("debian_arch", row.get("arch"))).lower() == "amd64"]
                self.assertEqual(len(both), 2)
                self.assertEqual(len(amd64), 1)
                for event, repo, number, expected in (
                    ("pull_request", "securely1g/sonic-buildimage", 196, amd64),
                    ("pull_request", "securely1g/sonic-buildimage", 201, both),
                    ("pull_request", "example/sonic-buildimage", 196, both),
                    ("workflow_dispatch", "securely1g/sonic-buildimage", 196, both),
                    ("push", "securely1g/sonic-buildimage", 196, both),
                ):
                    with self.subTest(workflow=filename, event=event, repository=repo, number=number):
                        self.assertEqual(self.evaluate(expression, {
                            "github.event_name": event, "github.repository": repo,
                            "github.event.pull_request.number": number,
                        }), expected)
                seen += 1
        self.assertEqual(seen, 8)

    def test_receipt_metadata_writer_and_uploader_share_the_selected_paths(self):
        with tempfile.TemporaryDirectory(prefix="receipt request workspace ") as temporary:
            root = Path(temporary)
            for layout in ("fresh-child", "hosted"):
                workspace = root / layout / "source"
                scratch = root / layout / "scratch"
                values = {"env.SONIC_RECEIPT_WORKSPACE": str(workspace),
                          "env.SONIC_RECEIPT_SCRATCH": str(scratch)}

                def scalar(block, key, indent=8):
                    match = re.search(r"^" + " " * indent + re.escape(key) +
                                      r": ([^\n]*)$", block, re.MULTILINE)
                    self.assertIsNotNone(match, (key, block.splitlines()[0]))
                    return match[1]

                def expand(value):
                    return re.sub(r"\$\{\{ (.*?) \}\}",
                                  lambda match: values[match[1]], value)

                def argument(block, option):
                    command = shell(block)
                    if command.startswith('"'):
                        command = json.loads(command)
                    value = re.search(r"--" + option + r' "([^"\n]+)"', command)[1]
                    return Path(value.replace("$SONIC_RECEIPT_SCRATCH", str(scratch)))

                for family in NativeFamilyJobsTest.receipt_families:
                    blocks = step_blocks(jobs()[family + "_native"])
                    boundary = next(i for i, block in enumerate(blocks) if block.startswith(
                        "      - name: Allocate source-build space on this disposable runner"))
                    self.assertGreaterEqual(boundary, 9)
                    blocks = blocks[:boundary]
                    request = next(x for x in blocks if re.search(r"(?m)^        id: " + family + "_request$", x))
                    discover = next(x for x in blocks if re.search(r"(?m)^        id: " + family + "_acceptance$", x))
                    verify = next(x for x in blocks if x.startswith("      - name: Verify all original "))
                    uploads = [x for x in blocks if "uses: actions/upload-artifact@" in x and
                               scalar(x, "if").startswith("env." + family.upper() + "_WORKER_RECEIPT == 'true'")]
                    downloads = [x for x in blocks if "uses: actions/download-artifact@" in x and
                                 scalar(x, "if").startswith("env." + family.upper() + "_WORKER_RECEIPT == 'true'")]
                    with self.subTest(family=family, layout=layout):
                        for step in (request, discover, verify):
                            self.assertEqual(Path(expand(scalar(step, "working-directory"))), workspace)
                        request_dir = argument(request, "directory")
                        self.assertEqual(request_dir, argument(discover, "directory"))
                        self.assertEqual(request_dir, argument(verify, "directory"))
                        output = argument(verify, "output")
                        produced = (request_dir / "request.json", output)
                        for target in produced:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text('{"fixture":"original-metadata"}\n')
                        retained = {Path(expand(scalar(x, "path", 10))) for x in uploads}
                        self.assertEqual(retained, set(produced))
                        for target in retained:
                            self.assertEqual(target.read_text(), '{"fixture":"original-metadata"}\n')
                        self.assertEqual({Path(expand(scalar(x, "path", 10))) for x in downloads},
                                         {request_dir, argument(verify, "public")})



class BazeliskBootstrapTest(unittest.TestCase):
    def test_archive_keeps_amd64_and_arm64_launcher_selection_separate(self):
        job = jobs()['archive']
        selected = steps(job)
        setup = selected['Set up Bazel']
        self.assertEqual(step_value(setup, 'bazelisk-version', 10), '${{ matrix.bazelisk_version }}')
        header = job.split("    steps:\n", 1)[0]
        row_sets = [json.loads(value) for value in re.findall(r"'(\[\{.*?\}\])'", header)]
        self.assertEqual(len(row_sets), 2)
        rows = {row["name"]: row for row in row_sets[1]}
        self.assertEqual(set(rows), {'AMD64', 'ARM64'})
        self.assertEqual(rows['AMD64']['bazelisk_version'], '')
        self.assertEqual(rows['ARM64']['bazelisk_version'], '1.29.0')
        prepare = selected['Prepare checksum-pinned Bazelisk']
        self.assertEqual(re.search(r"^        if: ([^\n]+)$", prepare, re.MULTILINE)[1],
                         "matrix.architecture == 'amd64'")
        self.assertNotIn('continue-on-error', prepare)
        for key, value in (('bazelisk-cache', 'true'), ('repository-cache', 'true'),
                           ('disk-cache', '${{ matrix.disk_cache }}'), ('cache-version', '8.5.1')):
            self.assertEqual(step_value(setup, key, 10), value)
        names = list(selected)
        self.assertLess(names.index('Check out the pinned GitHub input reader'),
                        names.index('Prepare checksum-pinned Bazelisk'))
        self.assertLess(names.index('Prepare checksum-pinned Bazelisk'), names.index('Set up Bazel'))

    def test_archive_reuses_verified_bazelisk_and_separate_safe_receipt(self):
        selected = steps(jobs()['archive'])
        self.assertIn('Prepare checksum-pinned Bazelisk', selected)
        reader = selected['Check out the pinned GitHub input reader']
        self.assertEqual(step_value(reader, 'ref', 10), '997796400cc3eefc1f0590b227cd9b74c2141424')
        self.assertIn('ci/github_sources/bazelisk-1.29.0-linux-amd64.json', reader)
        script = shell(selected['Prepare checksum-pinned Bazelisk'])
        validation = '''python3 - <<'PYRECEIPT'
from pathlib import Path
from tools.bazel.ci.public_artifacts import load_json, safe_json
safe_json(load_json(Path("artifacts/bazelisk-inputs/bazelisk-bootstrap.json").read_bytes()))
PYRECEIPT
'''
        self.assertIn(validation, script)
        donor = shell(steps(jobs()['swss_layer_native'])['Prepare checksum-pinned Bazelisk'])
        self.assertEqual(script.replace(validation, '').replace(
            'artifacts/bazelisk-inputs/bazelisk-bootstrap.json', 'artifacts/swss-layer/bazelisk-bootstrap.json'), donor)
        retain = selected['Retain verified Bazelisk input provenance']
        self.assertEqual(step_value(retain, 'if'),
                         "always() && matrix.architecture == 'amd64' && steps.bazelisk_bootstrap.outcome == 'success'")
        self.assertEqual(step_value(retain, 'path', 10), 'artifacts/bazelisk-inputs/bazelisk-bootstrap.json')
        self.assertEqual(step_value(retain, 'if-no-files-found', 10), 'error')
        self.assertEqual(step_value(retain, 'name', 10),
                         'bazelisk-inputs-${{ github.job }}-${{ strategy.job-index || 0 }}-${{ github.run_attempt }}')
        self.assertEqual(step_value(selected['Retain verified Bazel rule input provenance'], 'path', 10),
                         'artifacts/rule-inputs/rule-sources.json')

    def test_checks_use_verified_common_mirrors_before_gitlab_fetch(self):
        import ast
        import hashlib
        job = jobs()["checks"]
        selected = steps(job)
        script = shell(selected["Configure the verified source mirror"])
        code = ast.parse("\n".join(script.splitlines()[1:-1]))
        constants = {node.targets[0].id: node.value.value for node in code.body
                     if isinstance(node, ast.Assign) and len(node.targets) == 1
                     and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
        self.assertEqual(constants["url"],
                         "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
                         "997796400cc3eefc1f0590b227cd9b74c2141424/ci/source_mirrors.cfg")
        fixture = ROOT / "tools/bazel/tests/fixtures/source-mirrors-99779640.cfg"
        self.assertEqual(constants["expected"], hashlib.sha256(fixture.read_bytes()).hexdigest())
        self.assertIn("if hashlib.sha256(data).hexdigest() != expected:", script)
        names = list(selected)
        self.assertLess(names.index("Configure the verified source mirror"),
                        names.index("Prepare checksum-pinned Bazel rule archives"))
        self.assertLess(names.index("Prepare checksum-pinned Bazel rule archives"),
                        names.index("Retain the verified GitLab source downloads"))

    propagated = ('orchagent', 'sflow', 'otel', 'dash_ha', 'sysmgr', 'dash_engine', 'teamd')

    def native_steps(self, family):
        return ''.join(step_blocks(jobs()[family + '_native']))

    def test_rule_archives_use_restored_cache_and_existing_distdir(self):
        selections = {
            "swss_native": (ROOT / ".github/workflows/bazel-swss-native.yml").read_text(),
            "swss_layer_native": jobs()["swss_layer_native"],
            "frr_native": (ROOT / ".github/workflows/bazel-frr.yml").read_text(),
        }
        selections.update({family + '_native': self.native_steps(family) for family in self.propagated})
        reader = textwrap.dedent(r'''
            import json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            options = dict(zip(args[::2], args[1::2]))
            assert set(options) == {'--manifest', '--distdir', '--repository-cache',
                                    '--cache-dir', '--receipt', '--github-token-env'}
            assert options['--manifest'] == 'tools/bazel/ci/sonic-buildimage-rule-inputs.json'
            assert Path(options['--manifest']).is_file()
            assert options['--github-token-env'] == 'GITHUB_TOKEN'
            assert os.environ['GITHUB_TOKEN'] == 'fixture-token'
            repository = Path(os.environ['HOME']) / '.cache/bazel-repo'
            assert Path(options['--repository-cache']) == repository
            assert Path(options['--cache-dir']) == Path(os.environ['HOME']) / '.cache/sonic-github-sources'
            distdir = Path.cwd() / 'artifacts/source-downloads'
            assert Path(options['--distdir']) == distdir
            assert options['--receipt'] == os.environ['EXPECTED_RECEIPT']
            assert (distdir / 'pigz-2.8.tar.gz').read_bytes() == b'existing pigz'
            data = (repository / 'fixture-archive').read_bytes()
            if int(os.environ['READER_EXIT']):
                raise SystemExit(int(os.environ['READER_EXIT']))
            (distdir / 'rules_cc-0.2.22.tar.gz').write_bytes(data)
            receipt = Path(options['--receipt'])
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({'origin': 'repository-cache'}))
        ''')
        for name, job in selections.items():
            prepare = steps(job)["Prepare checksum-pinned Bazel rule archives"]
            self.assertNotIn("continue-on-error", prepare)
            self.assertNotIn("if:", prepare)
            self.assertLess(job.index("bazel-contrib/setup-bazel@"),
                            job.index("Prepare checksum-pinned Bazel rule archives"))
            if name != "frr_native":
                self.assertLess(job.index("Prepare the checksum-pinned pigz source archive"),
                                job.index("Prepare checksum-pinned Bazel rule archives"))
            self.assertIn('common --distdir=%s', job)
            receipt_name = ("artifacts/rule-sources.json" if name in ("swss_native", "frr_native")
                            else "artifacts/" + name.removesuffix('_native').replace('_', '-') + "/rule-sources.json")
            for reader_exit in (23, 0):
                with self.subTest(workflow=name, reader_exit=reader_exit), tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    commands = directory / "commands"
                    commands.mkdir()
                    (commands / "python3").symlink_to(sys.executable)
                    inputs = directory / ".bazel-inputs/github-reader/ci"
                    (inputs / "github_sources").mkdir(parents=True)
                    (inputs / "github_sources.py").write_text(reader)
                    manifest = directory / "tools/bazel/ci/sonic-buildimage-rule-inputs.json"
                    manifest.parent.mkdir(parents=True)
                    manifest.write_text("{}\n")
                    repository = directory / ".cache/bazel-repo"
                    repository.mkdir(parents=True)
                    (repository / "fixture-archive").write_bytes(b'cached archive fixture')
                    distdir = directory / "artifacts/source-downloads"
                    distdir.mkdir(parents=True)
                    (distdir / "pigz-2.8.tar.gz").write_bytes(b'existing pigz')
                    result = subprocess.run(
                        ["/bin/bash", "-e", "-o", "pipefail", "-c", shell(prepare)], cwd=directory,
                        env={"PATH": str(commands), "HOME": str(directory), "PWD": str(directory),
                             "GITHUB_TOKEN": "fixture-token", "EXPECTED_RECEIPT": receipt_name,
                             "READER_EXIT": str(reader_exit), "GITHUB_JOB": name}, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, reader_exit, result.stdout + result.stderr)
                    self.assertEqual((distdir / "pigz-2.8.tar.gz").read_bytes(), b'existing pigz')
                    self.assertEqual((repository / "fixture-archive").read_bytes(), b'cached archive fixture')
                    self.assertEqual((directory / receipt_name).exists(), reader_exit == 0)
                    if name == "frr_native":
                        rc = directory / ".bazelrc.user"
                        self.assertEqual(rc.exists(), reader_exit == 0)
                        if not reader_exit:
                            self.assertEqual(rc.read_text(), "common --distdir=" + str(distdir) + "\n")
                    archive = distdir / "rules_cc-0.2.22.tar.gz"
                    self.assertEqual(archive.exists(), reader_exit == 0)
                    if reader_exit == 0:
                        self.assertEqual(archive.read_bytes(), b'cached archive fixture')

    def test_only_verified_reader_success_exposes_executable_on_path(self):
        scripts = {
            "swss_native": shell(steps((ROOT / ".github/workflows/bazel-swss-native.yml").read_text())[
                "Prepare checksum-pinned Bazelisk"]),
            "swss_layer_native": shell(steps(jobs()["swss_layer_native"])[
                "Prepare checksum-pinned Bazelisk"]),
            "frr_native": shell(steps((ROOT / ".github/workflows/bazel-frr.yml").read_text())[
                "Prepare checksum-pinned Bazelisk"]),
        }
        scripts.update({family + '_native': shell(steps(self.native_steps(family))[
            "Prepare checksum-pinned Bazelisk"]) for family in self.propagated})
        reader = textwrap.dedent(r'''
            import json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            assert args[args.index('--github-token-env') + 1] == 'GITHUB_TOKEN'
            assert os.environ['GITHUB_TOKEN'] == 'fixture-token'
            assert Path(args[args.index('--manifest') + 1]).is_file()
            calls = Path(os.environ['CALLS'])
            calls.write_text('reader\n')
            binary = Path(args[args.index('--distdir') + 1]) / 'bazel'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'fixture bytes; never executed\n')
            binary.chmod(0o600)
            # Leave partial bytes even on failure: the shell must not expose them.
            if int(os.environ['READER_EXIT']):
                raise SystemExit(int(os.environ['READER_EXIT']))
            receipt = Path(args[args.index('--receipt') + 1])
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({'verified': True}))
            calls.write_text('reader\nverified\n')
        ''')
        chmod = "#!" + sys.executable + "\n" + textwrap.dedent(r'''
            import os, sys
            from pathlib import Path
            binary = Path(os.environ['RUNNER_TEMP']) / 'sonic-bazelisk/bin/bazel'
            assert sys.argv[1:] == ['0755', str(binary)]
            calls = Path(os.environ['CALLS'])
            assert calls.read_text() == 'reader\nverified\n'
            assert Path(os.environ['GITHUB_PATH']).read_text() == ''
            binary.chmod(0o755)
            calls.write_text('reader\nverified\nchmod\n')
        ''')
        for name, script in scripts.items():
            for reader_exit in (23, 0):
                with self.subTest(workflow=name, reader_exit=reader_exit), tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    commands = directory / "commands"
                    commands.mkdir()
                    (commands / "python3").symlink_to(sys.executable)
                    (commands / "chmod").write_text(chmod)
                    (commands / "chmod").chmod(0o755)
                    inputs = directory / ".bazel-inputs/github-reader/ci"
                    (inputs / "github_sources").mkdir(parents=True)
                    (inputs / "github_sources.py").write_text(reader)
                    (inputs / "github_sources/bazelisk-1.29.0-linux-amd64.json").write_text("{}\n")
                    path_file = directory / "github-path"
                    path_file.write_text("")
                    calls = directory / "calls"
                    runner_temp = directory / "runner-temp"
                    result = subprocess.run(
                        ["/bin/bash", "-e", "-o", "pipefail", "-c", script], cwd=directory,
                        env={"PATH": str(commands), "HOME": str(directory),
                             "RUNNER_TEMP": str(runner_temp), "GITHUB_PATH": str(path_file),
                             "GITHUB_TOKEN": "fixture-token", "CALLS": str(calls),
                             "READER_EXIT": str(reader_exit), "GITHUB_JOB": name},
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, reader_exit, result.stdout + result.stderr)
                    binary = runner_temp / "sonic-bazelisk/bin/bazel"
                    self.assertEqual(binary.stat().st_mode & 0o777, 0o600 if reader_exit else 0o755)
                    self.assertEqual(calls.read_text(), "reader\n" if reader_exit else "reader\nverified\nchmod\n")
                    self.assertEqual(path_file.read_text(), "" if reader_exit else str(binary.parent) + "\n")


    def test_propagated_jobs_use_the_pinned_reader_and_existing_artifact_roots(self):
        for family in self.propagated:
            with self.subTest(family=family):
                job = self.native_steps(family)
                selected = steps(job)
                names = list(selected)
                checkout = selected['Check out the pinned GitHub input reader']
                self.assertIn('ref: 997796400cc3eefc1f0590b227cd9b74c2141424', checkout)
                self.assertIn('persist-credentials: false', checkout)
                self.assertIn('path: .bazel-inputs/github-reader', checkout)
                self.assertIn('ci/github_sources.py', checkout)
                self.assertIn('ci/github_sources/bazelisk-1.29.0-linux-amd64.json', checkout)
                self.assertNotIn('ci/github_sources/sonic-buildimage-rule-inputs.json', checkout)
                self.assertLess(names.index('Prepare checksum-pinned Bazelisk'), names.index('Set up Bazel'))
                self.assertIn('bazelisk-version: ""', selected['Set up Bazel'])
                self.assertLess(names.index('Preserve configured caches for Draft validation'),
                                names.index('Prepare checksum-pinned Bazel rule archives'))
                self.assertLess(names.index('Prepare the checksum-pinned pigz source archive'),
                                names.index('Prepare checksum-pinned Bazel rule archives'))
                upload = next(block for block in step_blocks(jobs()[family + '_native'])
                              if 'uses: actions/upload-artifact@' in block
                              and not (family == 'dash_engine' and
                                       step_value(block, 'name', 10) == 'dash-engine-incremental-amd64'))
                self.assertEqual(step_value(upload, 'if'),
                                 "always() && steps." + family + "_public.outcome == 'success'")
                paths = step_value(upload, 'path', 10).splitlines()
                root = 'artifacts/' + family.replace('_', '-')
                for filename in ('bazelisk-bootstrap.json', 'rule-sources.json'):
                    self.assertEqual(paths.count(root + '/' + filename), 1)
                self.assertFalse(any('source-downloads' in path or '.bazel-inputs' in path for path in paths))

    def test_propagated_receipts_use_existing_public_sanitizers(self):
        import contextlib
        import io
        import socket
        from unittest import mock
        from tools.bazel.ci import dash_engine_public
        previous = Path.cwd()
        values = (None, b'{"files": [], "sources": []}',
                  b'{"authorization": "private-sentinel"}', b'{"malformed":')
        for family in self.propagated:
            job = self.native_steps(family)
            review = next(value for value in steps(job).values()
                          if 'id: ' + family + '_public' in value)
            code = None if family == 'dash_engine' else '\n'.join(shell(review).splitlines()[1:-1])
            for filename in ('bazelisk-bootstrap.json', 'rule-sources.json'):
                for value in values:
                    with self.subTest(family=family, filename=filename, value=value), tempfile.TemporaryDirectory() as temp:
                        workspace = Path(temp)
                        root = workspace / 'artifacts' / family.replace('_', '-')
                        root.mkdir(parents=True)
                        if value is not None:
                            (root / filename).write_bytes(value)
                        output = io.StringIO()
                        try:
                            os.chdir(workspace)
                            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                                    mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('process denied')), \
                                    mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network denied')):
                                def execute():
                                    if code is None:
                                        dash_engine_public.review(root)
                                    else:
                                        exec(compile(code, '<actual-native-public-evidence-step>', 'exec'), {})

                                if value in values[:2]:
                                    execute()
                                else:
                                    with self.assertRaises(ValueError):
                                        execute()
                            self.assertEqual(output.getvalue(), '')
                            if value is not None:
                                self.assertEqual((root / filename).read_bytes(), value)
                        finally:
                            os.chdir(previous)

    def test_frr_download_receipts_gate_upload_without_exposing_private_values(self):
        import contextlib
        import io
        import socket
        from unittest import mock
        workflow = (ROOT / ".github/workflows/bazel-frr.yml").read_text()
        self.assertIn("if: always() && steps.download_receipts.outcome == 'success'", workflow)
        self.assertIn("!artifacts/source-downloads/**", workflow)
        script = shell(steps(workflow)["Validate public download receipts"]).splitlines()
        code = "\n".join(script[1:-1])
        previous = Path.cwd()
        for value in (None, b'{"files": [], "sources": []}',
                      b'{"authorization": "private-sentinel"}', b'{"malformed":'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                workspace = Path(temp)
                (workspace / 'artifacts').mkdir()
                if value is not None:
                    (workspace / 'artifacts/rule-sources.json').write_bytes(value)
                output = io.StringIO()
                try:
                    os.chdir(workspace)
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                            mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('process denied')), \
                            mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network denied')):
                        if value in (None, b'{"files": [], "sources": []}'):
                            exec(compile(code, '<FRR-download-receipt-step>', 'exec'), {})
                        else:
                            with self.assertRaises(ValueError):
                                exec(compile(code, '<FRR-download-receipt-step>', 'exec'), {})
                    self.assertEqual(output.getvalue(), '')
                finally:
                    os.chdir(previous)


class SourceWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-workflow-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.commands = self.directory / "commands"
        self.commands.mkdir()
        (self.directory / "artifacts/vs").mkdir(parents=True)
        for name in (".bazelversion", "tools/bazel/build_helpers.py",
                     "tools/bazel/cache/credentials.py",
                     "platform/vs/docker-syncd-vs/bazel/apt.lock.json",
                     "platform/vs/docker-syncd-vs/BUILD.bazel"):
            fixture = self.directory / name
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes((ROOT / name).read_bytes())
        self.calls = self.directory / "calls.jsonl"
        recorder = "#!" + sys.executable + "\n" + textwrap.dedent(r'''
            import json, os, shlex, subprocess, sys
            from pathlib import Path
            name = Path(sys.argv[0]).name
            args = sys.argv[1:]
            with Path(os.environ['CALLS']).open('a') as output:
                output.write(json.dumps({'name': name, 'args': args,
                    'environment': {key: os.environ.get(key) for key in
                                    ('BAZEL', 'BAZEL_CONTAINER_ARGS')}}) + '\n')
            if name == 'rm' or 'clean' in args or 'cleanall' in args:
                raise SystemExit('Source validation must preserve existing build state')
            source_runner = name == 'python3' and 'tools/bazel/ci/container.py' in args
            if os.environ.get('HOSTED_CHECKS') == '1':
                if source_runner or (name == 'bazel' and any(arg in ('build', 'test') for arg in args)):
                    raise SystemExit('Hosted regression checks must not compile source packages')
            if name == 'make' and 'sonic-slave-run' in args:
                if int(os.environ.get('MAKE_EXIT', '0')):
                    raise SystemExit(int(os.environ['MAKE_EXIT']))
                values = dict(arg.split('=', 1) for arg in args if '=' in arg)
                # Model Docker's explicit -e forwarding instead of inheriting
                # the outer workflow's Bazel settings by accident.
                environment = dict(os.environ)
                for key in ('BAZEL', 'BAZEL_CONTAINER_ARGS'):
                    environment.pop(key, None)
                extra = shlex.split(values.get('SONIC_BUILDER_EXTRA_CMDLINE', ''))
                for index, arg in enumerate(extra):
                    if arg == '-e':
                        key, separator, value = extra[index + 1].partition('=')
                        environment[key] = value if separator else os.environ[key]
                raise SystemExit(subprocess.run(
                    ['bash', '-e', '-o', 'pipefail', '-c', values['SONIC_RUN_CMDS']],
                    env=environment).returncode)
            if source_runner:
                raise SystemExit(int(os.environ.get('SOURCE_EXIT', '0')))
            if name == 'python3' and args[:1] == ['tools/bazel/build_helpers.py']:
                raise SystemExit(subprocess.run([sys.executable, *args]).returncode)
            if name == 'bazel' and args == ['--version']:
                print('bazel ' + Path('.bazelversion').read_text().strip())
            if name == 'git':
                if args[:2] == ['rev-parse', 'HEAD']:
                    print('a' * 40)
                elif args[:1] == ['check-ignore']:
                    print('MODULE.bazel.lock')
            ''')
        for name in ("make", "python3", "bazel", "apt-get", "git", "df", "du", "rm", "cp"):
            executable = self.commands / name
            executable.write_text(recorder)
            executable.chmod(0o755)
        self.environment = dict(
            os.environ, PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            CALLS=str(self.calls), BAZEL=str(self.commands / "bazel"),
            BAZEL_CONTAINER_ARGS="--distdir='/bazel_cache/source downloads' --remote_download_all",
            BAZEL_CONTAINER_CACHE_DIR=str(self.directory / "build-cache"),
            GITHUB_WORKSPACE=str(self.directory), GITHUB_OUTPUT=str(self.directory / "github-output"),
            GITHUB_STEP_SUMMARY=str(self.directory / "github-summary"),
            SONIC_CI_CONTAINER_LABEL="test-source-validation=one-job",
        )

    def execute(self, script, **environment):
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                              cwd=self.directory, env=dict(self.environment, **environment),
                              capture_output=True, text=True)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_owned_source_companions_keep_source_only_scope(self):
        text = WORKFLOW.read_text()
        fixture_conditions = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.*?) \}\}", text)
        self.assertEqual(len(fixture_conditions), 2)
        vs_condition = re.search(r"^    if: >-\n(.*?)(?=^    runs-on:)",
                                 jobs()["vs_container"], re.MULTILINE | re.DOTALL)[1]

        def evaluate(expression, number, branch, owner):
            values = {
                "github.event_name": "pull_request",
                "github.event.pull_request.number": number,
                "github.event.pull_request.head.ref": branch,
                "github.event.pull_request.head.repo.full_name": owner,
                "github.repository": "securely1g/sonic-buildimage",
                "github.head_ref": branch,
                "inputs.source_image": "none",
                "github.ref": "refs/pull/999/merge",
                "inputs.skip_vs": False,
            }
            expression = re.sub(r"(?:github|inputs)\.[A-Za-z0-9_.]+",
                                lambda item: repr(values[item[0]]), expression)
            expression = expression.replace("&&", " and ").replace("||", " or ")
            expression = re.sub(r"!(?!=)", " not ", expression)
            return eval(" ".join(expression.split()), {"__builtins__": {},
                "contains": lambda values, item: item in values, "fromJSON": json.loads})

        for number, branch, owner, source_only, run_vs in (
            (51, "frr-native-bazel", "songqing-sq/sonic-buildimage", True, False),
            (61, "pr/bazel-libteam", "songqing-sq/sonic-buildimage", True, False),
            (999, "codex/libteam-current-native-inputs", "securely1g/sonic-buildimage", True, False),
            (999, "codex/libteam-current-native-inputs", "example/sonic-buildimage", False, False),
            (999, "codex/libteam-current-native-inputs-extra", "securely1g/sonic-buildimage", False, True),
            (999, "frr-native-bazel", "securely1g/sonic-buildimage", True, False),
            (999, "frr-native-bazel", "example/sonic-buildimage", False, False),
            (999, "frr-native-bazel-extra", "securely1g/sonic-buildimage", False, True),
            (999, "codex/dash-engine-review-catalog", "securely1g/sonic-buildimage", True, False),
            (999, "codex/dash-engine-review-catalog", "example/sonic-buildimage", False, False),
            (999, "codex/dash-engine-review-catalog-extra", "securely1g/sonic-buildimage", False, True),
            (999, "unrelated", "securely1g/sonic-buildimage", False, True),
        ):
            with self.subTest(number=number, branch=branch, owner=owner):
                for expression in fixture_conditions:
                    self.assertEqual(evaluate(expression, number, branch, owner), source_only)
                self.assertEqual(evaluate(vs_condition, number, branch, owner), run_vs)

    def test_hosted_regressions_gate_vs_without_source_compilation(self):
        workflow_jobs = jobs()
        self.assertNotIn("source", workflow_jobs)
        self.assertNotIn("syncd-contracts", workflow_jobs)
        checks = workflow_jobs["checks"]
        self.assertIn("name: OCI regression checks (AMD64)", checks)
        self.assertIn("runs-on: ubuntu-24.04", checks)
        self.assertRegex(workflow_jobs["vs"], r"(?m)^    needs: (?:checks|\[checks\])$")
        for name, step in steps(checks).items():
            script = shell(step)
            if script is not None:
                with self.subTest(step=name):
                    result = self.execute(script, HOSTED_CHECKS="1")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded()
        self.assertTrue(any(call["name"] == "python3" and
                            "unittest" in call["args"] for call in calls))
        self.assertFalse(any("tools/bazel/ci/container.py" in call["args"] or
                             (call["name"] == "bazel" and
                              any(arg in ("build", "test") for arg in call["args"]))
                             for call in calls))

    def test_make_source_validation_reuses_outputs_and_propagates_failures(self):
        workflow_steps = steps(jobs()["vs"])
        names = list(workflow_steps)
        build_steps = [name for name, step in workflow_steps.items()
                       if re.search(r"(?m)^        id: vs_build$", step)]
        self.assertEqual(len(build_steps), 1)
        families = (
            ("SWSS", "//dockers/docker-orchagent", "rules/docker-orchagent.mk", "artifacts/swss", False),
            ("Syncd", "//platform/vs/docker-syncd-vs", "platform/vs/docker-syncd-vs.mk", "artifacts/syncd-vs/bazel", True),
        )
        lock = self.directory / "MODULE.bazel.lock"
        lock.write_text("already-resolved Make build\n")
        for family, owner, source, artifacts, dash in families:
            name = "Validate " + family + " source packages from the Make build"
            self.assertLess(names.index(build_steps[0]), names.index(name))
            self.assertIn("if: ${{ !cancelled() && steps.vs_build.outcome == 'success' }}", workflow_steps[name])
            for source_exit, make_exit in ((0, 0), (19, 0), (0, 23)):
                with self.subTest(family=family, source_exit=source_exit, make_exit=make_exit):
                    self.calls.unlink(missing_ok=True)
                    result = self.execute(shell(workflow_steps[name]),
                                          SOURCE_EXIT=str(source_exit), MAKE_EXIT=str(make_exit))
                    self.assertEqual(result.returncode, source_exit or make_exit,
                                     result.stdout + result.stderr)
                    self.assertEqual(lock.read_text(), "already-resolved Make build\n")
                    calls = self.recorded()
                    make = [call for call in calls if call["name"] == "make"]
                    self.assertEqual(len(make), 1)
                    self.assertEqual(make[0]["args"][:2], ["-f", "Makefile.work"])
                    self.assertIn("BLDENV=trixie", make[0]["args"])
                    self.assertIn("sonic-slave-run", make[0]["args"])
                    values = dict(arg.split("=", 1) for arg in make[0]["args"] if "=" in arg)
                    self.assertRegex(values["SONIC_RUN_CMDS"], r"\bbash artifacts/vs/[\w-]+\.sh$")
                    runner = [call for call in calls if "tools/bazel/ci/container.py" in call["args"]]
                    self.assertEqual(len(runner), 0 if make_exit else 1)
                    if runner:
                        args = runner[0]["args"]
                        self.assertIn("--reuse-build", args)
                        for flag, value in (("--container", owner), ("--source-file", source),
                                            ("--artifacts", artifacts), ("--bazel", self.environment["BAZEL"])):
                            self.assertEqual(args[args.index(flag) + 1], value)
                        self.assertEqual("--bazel-arg=--config=syncd-dash" in args, dash)
                        self.assertFalse(any("source_config_engine" in arg for arg in args))
                        for option in ("--distdir=/bazel_cache/source downloads", "--remote_download_all",
                                       "--platforms=@sonic_build_infra//platforms:x86_64_trixie",
                                       "--host_platform=@sonic_build_infra//platforms:x86_64_trixie",
                                       "--repository_cache=" + self.environment["BAZEL_CONTAINER_CACHE_DIR"] + "/repository_cache",
                                       "--disk_cache=" + self.environment["BAZEL_CONTAINER_CACHE_DIR"] + "/disk_cache"):
                            self.assertIn("--bazel-arg=" + option, args)
                        self.assertEqual(runner[0]["environment"],
                                         {key: self.environment[key] for key in ("BAZEL", "BAZEL_CONTAINER_ARGS")})
                    analyses = [call for call in calls if "tools/bazel/ci/bazel_commands.py" in call["args"]]
                    self.assertEqual(len(analyses), int(dash and not source_exit and not make_exit))
                    self.assertFalse(any(call["name"] == "rm" or "clean" in call["args"] for call in calls))

class RootLockFailureEvidenceTest(unittest.TestCase):
    """Execute the actual two evidence snippets without a build or resolver."""

    def snippets(self):
        native = (ROOT / '.github/workflows/bazel-swss-native.yml').read_text()
        cases = (
            (steps(native)['Prepare public validation evidence'], 'artifacts',
             'build-test-summary.json', "steps.public.outcome == 'success'", native),
            (steps(jobs()['swss_layer_native'])['Prepare shared SWSS image evidence'],
             'artifacts/swss-layer', 'build-summary.json',
             "steps.swss_layer_public.outcome == 'success'", jobs()['swss_layer_native']),
        )
        for step, destination, summary, gate, workflow in cases:
            self.assertIn('if: always()', step)
            self.assertIn('if: always() && ' + gate, workflow)
            script = shell(step).splitlines()
            self.assertRegex(script[0], r"^python3 - <<'PY(?:CODE)?'$")
            self.assertIn(script[-1], ('PY', 'PYCODE'))
            yield '\n'.join(script[1:-1]), destination, summary

    def image_snippets(self):
        cases = (
            ('orchagent_native', 'Review public Orchagent evidence', 'orchagent', 'orchagent_public'),
            ('dash_ha_native', 'Prepare reviewed public evidence', 'dash-ha', 'dash_ha_public'),
            ('sysmgr_native', 'Prepare reviewed Sysmgr public evidence', 'sysmgr', 'sysmgr_public'),
            ('teamd_native', 'Prepare reviewed Teamd evidence', 'teamd', 'teamd_public'),
        )
        for job, name, folder, review in cases:
            workflow = jobs()[job]
            step = steps(workflow)[name]
            self.assertIn('if: always()', step)
            self.assertIn("if: always() && steps." + review + ".outcome == 'success'", workflow)
            script = shell(step).splitlines()
            self.assertRegex(script[0], r"^python3 - <<'PY(?:CODE|ORCH)?'$")
            self.assertIn(script[-1], ('PY', 'PYCODE', 'PYORCH'))
            yield '\n'.join(script[1:-1]), 'artifacts/' + folder

    @staticmethod
    def lock(**extra):
        return {'lockFileVersion': 13, 'registryFileHashes': {}, 'moduleExtensions': {}, **extra}

    def execute(self, code, workspace):
        import contextlib
        import io
        import socket
        from unittest import mock
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                    mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('process denied')), \
                    mock.patch.object(os, 'system', side_effect=AssertionError('process denied')), \
                    mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network denied')), \
                    mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network denied')):
                try:
                    exec(compile(code, '<actual-public-evidence-step>', 'exec'), {'__name__': '__main__'})
                except SystemExit as error:
                    return str(error), output.getvalue()
            return None, output.getvalue()
        finally:
            os.chdir(previous)

    def rejected(self, code, workspace, destination):
        error, output = self.execute(code, workspace)
        self.assertEqual(error, 'Root module lock is not safe to publish')
        self.assertEqual(output, '')
        self.assertFalse((workspace / destination / 'MODULE.bazel.lock').exists())

    def test_image_evidence_survives_missing_artifact_directory(self):
        for code, destination in self.image_snippets():
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as temp:
                workspace = Path(temp)
                folder = workspace / destination
                self.assertFalse(folder.exists())
                self.assertEqual(self.execute(code, workspace), (None, ''))
                self.assertEqual(json.loads((folder / 'build-summary.json').read_text()), {})
                self.assertEqual(sorted(path.name for path in folder.iterdir()), ['build-summary.json'])

    def test_image_evidence_preserves_safe_data_and_rejects_private_data(self):
        from tools.bazel.ci.public_artifacts import PublicArtifactError
        for code, destination in self.image_snippets():
            for private in (False, True):
                with self.subTest(destination=destination, private=private), tempfile.TemporaryDirectory() as temp:
                    workspace = Path(temp)
                    folder = workspace / destination
                    folder.mkdir(parents=True)
                    value = {'authorization': 'private-sentinel'} if private else {'files': [], 'status': 'verified'}
                    original = (json.dumps(value) + '\n').encode()
                    (folder / 'rule-sources.json').write_bytes(original)
                    if private:
                        with self.assertRaises(PublicArtifactError):
                            self.execute(code, workspace)
                        self.assertFalse((folder / 'build-summary.json').exists())
                        self.assertFalse((folder / 'public-receipt.json').exists())
                    else:
                        receipt = {'status': 'failed', 'architecture': 'amd64', 'argv': ['private-sentinel']}
                        (folder / 'build-receipt.json').write_text(json.dumps(receipt))
                        self.assertEqual(self.execute(code, workspace), (None, ''))
                        self.assertEqual(json.loads((folder / 'build-summary.json').read_text()), {})
                        self.assertEqual(json.loads((folder / 'public-receipt.json').read_text()),
                                         {'status': 'failed', 'architecture': 'amd64'})
                        self.assertEqual(json.loads((folder / 'build-receipt.json').read_text()), receipt)
                    self.assertEqual((folder / 'rule-sources.json').read_bytes(), original)

    def test_missing_root_preserves_summaries_and_existing_safe_evidence(self):
        for code, destination, summary in self.snippets():
            for old_lock in (False, True):
                with self.subTest(destination=destination, old_lock=old_lock), tempfile.TemporaryDirectory() as temp:
                    workspace = Path(temp)
                    folder = workspace / destination
                    folder.mkdir(parents=True)
                    receipt = b'{"files": [], "status": "verified"}\n'
                    (folder / 'rule-sources.json').write_bytes(receipt)
                    (folder / 'receipt.json').write_text('{"status":"failed","architecture":"amd64","argv":["private"]}')
                    value = json.dumps(self.lock()).encode()
                    if old_lock:
                        (folder / 'MODULE.bazel.lock').write_bytes(value)
                    self.assertEqual(self.execute(code, workspace), (None, ''))
                    self.assertEqual(json.loads((folder / summary).read_text()), {})
                    self.assertEqual((folder / 'rule-sources.json').read_bytes(), receipt)
                    self.assertEqual((folder / 'MODULE.bazel.lock').exists(), old_lock)
                    if old_lock:
                        self.assertEqual((folder / 'MODULE.bazel.lock').read_bytes(), value)
                    if destination.endswith('swss-layer'):
                        self.assertEqual(json.loads((folder / 'public-receipt.json').read_text()),
                                         {'status': 'failed', 'architecture': 'amd64'})

    def test_valid_root_is_copied_byte_exactly_without_modifying_it(self):
        value = b' { "lockFileVersion": 13, "registryFileHashes": {}, "moduleExtensions": {} }\n\n'
        for code, destination, _ in self.snippets():
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as temp:
                workspace = Path(temp)
                source = workspace / 'MODULE.bazel.lock'
                source.write_bytes(value)
                folder = workspace / destination
                folder.mkdir(parents=True)
                (folder / source.name).write_bytes(json.dumps(self.lock(facts={'old': True})).encode())
                self.assertEqual(self.execute(code, workspace), (None, ''))
                self.assertEqual(source.read_bytes(), value)
                self.assertEqual((folder / source.name).read_bytes(), value)
                self.assertFalse(list(folder.glob('.module-lock-*')))

    def test_invalid_or_private_root_never_becomes_public(self):
        values = [
            b'{"invalid-private-sentinel":',
            b'{"lockFileVersion":13,"lockFileVersion":13,"registryFileHashes":{},"moduleExtensions":{}}',
            json.dumps(self.lock(facts={'value': float('nan')})).encode(),
            json.dumps({'private-sentinel': 'invalid schema'}).encode(),
            json.dumps(self.lock(facts={'authorization': 'private-sentinel'})).encode(),
            json.dumps(self.lock(facts={'value': 'ghp_' + 'A' * 30})).encode(),
            json.dumps(self.lock(facts={'envVariables': {'secret': 'private-sentinel'}})).encode(),
            json.dumps(self.lock(facts={'value': '/home/fixture/.cache/private-sentinel'})).encode(),
            json.dumps(self.lock(facts={'value': 'https://user:private-sentinel@example.invalid/input'})).encode(),
            json.dumps(self.lock(facts={'value': 'https://example.invalid/input?token=private-sentinel'})).encode(),
        ]
        for code, destination, _ in self.snippets():
            for number, value in enumerate(values):
                with self.subTest(destination=destination, case=number), tempfile.TemporaryDirectory() as temp:
                    workspace = Path(temp)
                    (workspace / 'MODULE.bazel.lock').write_bytes(value)
                    self.rejected(code, workspace, destination)

    def test_source_types_and_actual_size_limit_fail_before_copy(self):
        for code, destination, _ in self.snippets():
            for kind in ('symlink', 'directory', 'fifo', 'oversized'):
                with self.subTest(destination=destination, kind=kind), tempfile.TemporaryDirectory() as temp:
                    workspace = Path(temp)
                    source = workspace / 'MODULE.bazel.lock'
                    if kind == 'symlink':
                        (workspace / 'other').write_text(json.dumps(self.lock()))
                        source.symlink_to('other')
                    elif kind == 'directory':
                        source.mkdir()
                    elif kind == 'fifo':
                        os.mkfifo(source)
                    else:
                        with source.open('wb') as stream:
                            stream.truncate(128 * 1024 * 1024 + 1)
                    self.rejected(code, workspace, destination)

    def test_destination_symlinks_and_nonfiles_are_rejected_without_writes(self):
        for code, destination, _ in self.snippets():
            for kind in ('file-link', 'parent-link', 'directory'):
                with self.subTest(destination=destination, kind=kind), tempfile.TemporaryDirectory() as temp:
                    workspace = Path(temp)
                    (workspace / 'MODULE.bazel.lock').write_text(json.dumps(self.lock()))
                    outside = workspace / 'outside'
                    outside.mkdir()
                    sentinel = outside / 'sentinel'
                    sentinel.write_bytes(b'unchanged')
                    folder = workspace / destination
                    if kind == 'parent-link':
                        (workspace / 'artifacts').symlink_to(outside, target_is_directory=True)
                    else:
                        folder.mkdir(parents=True)
                        if kind == 'file-link':
                            (folder / 'MODULE.bazel.lock').symlink_to(sentinel)
                        else:
                            (folder / 'MODULE.bazel.lock').mkdir()
                    error, output = self.execute(code, workspace)
                    self.assertEqual((error, output), ('Root module lock is not safe to publish', ''))
                    self.assertEqual(sentinel.read_bytes(), b'unchanged')
                    self.assertEqual(sorted(p.name for p in outside.iterdir()), ['sentinel'])

    def test_changed_root_is_rejected_after_validation(self):
        from unittest import mock
        from tools.bazel.ci import public_artifacts
        validate = public_artifacts.dependency_json
        for code, destination, _ in self.snippets():
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as temp:
                workspace = Path(temp)
                source = workspace / 'MODULE.bazel.lock'
                source.write_text(json.dumps(self.lock()))

                def change_after_validation(value, kind):
                    validate(value, kind)
                    source.write_bytes(source.read_bytes() + b' ')

                with mock.patch.object(public_artifacts, 'dependency_json', side_effect=change_after_validation):
                    self.rejected(code, workspace, destination)






if __name__ == "__main__":
    unittest.main()
