#!/usr/bin/env python3
"""Check immutable manual selections and the SNMP control PR's CI boundary."""

from pathlib import Path
import hashlib
import json
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/snmp-review-source-inputs"


def condition(expression, *, event="pull_request", branch=BRANCH,
              head_repo="securely1g/sonic-buildimage", source_revision="",
              registry_revision=""):
    expression = " ".join(expression.splitlines())
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.repository": repr("securely1g/sonic-buildimage"),
        "github.event_name": repr(event), "github.head_ref": repr(branch),
        "github.ref": repr("refs/heads/review"), "inputs.skip_vs": "True",
        "inputs.source_revision": repr(source_revision),
        "inputs.registry_revision": repr(registry_revision),
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    return bool(eval(expression, {"__builtins__": {}}, {}))


CONFIG_URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/18e7c652222449b906a17c2fd948e37bcad3324e/ci/source_mirrors.cfg"
CONFIG_SHA = "fa60a63fd62f11e3c097905a3ff2a5dc4b25bd7ba42ebc9877fe7ea4dd1d3ee2"
CONFIG = rb"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# Prefer the official pool for pinned Debian binary packages. Bazel keeps the
# repository rule's checksum; the original snapshot remains the second URL.
# Indexes and source archives must retain their snapshot URLs.
rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian/pool/$2
rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian-security/pool/$2
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
"""


def workflow_step(name):
    workflow = (ROOT / ".github/workflows/bazel-snmp.yml").read_text()
    marker = "      - name: " + name + "\n"
    if workflow.count(marker) != 1:
        raise AssertionError("missing or duplicate workflow step: " + name)
    return workflow.split(marker, 1)[1].split("\n      - ", 1)[0]


def run_transport_steps(work, *, response=CONFIG, selected=True, network_failure=False):
    """Run the real workflow bodies with only transport/pigz/Bazel recorded."""
    mock = work / "mock"
    mock.mkdir()
    (mock / "sitecustomize.py").write_text(
        "import io, json, urllib.request\nfrom pathlib import Path\n"
        "def urlopen(url, timeout):\n"
        "    assert url == " + repr(CONFIG_URL) + "\n"
        "    assert timeout == 60\n"
        "    Path('network.json').write_text(json.dumps({'url': url, 'timeout': timeout}))\n"
        + ("    raise OSError('recorded transport failure')\n" if network_failure else
           "    return io.BytesIO(" + repr(response) + ")\n")
        + "urllib.request.urlopen = urlopen\n")
    pigz = work / "tools/bazel/gzip/prepare_pigz.py"
    pigz.parent.mkdir(parents=True)
    pigz.write_text("import json\nprint(json.dumps({'status': 'recorded-pigz-only'}))\n")
    bazel = mock / "bazel"
    bazel.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > bazel-command.txt\n")
    bazel.chmod(0o755)
    temporary = work / "runner-temp"
    temporary.mkdir()
    scripts = []
    for name in ("Prepare the verified compression source", "Configure verified Debian payload transport"):
        if name.startswith("Configure") and not selected:
            continue
        scripts.append(textwrap.dedent(workflow_step(name).split("        run: |\n", 1)[1]))
    scripts.append("bazel aquery recorded-target")
    return subprocess.run(["bash", "-e", "-c", "\n".join(scripts)], cwd=work,
        env={"PATH": str(mock) + ":/usr/bin:/bin", "PYTHONPATH": str(mock),
             "PYTHONDONTWRITEBYTECODE": "1", "RUNNER_TEMP": str(temporary)},
        capture_output=True, text=True)


class SnmpWorkflowTest(unittest.TestCase):
    def test_transport_is_only_for_paired_manual_inputs(self):
        step = workflow_step("Configure verified Debian payload transport")
        gate = step.split("        if: ", 1)[1].splitlines()[0]
        paired = {"source_revision": "a" * 40, "registry_revision": "b" * 40}
        self.assertTrue(condition(gate, event="workflow_dispatch", **paired))
        for changes in ({}, paired, {"source_revision": "a" * 40},
                        {"registry_revision": "b" * 40}):
            for event in ("pull_request", "push", "workflow_dispatch"):
                if event == "workflow_dispatch" and changes == paired:
                    continue
                with self.subTest(event=event, changes=changes):
                    self.assertFalse(condition(gate, event=event, **changes))

    def test_verified_transport_appends_after_pigz_and_preserves_distdir(self):
        self.assertEqual(hashlib.sha256(CONFIG).hexdigest(), CONFIG_SHA)
        workflow = (ROOT / ".github/workflows/bazel-snmp.yml").read_text()
        self.assertLess(workflow.index("Prepare the verified compression source"),
                        workflow.index("Configure verified Debian payload transport"))
        self.assertLess(workflow.index("Configure verified Debian payload transport"),
                        workflow.index("Build runtime and debug images from source owners"))
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            result = run_transport_steps(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((work / "runner-temp/snmp-source-mirrors.cfg").read_bytes(), CONFIG)
            self.assertEqual((work / ".bazelrc.user").read_text().splitlines(), [
                "common --distdir=" + str(work / "runner-temp/pigz-distdir"),
                "common --experimental_downloader_config=" + str(work / "runner-temp/snmp-source-mirrors.cfg")])
            self.assertEqual(json.loads((work / "network.json").read_text())["url"], CONFIG_URL)
            self.assertEqual((work / "bazel-command.txt").read_text(), "aquery recorded-target\n")

    def test_corrupt_or_failed_transport_stops_before_bazel_and_rc_append(self):
        for response, failure in ((b"corrupt config", False), (CONFIG, True)):
            with self.subTest(network_failure=failure), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                result = run_transport_steps(work, response=response, network_failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((work / "bazel-command.txt").exists())
                self.assertFalse((work / "runner-temp/snmp-source-mirrors.cfg").exists())
                self.assertEqual((work / ".bazelrc.user").read_text(),
                    "common --distdir=" + str(work / "runner-temp/pigz-distdir") + "\n")

    def test_default_mode_keeps_original_pigz_without_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            result = run_transport_steps(work, selected=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((work / "network.json").exists())
            self.assertFalse((work / "runner-temp/snmp-source-mirrors.cfg").exists())
            self.assertEqual((work / ".bazelrc.user").read_text(),
                "common --distdir=" + str(work / "runner-temp/pigz-distdir") + "\n")

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
