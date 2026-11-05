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


CONFIG_URL = 'https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/cecadbe61827bf570c53c0d2fc53dcd63f839908/ci/source_mirrors.cfg'
CONFIG_SHA = '8d533cc7c6189f4a3917b80bd8ced680c359c1df2bdf458793f97e94f341c354'
CONFIG = b"# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.\n# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0\n# Bazel matches the complete URL without its https:// prefix.\nrewrite zlib\\.net/pigz/pigz-2\\.8\\.tar\\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz\n\n# Exact Bookworm security header payload; the repository SHA256 remains\n# ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f.\nrewrite snapshot\\.debian\\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\\.1\\.177-1_amd64\\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248\n\n# Prefer the official pool for pinned Debian binary packages. Bazel keeps the\n# repository rule's checksum; the original snapshot remains a fallback.\n# Indexes and source archives must retain their snapshot URLs.\nrewrite (snapshot\\.debian\\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://deb.debian.org/debian/pool/$2\n\n# Exact historical AMD64 payloads selected by declared build tools and source\n# images. Reuse their verified content IDs before the dated Snapshot fallback.\n# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\\.280_amd64\\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01\n# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/p/python3\\.13/libpython3\\.13_3\\.13\\.5\\-2\\+deb13u4_amd64\\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d\n# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/p/python3\\.13/libpython3\\.13\\-minimal_3\\.13\\.5\\-2\\+deb13u4_amd64\\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b\n# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\\.8\\.1\\-1_amd64\\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354\n\n# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\\.19\\.8\\-1_amd64\\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d\n# SHA256: 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\\.0\\.2\\-2\\+b2_amd64\\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524\n# SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\\.41\\-5_amd64\\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33\n# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\\.41\\-12\\+deb13u3_amd64\\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100\n\n# Exact AMD64 Graphviz dependency selected by libteam's native analysis.\n# SHA256: cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\\.41-5_amd64\\.deb https://snapshot.debian.org/file/a82622f9dbedc5d438d8791f4e3a59f6f9976d88\n\n# Exact AMD64 base-image input selected by FRR's Sysmgr image analysis.\n# SHA256: c6464bdc20086222fa9295481367e9a52f3fb78120a14089bae5f0d36f1b8a74\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/j/jq/jq_1\\.7\\.1-6\\+deb13u2_amd64\\.deb https://snapshot.debian.org/file/5556211154d1adb33a69780c9b9385f7cf79fda9\n\n# Exact AMD64 build tool selected by MACsec image analysis.\n# SHA256: c98b1130f08ef6ac3932972c56be84cfc308ce1afa37105cb06aaf47ced2d078\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/g/gzip/gzip_1\\.13-1_amd64\\.deb https://snapshot.debian.org/file/17e2275e726dad554ab38aa3f3d240f5270923cb\n\n# Exact libc6 inputs fetched by MACsec's AMD64 SWSS/OCI/libteam analysis.\n# The multiarch sysroot reads both package records before native execution.\n# SHA256 (amd64): 8ffd13165b9ee3f067e2ee670df718e48c1bdaa18676ac93d1de761dbbb3913c\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\\.41-12\\+deb13u3_amd64\\.deb https://snapshot.debian.org/file/11449623c6859d9164dff8aedbcf023edaddc513\n# SHA256 (arm64): ff529924782d3286181188fc265a6a92e7fe28975fb3a925dc0e05c0ca66e52f\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6_2\\.41-12\\+deb13u3_arm64\\.deb https://snapshot.debian.org/file/8dd0cfbe0f5bd2c3923c8252c00c868bc6ac945e\n\n# Exact AMD64 ss-dev dependency selected by MACsec's native libteam check.\n# SHA256: 879e8477dc20006e59788831e4f3a436d15afe3c055ae0e93beb96696f0e5eb1\nrewrite snapshot\\.debian\\.org/archive/debian/20260727T143429Z/pool/main/e/e2fsprogs/libss2_1\\.47\\.2-3\\+b11_amd64\\.deb https://snapshot.debian.org/file/458f436ed67e7f80aed736c6665c6b725301024c\n\nrewrite (snapshot\\.debian\\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://$1\nrewrite (snapshot\\.debian\\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://deb.debian.org/debian-security/pool/$2\n# Exact Trixie headers selected by FRR's Eventd AMD64 analysis.\n# SHA256: c514b81492fc01c573ecfc809f9b460023d50e5006848c4caf78997bf1f776a5\nrewrite snapshot\\.debian\\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\\.12\\.96-1_all\\.deb https://snapshot.debian.org/file/66b33fe93d5d572e06cdabdd49d01c7b4947cfc7\nrewrite (snapshot\\.debian\\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\\.deb)) https://$1\n"


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


def inherited_step(filename, name):
    workflow = (ROOT / ".github/workflows" / filename).read_text()
    marker = "      - name: " + name + "\n"
    if workflow.count(marker) != 1:
        raise AssertionError("missing or duplicate inherited step: " + name)
    return workflow.split(marker, 1)[1].split("\n      - ", 1)[0]


def inherited_transport(work, filename, response=CONFIG):
    """Execute the inherited job's bootstrap with recorded network and pigz."""
    import shutil
    mock = work / "mock"; mock.mkdir()
    (mock / "sitecustomize.py").write_text(
        "import io, urllib.request\n"
        "def urlopen(url, timeout):\n"
        "    assert url == " + repr(CONFIG_URL) + " and timeout == 60\n"
        "    return io.BytesIO(" + repr(response) + ")\n"
        "urllib.request.urlopen = urlopen\n")
    home = work / "home"; home.mkdir()
    (home / ".bazelrc").write_text(
        "common --repository_cache=/kept/repository\nbuild --disk_cache=/kept/disk\n")
    (work / ".bazelrc.user").write_text("common --distdir=/kept/custom\n")
    temporary = work / "runner-temp"; temporary.mkdir()
    pigz = work / "tools/bazel/gzip/prepare_pigz.py"
    pigz.parent.mkdir(parents=True); pigz.write_text("print('recorded pigz preparation')\n")
    for relative in ("tools/bazel/common.bazelrc", "tools/bazel/ci/review_bazel.sh",
                     "tools/bazel/ci/review_bazel_rc.py", "tools/bazel/ci/libteam_draft_bazel.sh"):
        target = work / relative; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    recorder = mock / "bazel-record"
    recorder.write_text(
        "#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n"
        "args=sys.argv[1:]; rc=next(x.split('=',1)[1] for x in args if x.startswith('--bazelrc='))\n"
        "with Path('commands.jsonl').open('a') as f: f.write(json.dumps({'argv':args,'rc':Path(rc).read_text()})+'\\n')\n")
    recorder.chmod(0o755)
    environment = {"PATH": str(mock) + ":/usr/bin:/bin", "HOME": str(home),
                   "PYTHONPATH": str(mock), "PYTHONDONTWRITEBYTECODE": "1",
                   "RUNNER_TEMP": str(temporary), "TMPDIR": str(temporary),
                   "BAZEL_REAL": str(recorder)}
    scripts = []
    if filename != "bazel-libteam.yml":
        scripts.append(textwrap.dedent(inherited_step(filename,
            "Prepare the checksum-pinned pigz source archive").split("        run: |\n", 1)[1]))
    scripts.append("cp .bazelrc.user before-transport.rc")
    scripts.append(textwrap.dedent(inherited_step(filename,
        "Configure verified Debian payload transport").split("        run: |\n", 1)[1]))
    scripts.append("for command in aquery build cquery; do bash tools/bazel/ci/libteam_draft_bazel.sh \"$command\" //recorded:target; done")
    result = subprocess.run(["bash", "-e", "-c", "\n".join(scripts)],
        cwd=work, env=environment, capture_output=True, text=True)
    return result, environment


class InheritedTransportTest(unittest.TestCase):
    workflows = {
        "bazel-swss-layer.yml": "Build and inspect the shared runtime and symbols",
        "bazel-libteam.yml": "Build source outputs and execute native tests",
        "bazel-oci.yml": "Check Make OCI bases and reproducible Docker archives",
    }

    def test_bootstrap_precedes_first_analysis_and_actual_wrapper_keeps_inputs(self):
        for filename, first in self.workflows.items():
            with self.subTest(workflow=filename), tempfile.TemporaryDirectory() as directory:
                workflow = (ROOT / ".github/workflows" / filename).read_text()
                step = inherited_step(filename, "Configure verified Debian payload transport")
                self.assertNotIn("        if:", step)
                self.assertLess(workflow.index("Configure verified Debian payload transport"), workflow.index(first))
                if filename != "bazel-libteam.yml":
                    self.assertLess(workflow.index("Prepare the checksum-pinned pigz source archive"),
                                    workflow.index("Configure verified Debian payload transport"))
                work = Path(directory)
                result, _ = inherited_transport(work, filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = work / "runner-temp/sonic-source-mirrors.cfg"
                self.assertEqual(config.read_bytes(), CONFIG)
                records = [json.loads(line) for line in (work / "commands.jsonl").read_text().splitlines()]
                self.assertEqual(len(records), 3)
                for command, record in zip(("aquery", "build", "cquery"), records):
                    self.assertIn(command, record["argv"])
                    self.assertIn("--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/2c9a1bfed7c4e5db33bb7279ea10d055a17eff29", record["argv"])
                    for line in ("common --repository_cache=/kept/repository", "build --disk_cache=/kept/disk",
                                 "common --distdir=/kept/custom", "common --experimental_downloader_config=" + str(config)):
                        self.assertIn(line, record["rc"].splitlines())
                    if filename != "bazel-libteam.yml":
                        self.assertIn("common --distdir=" + str(work / "artifacts/source-downloads"), record["rc"].splitlines())
                    policies = [line for line in record["rc"].splitlines() if "--experimental_downloader_config=" in line]
                    self.assertEqual(policies[-1], "common --experimental_downloader_config=" + str(config))

    def test_corrupt_download_rejects_before_config_rc_or_bazel_changes(self):
        for filename in self.workflows:
            with self.subTest(workflow=filename), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                result, _ = inherited_transport(work, filename, b"corrupt configuration")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("configuration checksum mismatch", result.stderr)
                self.assertEqual((work / ".bazelrc.user").read_bytes(), (work / "before-transport.rc").read_bytes())
                self.assertFalse((work / "runner-temp/sonic-source-mirrors.cfg").exists())
                self.assertFalse((work / "commands.jsonl").exists())

    def test_fresh_checkout_receives_absolute_config_through_existing_bazel_args(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            result, environment = inherited_transport(work, "bazel-oci.yml")
            self.assertEqual(result.returncode, 0, result.stderr)
            recorder = work / "mock/python3"
            recorder.write_text("#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\nPath('fresh-argv.json').write_text(json.dumps(sys.argv[1:]))\n")
            recorder.chmod(0o755)
            info = work / "mock/bazel-info"
            info.write_text("#!/bin/sh\n[ \"$*\" = 'info repository_cache' ] || exit 2\nprintf '%s\\n' /kept/repository\n")
            info.chmod(0o755)
            environment["BAZEL"] = str(info)
            script = textwrap.dedent(inherited_step("bazel-oci.yml",
                "Check local cache reuse across fresh checkouts").split("        run: |\n", 1)[1])
            script = script.replace("${{ matrix.platform }}", "x86_64_trixie")
            result = subprocess.run(["bash", "-e", "-c", script], cwd=work,
                env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads((work / "fresh-argv.json").read_text())
            config = work / "runner-temp/sonic-source-mirrors.cfg"
            forwarded = [arg.removeprefix("--bazel-arg=") for arg in argv if arg.startswith("--bazel-arg=")]
            self.assertIn("--experimental_downloader_config=" + str(config), forwarded)
            self.assertIn("--platforms=@sonic_build_infra//platforms:x86_64_trixie", forwarded)
            self.assertEqual(argv[argv.index("--repository-cache") + 1], "/kept/repository")
            spec = importlib.util.spec_from_file_location("cache_probe", ROOT / "tools/bazel/ci/verify_agent_cache.py")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            source = work / "source"; source.mkdir()
            (source / "tracked.txt").write_text("retained source")
            (source / ".bazelrc.user").write_text("untracked local policy")
            for command in (["git", "init", "-q"], ["git", "add", "tracked.txt"]):
                subprocess.run(command, cwd=source, check=True, capture_output=True)
            checkout = work / "fresh"
            module.snapshot(source, checkout)
            self.assertFalse((checkout / ".bazelrc.user").exists())
            artifacts = work / "cache-evidence"; artifacts.mkdir()
            args = SimpleNamespace(artifacts=artifacts, docker_image=None, repository_cache=Path("/kept/repository"),
                                   bazel="recorded-only", bazel_startup_arg=[], bazel_arg=forwarded)
            with patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaisesRegex(RuntimeError, "cold build failed"):
                    module.run_build(args, "cold", checkout, work / "new-disk-cache", work / "new-output")
            actual = run.call_args.args[0]
            self.assertIn("--experimental_downloader_config=" + str(config), actual)
            self.assertIn("--repository_cache=/kept/repository", actual)
            self.assertIn("--disk_cache=" + str(work / "new-disk-cache/disk_cache"), actual)
            self.assertEqual(run.call_args.kwargs["cwd"], checkout)


if __name__ == "__main__":
    unittest.main()
