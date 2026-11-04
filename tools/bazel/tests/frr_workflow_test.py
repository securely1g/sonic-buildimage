#!/usr/bin/env python3
"""Check immutable manual selections and the FRR control PR's CI boundary."""

from pathlib import Path
import re
import subprocess
import textwrap
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/frr-review-source-inputs"


def condition(expression, *, event="pull_request", branch=BRANCH,
              head_repo="securely1g/sonic-buildimage"):
    expression = " ".join(expression.splitlines())
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.repository": repr("securely1g/sonic-buildimage"),
        "github.event.pull_request.number": "178",
        "github.event_name": repr(event), "github.head_ref": repr(branch),
        "github.ref": repr("refs/heads/review"), "inputs.skip_vs": "True",
    }
    for name, value in values.items():
        expression = expression.replace(name, value)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression)
    return bool(eval(expression, {"__builtins__": {}}, {}))


class FrrWorkflowTest(unittest.TestCase):
    def test_manual_selection_accepts_only_paired_immutable_shas(self):
        workflow = (ROOT / ".github/workflows/bazel-frr-image.yml").read_text()
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
        workflow = (ROOT / ".github/workflows/bazel-frr-image.yml").read_text()
        self.assertEqual(workflow.count("ref: ${{ inputs.source_revision || github.sha }}"), 2)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertIn("BAZEL: ${{ inputs.registry_revision && 'tools/bazel/ci/review_bazel.sh' || 'tools/bazel/ci/frr_draft_bazel.sh' }}", workflow)
        self.assertIn("SONIC_BAZEL_REGISTRY_REVISION: ${{ inputs.registry_revision }}", workflow)
        for phase in ("build", "incremental"):
            self.assertIn("frr_image.py " + phase + ' --artifacts artifacts/frr --bazel "$BAZEL"', workflow)

    def test_only_the_owned_control_pr_skips_vs(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        job = workflow.split("\n  vs_container:\n", 1)[1]
        gate = textwrap.dedent(job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0])
        self.assertFalse(condition(gate))
        self.assertTrue(condition(gate, branch=BRANCH + "-suffix"))
        self.assertTrue(condition(gate, branch="unrelated"))
        self.assertFalse(condition(gate, head_repo="other/fork"))


    def test_official_fallbacks_keep_exact_inputs_and_existing_cache_settings(self):
        workflow = (ROOT / ".github/workflows/bazel-frr-image.yml").read_text()
        marker = "      - name: Add verified official Debian pool fallbacks\n"
        step = workflow.split(marker, 1)[1].split("\n      - ", 1)[0]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'artifacts/frr').mkdir(parents=True)
            original = ('build --disk_cache=/existing/disk\n'
                        'common --repository_cache=/existing/repository\n'
                        'common --distdir=/existing/pigz\n')
            (root / '.bazelrc.user').write_text(original)
            result = subprocess.run(['bash', '-eu', '-c', script], cwd=root,
                env={'PATH': '/usr/bin:/bin', 'RUNNER_TEMP': temporary},
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = (root / 'frr-downloader.cfg').read_text()
            self.assertEqual(config, (root / 'artifacts/frr/downloader.cfg').read_text())
            self.assertEqual((root / '.bazelrc.user').read_text(), original +
                'common --experimental_downloader_config=' + temporary + '/frr-downloader.cfg\n')
        rules = []
        for line in config.splitlines():
            kind, pattern, replacement = line.split()
            self.assertEqual(kind, 'rewrite')
            rules.append((re.compile(pattern), replacement))
        expected = [
            ('debian', '20260727T143429Z', 'main/libf/libfido2/libfido2-dev_1.15.0-1+b1_armhf.deb'),
            ('debian', '20260727T143429Z', 'main/libn/libnsl/libnsl-dev_1.3.0-3+b3_amd64.deb'),
            ('debian', '20260727T143429Z', 'main/r/readline/libreadline-dev_8.2-6_amd64.deb'),
            ('debian', '20260727T143429Z', 'main/a/arping/arping_2.25-1_amd64.deb'),
            ('debian', '20260727T143429Z', 'main/p/python-typing-extensions/python3-typing-extensions_4.13.2-1_all.deb'),
            ('debian-security', '20260726T121236Z', 'updates/main/libs/libsodium/libsodium-dev_1.0.18-1+deb13u1_arm64.deb'),
        ]
        self.assertEqual(len(rules), 4)
        for archive, timestamp, package in expected:
            url = 'snapshot.debian.org/archive/' + archive + '/' + timestamp + '/pool/' + package
            rewritten = []
            for pattern, replacement in rules:
                match = pattern.fullmatch(url)
                if match:
                    rewritten.append(re.sub(r"\$(\d+)", lambda m: match.group(int(m[1])), replacement))
            self.assertEqual(rewritten, ['https://deb.debian.org/' + archive + '/pool/' + package,
                                        'https://' + url])
            for other in (url.replace('/pool/', '/dists/'), url + '?different=1',
                          url + '#fragment', url.replace('snapshot.debian.org', 'other.example'),
                          url.replace(timestamp, 'latest')):
                self.assertFalse(any(pattern.fullmatch(other) for pattern, _ in rules))
        self.assertIn('artifacts/frr/downloader.cfg', workflow)


if __name__ == "__main__":
    unittest.main()
