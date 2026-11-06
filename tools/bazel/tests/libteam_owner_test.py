"""Check the image consumer's shared libteam boundary without building it."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-libteam.yml"


class LibteamOwnerTest(unittest.TestCase):
    def test_native_workflow_runs_the_owner_test_with_uncached_results(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step = next(step for step in workflow["jobs"]["native"]["steps"]
                    if step.get("name") == "Build source outputs and execute native tests")
        setup = re.search(r'(?ms)^options=\(.*?^\}', step["run"])[0]
        invocation = re.search(r'(?ms)^run_bazel test .*?tee artifacts/test.log', step["run"])[0]
        with tempfile.TemporaryDirectory(prefix="owner native test ") as temporary:
            work = Path(temporary)
            (work / "artifacts").mkdir()
            commands = work / "bin"
            commands.mkdir()
            calls = work / "commands.jsonl"
            (work / "tools/bazel").mkdir(parents=True)
            shutil.copyfile(ROOT / "tools/bazel/common.bazelrc", work / "tools/bazel/common.bazelrc")
            (work / ".bazelrc.user").write_text(
                "common " + shlex.quote("--experimental_downloader_config=" + temporary + "/sonic-source-mirrors.cfg") + "\n")
            home = work / "home"
            home.mkdir()
            recorder = commands / "bazel"
            recorder.write_text('''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
rc = next(value.split('=', 1)[1] for value in sys.argv if value.startswith('--bazelrc='))
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps({'arguments': sys.argv[1:], 'configuration': Path(rc).read_text()}) + "\\n")
''')
            recorder.chmod(0o755)
            environment = dict(os.environ, PATH=str(commands) + ":/usr/bin:/bin",
                               CALLS=str(calls), HOME=str(home), BASH_ENV="/dev/null",
                               BAZEL=str(ROOT / "tools/bazel/ci/libteam_draft_bazel.sh"),
                               CI_CPU="x86_64", CI_DISTRIBUTION="trixie", RUNNER_TEMP=temporary)
            subprocess.run(["bash", "-euo", "pipefail", "-c", setup + "\n" + invocation],
                           cwd=work, env=environment, check=True, capture_output=True, text=True)
            recorded = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(len(recorded), 1)
            command = recorded[0]["arguments"]
            self.assertIn("test", command)
            self.assertIn("@libteam//:consumer_test", command)
            self.assertNotIn("//src/libteam:consumer_test", command)
            self.assertIn("--nocache_test_results", command)
            self.assertIn("--build_event_json_file=" + str(work / "artifacts/test-events.json"), command)
            self.assertIn("--host_platform=@sonic_build_infra//platforms:x86_64_trixie", command)
            self.assertIn("--platforms=@sonic_build_infra//platforms:x86_64_trixie", command)
            self.assertIn(["common", "--experimental_downloader_config=" + temporary + "/sonic-source-mirrors.cfg"],
                          [shlex.split(line, comments=True) for line in recorded[0]["configuration"].splitlines()])
            self.assertIn("--nohome_rc", command)

    def test_compatibility_labels_only_forward_owner_providers(self):
        aliases = {}
        namespace = {"package": lambda **kwargs: None,
                     "alias": lambda name, actual: aliases.setdefault(name, actual)}
        exec((ROOT / "src/libteam/BUILD.bazel").read_text(), namespace)
        self.assertEqual(len(aliases), 19)
        self.assertTrue(all(actual == "@libteam//:" + name for name, actual in aliases.items()))
        required = {"libteam", "libteamdctl", "consumer_test", "runtime_packages.json", "libteam_dev"}
        for package in ("libteam", "libteamdctl", "libteam_utils"):
            required.update((package + "_runtime", package + "_runtime.debug_symbols"))
        self.assertTrue(required <= aliases.keys())
        # The old root-owned producer and its inert edit probe must not coexist.
        for filename in ("deps.MODULE.bazel", "libteam.BUILD.bazel", "config.h.in", "verify_rebuild.py"):
            self.assertFalse((ROOT / "src/libteam" / filename).exists(), filename)

    def test_probe_uses_the_same_wrapper_execution_root_and_preserves_flags(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step = next(step for step in workflow["jobs"]["native"]["steps"]
                    if step.get("name") == "Build source outputs and execute native tests")
        block = re.search(r'(?ms)^  probe_bazel=.*?--output artifacts/rebuild', step["run"])[0]
        with tempfile.TemporaryDirectory(prefix="owner probe caller ") as temporary:
            work = Path(temporary) / "image checkout"
            work.mkdir()
            wrapper = work / "tools/bazel/ci/libteam_draft_bazel.sh"
            wrapper.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / wrapper.relative_to(work), wrapper)
            wrapper.chmod(0o755)
            for name in ("tools/bazel/ci/review_bazel.sh", "tools/bazel/ci/review_bazel_rc.py",
                         "tools/bazel/common.bazelrc"):
                shutil.copy2(ROOT / name, work / name)
            home = work / "home"
            home.mkdir()
            output_base = Path(temporary) / "Bazel output base"
            execution = output_base / "execroot/_main"
            probe = execution / "external/libteam+/bazel/verify_rebuild.py"
            probe.parent.mkdir(parents=True)
            probe.write_text("# selected owner source\n")
            commands = Path(temporary) / "bin"
            commands.mkdir()
            calls = Path(temporary) / "commands.jsonl"
            for command in ("bazel", "python3"):
                recorder = commands / command
                recorder.write_text('''#!/usr/bin/python3
import json, os, pathlib, runpy, sys
if pathlib.Path(sys.argv[0]).name == "python3" and sys.argv[1].endswith("review_bazel_rc.py"):
    script = sys.argv.pop(1)
    sys.argv[0] = script
    runpy.run_path(script, run_name="__main__")
    raise SystemExit(0)
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
if pathlib.Path(sys.argv[0]).name == "bazel":
    if "info" in sys.argv:
        assert sys.argv[-1] == "output_base"
        print(os.environ["OUTPUT_BASE"])
    elif "cquery" in sys.argv:
        print(os.environ["PROBE_RESULT"])
''')
                recorder.chmod(0o755)
            environment = dict(os.environ, PATH=str(commands) + ":/usr/bin:/bin",
                               CALLS=str(calls), OUTPUT_BASE=str(output_base),
                               HOME=str(home), BASH_ENV="/dev/null",
                               CI_CPU="x86_64", CI_DISTRIBUTION="trixie", RUNNER_TEMP=temporary)
            for result in ("external/libteam+/bazel/verify_rebuild.py", str(probe)):
                with self.subTest(path=result):
                    calls.unlink(missing_ok=True)
                    subprocess.run(["bash", "-eu", "-c", block], cwd=work,
                                   env={**environment, "PROBE_RESULT": result}, check=True,
                                   capture_output=True, text=True)
                    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                    self.assertEqual(len(recorded), 3)
                    self.assertIn("info", recorded[0])
                    self.assertEqual(recorded[0][-1], "output_base")
                    self.assertIn("@libteam//:bazel/verify_rebuild.py", recorded[1])
                    self.assertEqual(recorded[2][1:3], ["-B", str(probe)])
                    self.assertEqual(recorded[2][3:5], ["--bazel", str(wrapper)])
                    flags = ["--host_platform=@sonic_build_infra//platforms:x86_64_trixie",
                             "--platforms=@sonic_build_infra//platforms:x86_64_trixie",
                             "--experimental_downloader_config=" + temporary + "/sonic-source-mirrors.cfg"]
                    for flag in flags:
                        self.assertTrue(all(flag in call for call in recorded[:2]))
                        self.assertIn("--bazel-arg=" + flag, recorded[2])
                    self.assertTrue(all("--nohome_rc" in call for call in recorded[:2]))
            # The lookup must fail before cquery or Python when the pinned
            # Bzlmod layout is absent, rather than falling back to another base.
            for invalid in ("", "relative/base", str(Path(temporary) / "missing"),
                            str(output_base) + "\n" + str(output_base)):
                with self.subTest(output_base=invalid):
                    calls.unlink(missing_ok=True)
                    failed = subprocess.run(["bash", "-eu", "-c", block], cwd=work,
                        env={**environment, "OUTPUT_BASE": invalid,
                             "PROBE_RESULT": "external/libteam+/bazel/verify_rebuild.py"},
                        capture_output=True, text=True)
                    self.assertNotEqual(failed.returncode, 0)
                    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                    self.assertEqual(len(recorded), 1)
                    self.assertIn("info", recorded[0])
                    self.assertIn("Bazel", failed.stderr)
            for result in ("", "missing.py", "one.py\ntwo.py"):
                with self.subTest(invalid=result):
                    calls.unlink(missing_ok=True)
                    failed = subprocess.run(["bash", "-eu", "-c", block], cwd=work,
                                            env={**environment, "PROBE_RESULT": result},
                                            capture_output=True, text=True)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertNotIn('"python3"', calls.read_text())


LIBTEAM = ROOT / "src/libteam"


def make_series():
    lines = (LIBTEAM / "patch/series").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


# Exact recipe from registry236, libteam1.31.0.sonic.1 at96f5d8e2.
# Update only together with the declared owner version and parity review.
RECIPE = ROOT / "tools/bazel/tests/fixtures/libteam-owner-source.json"


def bazel_patches():
    return list(json.loads(RECIPE.read_text())["patches"])


class LibteamOwnerPatchParityTest(unittest.TestCase):
    def test_bazel_applies_make_series_in_order(self):
        self.assertEqual(bazel_patches(), make_series())

    def test_every_series_patch_exists(self):
        for name in make_series():
            path = LIBTEAM / "patch" / name
            self.assertTrue(path.is_file(), name)
            data = path.read_bytes()
            # The shared owner adds purpose/provenance headers. The applied
            # patch body must remain exactly Make's existing patch bytes.
            body = data[data.index(b"diff --git "):]
            expected = json.loads(RECIPE.with_name("libteam-owner-patch-bodies.json").read_text())
            self.assertEqual(expected[name], hashlib.sha256(body).hexdigest(), name)

    def test_root_selects_the_shared_owner_without_local_override(self):
        tree = ast.parse((ROOT / "MODULE.bazel").read_text())
        selected = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keywords = {item.arg: item.value for item in node.keywords}
            name = getattr(node.func, "id", "")
            if name == "bazel_dep" and ast.literal_eval(keywords["name"]) == "libteam":
                selected.append(ast.literal_eval(keywords["version"]))
            if name.endswith("_override") and "module_name" in keywords:
                self.assertNotEqual(ast.literal_eval(keywords["module_name"]), "libteam")
        self.assertEqual(selected, ["1.31.0.sonic.1"])


if __name__ == "__main__":
    unittest.main()
