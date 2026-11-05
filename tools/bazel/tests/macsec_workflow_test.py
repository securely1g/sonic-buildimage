#!/usr/bin/env python3
"""Check immutable manual selections and the MACsec control PR's CI boundary."""

from pathlib import Path
import hashlib
import json
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[3]
BRANCH = "codex/macsec-local-package-handoff"


def condition(expression, *, event="pull_request", branch=BRANCH,
              head_repo="securely1g/sonic-buildimage", source_revision="",
              registry_revision="", number=177):
    expression = " ".join(expression.splitlines())
    values = {
        "github.event.pull_request.head.repo.full_name": repr(head_repo),
        "github.event.pull_request.number": repr(number),
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


CONFIG_URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/f9003910e389ce3dff5b2b543c907af635025ad3/ci/source_mirrors.cfg"
CONFIG_SHA = "84b777906cc81c5c4491bca69ec2e2bf8dedcda43fbf8c0c32388593ab05ef0b"
CONFIG = b"# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.\n# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0\n# Bazel matches the complete URL without its https:// prefix.\nrewrite zlib\\.net/pigz/pigz-2\\.8\\.tar\\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz\n\n# Exact Bookworm security header payload; the repository SHA256 remains\n# ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f.\nrewrite snapshot\\.debian\\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\\.1\\.177-1_amd64\\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248\n\n# Prefer the official pool for pinned Debian binary packages. Bazel keeps the\n# repository rule's checksum; the original snapshot remains a fallback.\n# Indexes and source archives must retain their snapshot URLs.\nrewrite (snapshot\\.debian\\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://deb.debian.org/debian/pool/$2\n\n# Exact historical AMD64 payloads selected by declared build tools and source\n# images. Reuse their verified content IDs before the dated Snapshot fallback.\n# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\\.280_amd64\\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01\n# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/p/python3\\.13/libpython3\\.13_3\\.13\\.5\\-2\\+deb13u4_amd64\\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d\n# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/p/python3\\.13/libpython3\\.13\\-minimal_3\\.13\\.5\\-2\\+deb13u4_amd64\\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b\n# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\\.8\\.1\\-1_amd64\\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354\n\nrewrite (snapshot\\.debian\\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://$1\nrewrite (snapshot\\.debian\\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://deb.debian.org/debian-security/pool/$2\nrewrite (snapshot\\.debian\\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://$1\n"


def workflow_step(name):
    workflow = (ROOT / ".github/workflows/bazel-macsec.yml").read_text()
    marker = "      - name: " + name + "\n"
    if workflow.count(marker) != 1:
        raise AssertionError("missing or duplicate workflow step: " + name)
    return workflow.split(marker, 1)[1].split("\n      - ", 1)[0]


def run_transport_steps(work, *, response=CONFIG, network_failure=False):
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
    home = work / "home"
    home.mkdir()
    (home / ".bazelrc").write_text("common --repository_cache=/kept/repository\nbuild --disk_cache=/kept/disk\n")
    (work / ".bazelrc.user").write_text("common --distdir=/kept/custom\n")
    temporary = work / "runner-temp"
    temporary.mkdir()
    scripts = []
    for name in ("Prepare the verified compression source", "Configure verified Debian payload transport"):
        scripts.append(textwrap.dedent(workflow_step(name).split("        run: |\n", 1)[1]))
    scripts.append("bazel aquery recorded-target")
    return subprocess.run(["bash", "-e", "-c", "\n".join(scripts)], cwd=work,
        env={"PATH": str(mock) + ":/usr/bin:/bin", "PYTHONPATH": str(mock),
             "PYTHONDONTWRITEBYTECODE": "1", "RUNNER_TEMP": str(temporary), "HOME": str(home)},
        capture_output=True, text=True)


class MacsecWorkflowTest(unittest.TestCase):
    def test_transport_covers_default_and_paired_runs(self):
        step = workflow_step("Configure verified Debian payload transport")
        self.assertNotIn("        if:", step)
        workflow = (ROOT / ".github/workflows/bazel-macsec.yml").read_text()
        self.assertIn("SONIC_BAZEL_REGISTRY_REVISION: ${{ inputs.registry_revision || '2c9a1bfed7c4e5db33bb7279ea10d055a17eff29' }}", workflow)
        self.assertEqual(workflow.count("ref: ${{ inputs.source_revision || github.sha }}"), 2)

    def test_verified_transport_appends_after_pigz_and_preserves_distdir(self):
        self.assertEqual(hashlib.sha256(CONFIG).hexdigest(), CONFIG_SHA)
        workflow = (ROOT / ".github/workflows/bazel-macsec.yml").read_text()
        self.assertLess(workflow.index("Prepare the verified compression source"),
                        workflow.index("Configure verified Debian payload transport"))
        self.assertLess(workflow.index("Configure verified Debian payload transport"),
                        workflow.index("Build runtime and debug images from source owners"))
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            result = run_transport_steps(work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((work / "runner-temp/macsec-source-mirrors.cfg").read_bytes(), CONFIG)
            self.assertEqual((work / ".bazelrc.user").read_text().splitlines(), [
                "common --distdir=/kept/custom", "", "common --repository_cache=/kept/repository", "build --disk_cache=/kept/disk", "common --distdir=" + str(work / "runner-temp/pigz-distdir"),
                "common --experimental_downloader_config=" + str(work / "runner-temp/macsec-source-mirrors.cfg")])
            self.assertEqual(json.loads((work / "network.json").read_text())["url"], CONFIG_URL)
            self.assertEqual((work / "bazel-command.txt").read_text(), "aquery recorded-target\n")

    def test_corrupt_or_failed_transport_stops_before_bazel_and_rc_append(self):
        for response, failure in ((b"corrupt config", False), (CONFIG, True)):
            with self.subTest(network_failure=failure), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                result = run_transport_steps(work, response=response, network_failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((work / "bazel-command.txt").exists())
                self.assertFalse((work / "runner-temp/macsec-source-mirrors.cfg").exists())
                self.assertEqual((work / ".bazelrc.user").read_text(),
                    "common --distdir=/kept/custom\n\ncommon --repository_cache=/kept/repository\nbuild --disk_cache=/kept/disk\ncommon --distdir=" + str(work / "runner-temp/pigz-distdir") + "\n")

    def test_review_wrapper_keeps_transport_for_default_and_paired_catalogs(self):
        import shutil
        for catalog in ('2c9a1bfed7c4e5db33bb7279ea10d055a17eff29', '6a69c2d89070d4d2ffc80b1e13626da387f7ea65'):
            with self.subTest(catalog=catalog), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                result = run_transport_steps(work)
                self.assertEqual(result.returncode, 0, result.stderr)
                for relative in ('tools/bazel/common.bazelrc', 'tools/bazel/ci/review_bazel.sh', 'tools/bazel/ci/review_bazel_rc.py'):
                    target = work / relative; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, target)
                recorder = work / 'record.py'
                recorder.write_text("#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\nargs=sys.argv[1:];rc=next(x.split('=',1)[1] for x in args if x.startswith('--bazelrc='));Path('actual-wrapper.json').write_text(json.dumps({'argv':args,'rc':Path(rc).read_text()}))\n")
                recorder.chmod(0o755)
                for command in ('aquery', 'build', 'cquery'):
                    result = subprocess.run(['bash', 'tools/bazel/ci/review_bazel.sh', command, '//recorded:target'], cwd=work,
                        env={'PATH':'/usr/bin:/bin','HOME':str(work/'home'),'TMPDIR':str(work/'runner-temp'),
                             'BAZEL_REAL':str(recorder),'SONIC_BAZEL_REGISTRY_REVISION':catalog}, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recorded = json.loads((work/'actual-wrapper.json').read_text())
                    self.assertIn('--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/'+catalog, recorded['argv'])
                    for line in ('common --repository_cache=/kept/repository', 'build --disk_cache=/kept/disk',
                                 'common --distdir=/kept/custom', 'common --distdir='+str(work/'runner-temp/pigz-distdir'),
                                 'common --experimental_downloader_config='+str(work/'runner-temp/macsec-source-mirrors.cfg')):
                        self.assertIn(line, recorded['rc'].splitlines())

    def test_manual_selection_accepts_only_paired_immutable_shas(self):
        workflow = (ROOT / ".github/workflows/bazel-macsec.yml").read_text()
        marker = "      - name: Validate the optional reviewed MACsec source selection\n"
        self.assertEqual(workflow.count(marker), 2)
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
        workflow = (ROOT / ".github/workflows/bazel-macsec.yml").read_text()
        self.assertEqual(workflow.count("ref: ${{ inputs.source_revision || github.sha }}"), 2)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertIn("SONIC_BAZEL_REGISTRY_REVISION: ${{ inputs.registry_revision || '2c9a1bfed7c4e5db33bb7279ea10d055a17eff29' }}", workflow)
        self.assertEqual(workflow.count("CI_HEAD_SHA: ${{ inputs.source_revision || github.event.pull_request.head.sha || github.sha }}"), 4)
        self.assertIn("run: python3 -B tools/bazel/tests/macsec_workflow_test.py", workflow)
        for phase in ("build", "incremental"):
            self.assertIn("macsec_image.py " + phase + " --artifacts artifacts/macsec", workflow)

    def test_only_the_owned_control_pr_skips_existing_deb_fixtures_and_vs(self):
        workflow = (ROOT / ".github/workflows/bazel-oci.yml").read_text()
        guards = re.findall(r"SOURCE_ONLY_NO_DEBS: \$\{\{ (.+?) \}\}", workflow)
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(condition(guard))
            self.assertTrue(condition(guard, branch="codex/macsec-oci-amd64"))
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


    def test_loaded_binding_rejects_artifact_source_catalog_control_and_driver_tampering(self):
        import os
        from unittest.mock import patch
        source, catalog, control = "a" * 40, "b" * 40, "c" * 40
        def python_body(name):
            script = textwrap.dedent(workflow_step(name).split("        run: |\n", 1)[1])
            return script.split("\n", 1)[1].rsplit("PYBIND", 1)[0]
        writer = python_body("Bind the completed MACsec artifacts to the reviewed source")
        reader = python_body("Verify the selected source against the downloaded MACsec evidence")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); artifacts = root / "artifacts/macsec"; artifacts.mkdir(parents=True)
            driver = root / "tools/bazel/ci/macsec_loaded.py"; driver.parent.mkdir(parents=True); driver.write_text("retained driver")
            for name, status in [("build-receipt.json", "built"), ("validation.json", "passed"), ("public-receipt.json", "built")]:
                (artifacts / name).write_text(json.dumps({"revision": source, "status": status, "invocation": {"review_registry_revision": catalog}}))
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {"SOURCE_REVISION": source, "REGISTRY_REVISION": catalog, "GITHUB_SHA": control}), patch("subprocess.check_output", return_value=source + "\n"), patch("subprocess.run"):
                    exec(writer, {})
                    saved = (artifacts / "reviewed-selection.json").read_text()
                    build = artifacts / 'build-receipt.json'
                    build_saved = build.read_text()
                    for key in ('revision', 'status', 'invocation'):
                        bad = json.loads(build_saved); bad[key] = {'review_registry_revision': 'wrong'} if key == 'invocation' else 'wrong'
                        build.write_text(json.dumps(bad))
                        with self.subTest(build_field=key), self.assertRaises(AssertionError): exec(writer, {})
                    build.write_text(build_saved)
                    exec(reader, {})
                    for key in ("source_revision", "registry_revision", "control_revision", "loaded_driver_sha256"):
                        value = json.loads(saved); value[key] = "wrong"; (artifacts / "reviewed-selection.json").write_text(json.dumps(value))
                        with self.subTest(key=key), self.assertRaises(AssertionError): exec(reader, {})
                    (artifacts / "reviewed-selection.json").write_text(saved)
                    for name in ("validation.json", "public-receipt.json"):
                        file = artifacts / name; content = file.read_text(); file.write_text(json.dumps({"revision": "wrong"}))
                        with self.subTest(receipt=name), self.assertRaises(AssertionError): exec(reader, {})
                        file.write_text(content)
                    driver.write_text("changed driver")
                    with self.assertRaises(AssertionError): exec(reader, {})
            finally:
                os.chdir(previous)

class MacsecGraphDiagnosticsTest(unittest.TestCase):
    def test_rejected_graph_retains_exact_diagnostics_without_accepting_failure(self):
        import importlib.util
        helper = ROOT / "tools/bazel/ci/resolution.py"
        spec = importlib.util.spec_from_file_location("macsec_resolution_fixture", helper)
        resolution = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolution)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts/macsec"
            artifacts.mkdir(parents=True)
            lock = b'{"lockFileVersion":24,"registryFileHashes":{}}\n'
            (root / "MODULE.bazel.lock").write_bytes(lock)
            fake = root / "recorded-bazel"
            fake.write_text("#!/usr/bin/python3\n" + textwrap.dedent('''
                import json, sys
                from pathlib import Path
                Path("command.json").write_text(json.dumps(sys.argv[1:]))
                print(json.dumps({"key": "<root>", "root": True,
                                  "dependencies": [{"key": "fixture@1.0"}]}))
                print("ERROR: fixture extension failed", file=sys.stderr)
                sys.exit(7)
            '''))
            fake.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "Unexpected module graph inspection failure"):
                resolution.collect(root, artifacts, bazel=[str(fake)])
            self.assertEqual((root / "MODULE.bazel.lock").read_bytes(), lock)
            command = json.loads((root / "command.json").read_text())
            self.assertEqual(command, ["mod", "graph", "--output=json", "--extension_info=hidden",
                                       "--lockfile_mode=off", "--color=no", "--curses=no"])
            self.assertEqual(json.loads((artifacts / "module-graph.json").read_text())["dependencies"],
                             [{"key": "fixture@1.0"}])
            self.assertEqual((artifacts / "module-graph.stderr").read_text(),
                             "ERROR: fixture extension failed\n")
            self.assertEqual(json.loads((artifacts / "module-graph-inspection.json").read_text()),
                             {"argv": [str(fake), *command], "returncode": 7})
            upload = workflow_step("Retain failed module graph diagnostics")
            paths = re.search(r"          path: \|\n((?:            .+\n)+)", upload)[1]
            paths = [line.strip() for line in paths.splitlines()]
            self.assertEqual(paths, ["artifacts/macsec/module-graph.json",
                                    "artifacts/macsec/module-graph.stderr",
                                    "artifacts/macsec/module-graph-inspection.json"])
            self.assertTrue(all((root / path).is_file() for path in paths))
            self.assertFalse((artifacts / "MODULE.bazel.lock").exists())

    def test_diagnostic_upload_only_on_build_failure_and_separate_from_image_artifact(self):
        build = workflow_step("Build runtime and debug images from source owners")
        self.assertIn("        id: macsec_build\n", build)
        self.assertNotIn("continue-on-error", build)
        upload = workflow_step("Retain failed module graph diagnostics")
        expression = re.search(r"^        if: (.+)$", upload, re.MULTILINE)[1]
        for outcome in ("success", "failure", "cancelled", "skipped"):
            with self.subTest(outcome=outcome):
                condition = expression.replace("always()", "True").replace(
                    "steps.macsec_build.outcome", repr(outcome)).replace("&&", " and ")
                self.assertEqual(eval(condition, {"__builtins__": {}}, {}), outcome == "failure")
        self.assertIn("name: macsec-module-graph-failure", upload)
        self.assertIn("if-no-files-found: warn", upload)
        self.assertNotIn("continue-on-error", upload)
        images = workflow_step("Retain images and reviewed build evidence")
        self.assertIn("always() && steps.public.outcome == 'success'", images)
        self.assertNotIn("module-graph.stderr", images)
        self.assertNotIn("module-graph-inspection.json", images)


if __name__ == "__main__":
    unittest.main()
