#!/usr/bin/env python3
"""Exercise selected-owner CI transport and preserve direct runtime providers."""
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import yaml

ROOT = Path(__file__).resolve().parents[3]
CATALOG = "2a849bbcaa254113a61ca0aeb972aeeccb8494f5"
CONFIG_SHA = "e9f004bfce0501f998c7d624d028d40c72eabddc12c1f7b34fc474f14042d6a8"
CONFIG_URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/6e305126f13965fd01b6b3aa06e98d4595832d39/ci/source_mirrors.cfg"
OCI_CONFIG_SHA = "bb3a1a988c5cfcc2586827687124f0cff230fe2de73b1d2789331b23dec169e1"
OCI_CONFIG_URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/264002ba05e75df2f7c90dea2d78cf2bf46a4d4a/ci/source_mirrors.cfg"
JOBS = [("bazel-base-images.yml", "native"), ("bazel-eventd.yml", "native"),
        ("bazel-supervisor-rust.yml", "native"), ("bazel-yang-models.yml", "package"),
        ("bazel-oci.yml", "archive"), ("bazel-oci.yml", "checks")]


def calls(path, function):
    return [{k.arg: ast.literal_eval(k.value) for k in node.keywords}
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == function]


class SwssOwnerWorkflowTest(unittest.TestCase):
    def test_swss_evidence_does_not_run_before_the_build_starts(self):
        steps = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())["jobs"]["archive"]["steps"]
        build = next(s for s in steps if s.get("id") == "swss_layer_build")
        evidence = next(s for s in steps if s.get("id") == "swss_layer_public")
        self.assertIn("mkdir -p artifacts/swss-layer", build["run"])
        self.assertEqual(evidence["if"],
                         "always() && matrix.architecture == 'amd64' && steps.swss_layer_build.outcome != 'skipped'")
        self.assertLess(steps.index(build), steps.index(evidence))
        # Execute the existing evidence code for a started, failed build: it may
        # publish an empty partial summary, but must not invent accepted outputs.
        code = evidence["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            root = Path("artifacts/swss-layer")
            root.mkdir(parents=True)
            exec(compile(code, "swss-evidence", "exec"), {})
            self.assertEqual(json.loads((root / "build-summary.json").read_text()), {})
            self.assertFalse((root / "public-receipt.json").exists())

    def test_bootstrap_verifies_config_before_appending_to_existing_caches(self):
        for filename, job in JOBS:
            fixture, config_url, config_sha = (
                ("source-mirrors-bc9a3a0f.cfg",
                 "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/bc9a3a0f684d3f0d42d20b47ea5387d448acf9c4/ci/source_mirrors.cfg",
                 "bf85cf7e4f277197732321a8f72086c296698fc495002a68e16fc7e7ef239014")
                if (filename, job) == ("bazel-oci.yml", "archive") else
                ("source-mirrors-a32c87f0.cfg",
                 "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/a32c87f0658e20f5fecd6459ef006caed96d905f/ci/source_mirrors.cfg",
                 "354c7d6ea5931c8ecb1c4a96fafa1269802b1547bde3b3b64dfc55a89a35cb46")
                if (filename, job) == ("bazel-base-images.yml", "native") else
                ("source-mirrors-264002ba.cfg", OCI_CONFIG_URL, OCI_CONFIG_SHA)
                if filename == "bazel-oci.yml" else
                ("source-mirrors-6e305126.cfg", CONFIG_URL, CONFIG_SHA))
            data = (ROOT / "tools/bazel/tests/fixtures" / fixture).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), config_sha)
            steps = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())["jobs"][job]["steps"]
            mirror = next(s for s in steps if s.get("name") == "Configure the verified source mirror")
            self.assertLess(next(i for i,s in enumerate(steps) if "preserve_setup_bazel_cache.py" in s.get("run", "")), steps.index(mirror))
            code = mirror["run"].split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
            for corrupt in (False, True):
                with self.subTest(filename=filename, job=job, corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    before = "common --repository_cache=/retained/repository\nbuild --disk_cache=/retained/disk\n"
                    (root / ".bazelrc.user").write_text(before)
                    with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
                        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data + (b"changed" if corrupt else b""))) as fetch:
                            if corrupt:
                                with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                                    exec(compile(code, filename, "exec"), {})
                            else:
                                exec(compile(code, filename, "exec"), {})
                        fetch.assert_called_once_with(config_url, timeout=60)
                    selected = root / "sonic-source-mirrors.cfg"
                    self.assertEqual(selected.exists(), not corrupt)
                    self.assertEqual((root / ".bazelrc.user").read_text(), before + (
                        "" if corrupt else "common --experimental_downloader_config=" + str(selected) + "\n"))
                    if not corrupt:
                        self.assertEqual(selected.read_bytes(), data)

    def test_real_wrapper_selects_complete_catalog_and_respects_private_cache_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = root / "bazel"
            recorder.write_text("#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            recorder.chmod(0o755)
            wrapper = ROOT / "tools/bazel/ci/libteam_draft_bazel.sh"
            result = subprocess.run(["bash", str(wrapper), "test", "--disk_cache=/private/disk",
                                     "@sonic_swss//crates/countersyncd:common_rust_test"], cwd=root,
                                    env=dict(os.environ, PATH=str(root)+":/usr/bin:/bin"), capture_output=True, text=True, check=True)
            args = json.loads(result.stdout)
            self.assertIn("--nohome_rc", args)
            self.assertIn("--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"+CATALOG, args)
            self.assertEqual(args[-2:], ["--disk_cache=/private/disk", "@sonic_swss//crates/countersyncd:common_rust_test"])

    def test_manual_legacy_and_current_source_interfaces_keep_cache_tests_and_probe(self):
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        steps = document["jobs"]["archive"]["steps"]
        selected = [next(s for s in steps if s.get("name") == name) for name in (
            "Preserve setup-bazel caches for source checks", "Validate reviewed SWSS consumer contracts",
            "Check cached no-op and source edit through the image")]
        for source, mode, success in (("legacy", "swss-layer-hosted", True),
                                      ("current", "swss-layer-hosted", True),
                                      ("current", "none", True), ("legacy", "none", False),
                                      ("missing", "swss-layer-hosted", False)):
            with self.subTest(source=source, mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                helper = root / "tools/bazel/ci"
                helper.mkdir(parents=True)
                if source != "missing":
                    names = (["lldp_draft_cache.py", "swss_rebuild.py"] if source == "legacy" else
                             ["preserve_setup_bazel_cache.py", "swss_image_rebuild.py"])
                    for name in names:
                        (helper / name).write_text("# source interface fixture\n")
                recorder = """#!/usr/bin/python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='python3':
    script=next((x for x in args if x.endswith('.py')), '')
    if script and not script.endswith('bazel_commands.py') and not Path(script).is_file():
        raise SystemExit(2)
with Path(os.environ['RECORD']).open('a') as out: out.write(json.dumps({'name':name,'argv':args})+'\\n')
"""
                for command in ("python3", "bazel", "git"):
                    (root / command).write_text(recorder)
                    (root / command).chmod(0o755)
                env = dict(os.environ, PATH=str(root)+":/usr/bin:/bin", SOURCE_IMAGE=mode,
                           BAZEL=str(root/"bazel"), RECORD=str(root/"calls.jsonl"), BASH_ENV="/dev/null")
                result = subprocess.run(["bash", "-eu", "-c", "\n".join(s["run"] for s in selected)],
                                        cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if not success:
                    self.assertFalse((root/"calls.jsonl").exists())
                    continue
                recorded = [json.loads(line) for line in (root/"calls.jsonl").read_text().splitlines()]
                paths = [arg for item in recorded for arg in item["argv"] if arg.endswith('.py')]
                self.assertIn("tools/bazel/ci/"+names[0], paths)
                self.assertIn("tools/bazel/ci/"+names[1], paths)
                test = next(item["argv"] for item in recorded if item["name"] == "bazel")
                expected = (["//src/libteam:consumer_test", "//tools/bazel/tests:swss_common_rust_test"]
                            if source == "legacy" else
                            ["@libteam//:consumer_test", "@sonic_swss//crates/countersyncd:common_rust_test"])
                self.assertEqual(test[-4:], expected + ["@sonic_swss//bazel:protobuf_runtime_test",
                                                      "//tools/bazel/tests:swss_package_contract_test"])
                self.assertIn("--nocache_test_results", test)
                self.assertIn("--host_platform=@sonic_build_infra//platforms:x86_64_trixie", test)
                probe = next(item["argv"] for item in recorded if "tools/bazel/ci/"+names[1] in item["argv"])
                self.assertEqual(probe[probe.index("--runtime-target")+1],
                                 "//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie.gz")
                self.assertEqual(probe[probe.index("--unchanged-target")+1],
                                 "//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz")

    def test_fresh_checkout_verifier_receives_verified_config_explicitly(self):
        document = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        step = next(s for s in document["jobs"]["archive"]["steps"]
                    if s.get("name") == "Check local cache reuse across fresh checkouts")
        script = step["run"].replace("${{ matrix.platform }}", "x86_64_trixie")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, text in (
                    ("bazel", "#!/bin/sh\nprintf '/retained/repository\\n'\n"),
                    ("python3", "#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")):
                (root / name).write_text(text)
                (root / name).chmod(0o755)
            result = subprocess.run(["bash", "-eu", "-c", script], cwd=root,
                env=dict(os.environ, PATH=str(root)+":/usr/bin:/bin", BAZEL=str(root/"bazel"),
                         RUNNER_TEMP=str(root)), capture_output=True, text=True, check=True)
            argv = json.loads(result.stdout)
            self.assertIn("--bazel-arg=--experimental_downloader_config=" + str(root/"sonic-source-mirrors.cfg"), argv)
            self.assertEqual(argv[argv.index("--repository-cache")+1], "/retained/repository")
            self.assertIn("--bazel-arg=--platforms=@sonic_build_infra//platforms:x86_64_trixie", argv)

    def test_image_keeps_direct_debug_providers_and_library_only_metadata(self):
        document = ROOT / "dockers/docker-swss-layer-trixie/BUILD.bazel"
        # Parse only the literal rules whose provider edges are the contract.
        tree = ast.parse(document.read_text())
        selected = {}
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fields = {k.arg: k.value for k in node.value.keywords}
                if "name" in fields:
                    selected[ast.literal_eval(fields["name"])] = fields
        runtime = ast.literal_eval(selected["source_runtime"]["tars"])
        self.assertIn("@libteam//:libteam_runtime", runtime)
        self.assertIn("@libteam//:libteamdctl_runtime", runtime)
        self.assertNotIn("@sonic_swss//dist:libteam_runtime", runtime)
        self.assertEqual(ast.literal_eval(selected["installed_packages"]["owner_metadata"]),
                         ["@libteam//:library_runtime_packages.json"])
        self.assertEqual(ast.literal_eval(selected["installed_packages"]["source_modules"]),
                         {"libteam": "@image_source_modules//:libteam.MODULE.bazel"})

    def test_ci_executes_owner_test_rules_and_runtime_adapter_matches_common_interface(self):
        oci = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        step = next(s for s in oci["jobs"]["archive"]["steps"] if "consumer-events.jsonl" in s.get("run", ""))
        self.assertIn("@libteam//:consumer_test", step["run"])
        self.assertIn("@sonic_swss//crates/countersyncd:common_rust_test", step["run"])
        # Evaluate the real Starlark macro with its rule/select interfaces stubbed,
        # then check the selected Common helper's positional argument contract.
        namespace = {"load": lambda *args: None, "select": lambda choices: choices["//conditions:default"]}
        observed = []
        namespace["sh_test"] = lambda **kwargs: observed.append(kwargs)
        exec(compile((ROOT / "tools/bazel/tests/runtime.bzl").read_text(), "runtime.bzl", "exec"), namespace)
        namespace["common_runtime_test"]("listener_tests", ":listener_tests_binary", test_args=["--test-threads=1"])
        test = observed[0]
        self.assertEqual(test["args"], ["$(rootpath @patchelf//:patchelf)",
            "$(rootpath @sonic_swss_common//tools/bazel/runtime:test_runtime)", "-", "disabled",
            "$(rootpath :listener_tests_binary)", "--test-threads=1"])
        self.assertIn("@patchelf//:patchelf", test["data"])
        dependencies = calls(ROOT / "MODULE.bazel", "bazel_dep")
        self.assertEqual([d["version"] for d in dependencies if d["name"] == "patchelf"], ["0.18.0.bcr.1"])


if __name__ == "__main__":
    unittest.main()
