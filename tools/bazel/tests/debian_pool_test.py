#!/usr/bin/env python3
"""Check locked package transport and its inherited CI override reach."""
from pathlib import Path
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock
import re
import shlex
import unittest
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG = "tools/bazel/debian-pool.cfg"


def rewritten_urls(url):
    # Bazel 8.5.1 UrlRewriter.applyRewriteRules matches the complete URL without
    # its scheme, collecting every matching replacement in configuration order.
    # These data-only rules use the common Java/Python regex subset.
    candidate = url.split("://", 1)[1]
    result = []
    for line in (ROOT / CONFIG).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        directive, expression, replacement = line.split()
        if directive != "rewrite":
            raise AssertionError("unexpected downloader directive")
        match = re.fullmatch(expression, candidate)
        if match:
            result.append(re.sub(r"\$(\d+)", lambda group: match[int(group[1])], replacement))
    return list(dict.fromkeys(result)) or [url]


class DebianPoolTest(unittest.TestCase):
    def test_real_locked_packages_keep_the_original_as_second_choice(self):
        examples = [
            ("debian", "20260727T143429Z", "main/a/arping/arping_2.25-1_amd64.deb"),
            ("debian", "20260727T143429Z", "main/n/nlohmann-json3/nlohmann-json3-dev_3.11.3-2.1_all.deb"),
            ("debian", "20260727T143429Z", "main/libn/libnsl/libnsl-dev_1.3.0-3+b3_amd64.deb"),
            ("debian", "20260727T143429Z", "main/libf/libfido2/libfido2-dev_1.15.0-1+b1_armhf.deb"),
            ("debian", "20260727T143429Z", "main/r/readline/libreadline-dev_8.2-6_amd64.deb"),
            ("debian", "20260727T143429Z", "main/p/python-typing-extensions/python3-typing-extensions_4.13.2-1_all.deb"),
            ("debian-security", "20260726T121236Z", "updates/main/libs/libsodium/libsodium-dev_1.0.18-1+deb13u1_arm64.deb"),
        ]
        for archive, timestamp, path in examples:
            original = f"https://snapshot.debian.org/archive/{archive}/{timestamp}/pool/{path}"
            with self.subTest(package=path):
                self.assertEqual(rewritten_urls(original), [f"https://deb.debian.org/{archive}/pool/{path}", original])

    def test_indexes_source_archives_and_unrelated_urls_are_unchanged(self):
        prefix = "https://snapshot.debian.org/archive/debian/20260727T143429Z/"
        urls = [
            prefix + "dists/trixie/main/binary-amd64/Packages.xz",
            prefix + "dists/trixie/Release", prefix + "dists/trixie/InRelease",
            prefix + "pool/main/a/arping/arping_2.25.orig.tar.gz",
            prefix + "pool/main/a/arping/arping_2.25-1.dsc",
            prefix + "pool/main/a/arping/arping_2.25-1_amd64.deb?download=1",
            prefix + "pool/main/a/arping/arping_2.25-1_amd64.deb#fragment",
            prefix.replace("20260727T143429Z", "latest") + "pool/a.deb",
            prefix.replace("20260727T143429Z", "20260727") + "pool/a.deb",
            prefix.replace("snapshot.debian.org", "snapshot.debian.org.example") + "pool/a.deb",
            prefix.replace("snapshot.debian.org", "other.example") + "pool/a.deb",
            "https://deb.debian.org/debian/pool/main/a/arping/arping_2.25-1_amd64.deb",
            "https://snapshot.debian.org/archive/debian-security/20260726T121236Z/dists/trixie-security/main/binary-arm64/Packages.xz",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(rewritten_urls(url), [url])

    def test_configuration_only_changes_payload_transport(self):
        rules = [line.split() for line in (ROOT / CONFIG).read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(rules), 4)
        for directive, expression, replacement in rules:
            self.assertEqual(directive, "rewrite")
            self.assertTrue(expression.endswith(r"\.deb))"))
            self.assertTrue(replacement.startswith("https://"))
            self.assertIn(urlsplit(replacement).netloc, ("deb.debian.org", "$1"))

    def test_shared_default_keeps_local_override_and_cache_options(self):
        root_rc = (ROOT / ".bazelrc").read_text().splitlines()
        self.assertIn("import %workspace%/tools/bazel/common.bazelrc", root_rc)
        lines = (ROOT / "tools/bazel/common.bazelrc").read_text().splitlines()
        option = "common --experimental_downloader_config=" + CONFIG
        self.assertEqual(lines.count(option), 1)
        self.assertLess(lines.index(option), lines.index("try-import %workspace%/.bazelrc.user"))
        self.assertFalse(Path(CONFIG).is_absolute())
        self.assertTrue((ROOT / CONFIG).is_file())
        # No cache location is replaced by the shared default.
        for line in lines:
            self.assertFalse(any(token.startswith(("--disk_cache", "--repository_cache", "--distdir"))
                                 for token in shlex.split(line, comments=True)))

    def mirror_steps(self):
        found = []
        for name, expected in (("bazel-base-images.yml", 1), ("bazel-oci.yml", 2),
                               ("bazel-eventd.yml", 1)):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])
                     if step.get("name") == "Configure the verified source mirror"]
            self.assertEqual(len(steps), expected)
            found.extend((name, step["run"]) for step in steps)
        return found

    def mirror_bytes(self):
        # Exact existing pigz configuration followed by the checked-in pool rules.
        # The production scripts must accept only the immutable combined checksum.
        prefix = (
            "# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.\n"
            "# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0\n"
            "# Bazel matches the complete URL without its https:// prefix.\n"
            "rewrite zlib\\.net/pigz/pigz-2\\.8\\.tar\\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz\n"
        ).encode()
        data = prefix + b"\n" + (ROOT / CONFIG).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "fa60a63fd62f11e3c097905a3ff2a5dc4b25bd7ba42ebc9877fe7ea4dd1d3ee2")
        return data

    def test_all_four_workflow_overrides_preserve_pigz_and_pool_transport(self):
        data = self.mirror_bytes()
        url = ("https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
               "18e7c652222449b906a17c2fd948e37bcad3324e/ci/source_mirrors.cfg")
        for name, script in self.mirror_steps():
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                before = "common --repository_cache=/retained/repository\ncommon --disk_cache=/retained/disk\ncommon --distdir=/retained/downloads\n"
                code = script.split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
                with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
                    Path(".bazelrc.user").write_text(before)
                    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as fetch:
                        exec(compile(code, name, "exec"), {})
                    fetch.assert_called_once_with(url, timeout=60)
                    destination = root / "sonic-source-mirrors.cfg"
                    self.assertEqual(destination.read_bytes(), data)
                    self.assertEqual(Path(".bazelrc.user").read_text(), before +
                                     "common --experimental_downloader_config=" + str(destination) + "\n")

    def test_corrupt_transport_leaves_existing_config_and_cache_options_untouched(self):
        for name, script in self.mirror_steps():
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                code = script.split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
                with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
                    Path(".bazelrc.user").write_text("common --disk_cache=/retained/cache\n")
                    destination = root / "sonic-source-mirrors.cfg"
                    destination.write_bytes(b"original verified configuration")
                    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"corrupt download")):
                        with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                            exec(compile(code, name, "exec"), {})
                    self.assertEqual(destination.read_bytes(), b"original verified configuration")
                    self.assertEqual(Path(".bazelrc.user").read_text(), "common --disk_cache=/retained/cache\n")

    def test_native_and_draft_callers_load_the_shared_rc_explicitly(self):
        # Execute the existing shell interfaces against an argv recorder, never Bazel.
        wrappers = list((ROOT / "tools/bazel/ci").glob("*_draft_bazel.sh"))
        self.assertEqual(len(wrappers), 1)
        owner = wrappers[0].name.removesuffix("_draft_bazel.sh")
        workflow = yaml.safe_load((ROOT / ".github/workflows" / ("bazel-" + owner + ".yml")).read_text())
        scripts = [step["run"] for job in workflow["jobs"].values() for step in job.get("steps", [])
                   if "run_bazel() {" in step.get("run", "")]
        self.assertEqual(len(scripts), 1)
        function = re.search(r"(?ms)^run_bazel\(\) \{\n.*?^\}", scripts[0]).group(0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = root / "bazel"
            recorder.write_text("#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            recorder.chmod(0o755)
            rc = root / "tools/bazel/common.bazelrc"
            rc.parent.mkdir(parents=True)
            rc.write_text((ROOT / "tools/bazel/common.bazelrc").read_text())
            (root / CONFIG).write_bytes((ROOT / CONFIG).read_bytes())
            env = {"PATH": str(root) + ":/usr/bin:/bin"}
            commands = [["/bin/bash", str(wrappers[0]), "cquery", "//:transport_probe"],
                        ["/bin/bash", "-eu", "-c", "options=(--jobs=2)\n" + function +
                         "\nrun_bazel cquery //:transport_probe\n"]]
            for command in commands:
                result = subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, text=True)
                argv = json.loads(result.stdout)
                self.assertIn("--nohome_rc", argv)
                self.assertIn("--noworkspace_rc", argv)
                self.assertIn("--bazelrc=" + str(rc), argv)
                self.assertIn("cquery", argv)
                self.assertFalse(any(arg.startswith("--experimental_downloader_config=") for arg in argv))
                self.assertLess(rc.read_text().index("--experimental_downloader_config="),
                                rc.read_text().index("try-import %workspace%/.bazelrc.user"))
                self.assertTrue((root / CONFIG).is_file())

    def test_fresh_snapshot_keeps_the_shared_config_and_kernel_stays_independent(self):
        with mock.patch.object(sys, "path", [str(ROOT), *sys.path]):
            from tools.bazel.ci.verify_agent_cache import snapshot
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            names = (".bazelrc", "tools/bazel/common.bazelrc", CONFIG)
            for name in names:
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / name).read_bytes())
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            copied = Path(directory) / "snapshot"
            snapshot(source, copied)
            for name in names:
                self.assertEqual((source / name).read_bytes(), (copied / name).read_bytes())
        # The retained kernel consumer copies its independent template directory;
        # it does not inherit this root's common rc or its relative config path.
        kernel_rc = (ROOT / "tools/bazel/kernel/.bazelrc").read_text()
        self.assertNotIn("common.bazelrc", kernel_rc)
        self.assertNotIn("debian-pool.cfg", kernel_rc)
        self.assertIn('shutil.copytree(workspace / "tools/bazel/kernel", consumer)',
                      (ROOT / "tools/bazel/kernel/prepare.py").read_text())

    def test_ci_runs_this_regression_with_explicit_yaml_dependency(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-oci.yml").read_text())
        callers = []
        for job in workflow["jobs"].values():
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                script = step.get("run", "")
                if "for test in tools/bazel/tests/*_test.py; do" not in script:
                    continue
                callers.append(step["name"])
                setup = [prior["run"] for prior in steps[:index]
                         if prior.get("name") == "Install execution tools"]
                self.assertEqual(len(setup), 1)
                self.assertIn("python3-yaml", shlex.split(setup[0]))
                # Exercise both real CI selectors with only this regression's
                # pathname present, using an argv recorder instead of Python.
                loop = script[:script.index("\nfi") + len("\nfi")]
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    test = root / "tools/bazel/tests/debian_pool_test.py"
                    test.parent.mkdir(parents=True)
                    test.write_text("")
                    recorder = root / "python3"
                    recorder.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
                    recorder.chmod(0o755)
                    for source_only in ("true", "false"):
                        result = subprocess.run(["bash", "-eu", "-c", loop], cwd=root,
                                                env={"PATH": str(root) + ":/usr/bin:/bin",
                                                     "SOURCE_ONLY_NO_DEBS": source_only},
                                                check=True, capture_output=True, text=True)
                        expected = ("-B tools/bazel/tests/debian_pool_test.py" if source_only == "true"
                                    else "-B -m unittest discover -s tools/bazel/tests -p *_test.py")
                        self.assertEqual(result.stdout.strip(), expected)
        self.assertEqual(len(callers), 2)


if __name__ == "__main__":
    unittest.main()
