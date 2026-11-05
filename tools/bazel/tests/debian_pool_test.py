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


SHARED_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
# Original SHA256: eb872b4f0e1f0ebe59c9f7bd8c506c4204893ba6a8492de31df416f0d5170fd0
# Bazel matches the complete URL without its https:// prefix.
rewrite zlib\.net/pigz/pigz-2\.8\.tar\.gz https://distfiles.macports.org/pigz/pigz-2.8.tar.gz

# Exact Bookworm security header payload; the repository SHA256 remains
# ea265b1dff21ab2e4b22b38097031f8d63fb940ecb89427659655598860efc0f.
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.1\.177-1_amd64\.deb https://snapshot.debian.org/file/aed472fca7f9b98fdb7103b974db264cb60c2248

# Prefer the official pool for pinned Debian binary packages. Bazel keeps the
# repository rule's checksum; the original snapshot remains a fallback.
# Indexes and source archives must retain their snapshot URLs.
rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian/pool/$2

# Exact historical AMD64 payloads selected by declared build tools and source
# images. Reuse their verified content IDs before the dated Snapshot fallback.
# SHA256: 25acadc8b394bc86d05df16e70975e523f8f839a7d8d6d5e22c1cafdcef741aa
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/c/cdebconf/libdebconfclient0_0\.280_amd64\.deb https://snapshot.debian.org/file/219dec5cd95ea5be91161ab1d30dc3f19efb6e01
# SHA256: 2381bdaa2462b186db27671507e53eba9515c5341575fb72189b6228c73e3df8
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/a817f92905b030fcbca7d433db7dc4dcccf91c3d
# SHA256: e3a45504aaf395acb5c57efb5ccfdf85efa6eaffc165653d1f03532bbf5b92c5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/p/python3\.13/libpython3\.13\-minimal_3\.13\.5\-2\+deb13u4_amd64\.deb https://snapshot.debian.org/file/d63097e4bb174c04bfc29e38e6270b43c2db9c0b
# SHA256: 68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libs/libselinux/libselinux1_3\.8\.1\-1_amd64\.deb https://snapshot.debian.org/file/44b247687c034c8a4142204f0827f7286614b354

# SHA256: 06460065e7768d7a4a72cb02a0eddd386936756a33596bb23901faeb72dc8f68
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/libh/libheif/libheif1_1\.19\.8\-1_amd64\.deb https://snapshot.debian.org/file/04d63b7e722e47ed3e0cd74aa01066c6d382621d
# SHA256: 4104cf1149947db1a3450b2a13aab23000a695aa6981f515216129ad8b884423
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/a/audit/libaudit-dev_4\.0\.2\-2\+b2_amd64\.deb https://snapshot.debian.org/file/930f8fe502ed2d1b79243a03d1e1318c1bf30524
# SHA256: fbc76c5c549192f52e7a03949108cdf28b1dc0751261495b7ef17cb8bf39dd8d
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/uuid-dev_2\.41\-5_amd64\.deb https://snapshot.debian.org/file/05b9652f442e9b33da16bb41cc69283e2a9b7c33

rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian-security/pool/$2
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
"""

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
        debian_mirrors = (ROOT / "tools/bazel/debian_mirrors.cfg").read_bytes()
        url = ("https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
               "18e7c652222449b906a17c2fd948e37bcad3324e/ci/source_mirrors.cfg")
        for name, script in self.mirror_steps():
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                mirrors = root / "tools/bazel/debian_mirrors.cfg"
                mirrors.parent.mkdir(parents=True)
                mirrors.write_bytes(debian_mirrors)
                before = "common --repository_cache=/retained/repository\ncommon --disk_cache=/retained/disk\ncommon --distdir=/retained/downloads\n"
                code = script.split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
                with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
                    Path(".bazelrc.user").write_text(before)
                    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as fetch:
                        exec(compile(code, name, "exec"), {})
                    fetch.assert_called_once_with(url, timeout=60)
                    destination = root / "sonic-source-mirrors.cfg"
                    self.assertEqual(destination.read_bytes(), debian_mirrors + b"\n" + data)
                    self.assertEqual(Path(".bazelrc.user").read_text(), before +
                                     "common --downloader_config=" + str(destination) + "\n")

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

    def test_libteam_fetch_verifies_bytes_before_admitting_builds(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-libteam.yml").read_text())
        steps = workflow["jobs"]["native"]["steps"]
        selected = next(step for step in steps if step.get("name") ==
                        "Fetch the verified shared downloader configuration")
        self.assertLess(steps.index(selected), next(index for index, step in enumerate(steps)
                        if step.get("name") == "Build source outputs and execute native tests"))
        self.assertRegex(selected["env"]["SOURCE_MIRRORS_URL"],
                         r"^https://raw\.githubusercontent\.com/securely1g/sonic-bazel-registry/[0-9a-f]{40}/ci/source_mirrors\.cfg$")
        self.assertRegex(selected["env"]["SOURCE_MIRRORS_SHA256"], r"^[0-9a-f]{64}$")
        payload = b"verified downloader test input\n"
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory(prefix="libteam transport ") as directory:
                root = Path(directory)
                recorder = root / "curl"
                recorder.write_text("#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\n"
                                    "Path(sys.argv[sys.argv.index('--output')+1]).write_bytes("
                                    "Path(os.environ['TEST_DOWNLOAD']).read_bytes())\n")
                recorder.chmod(0o755)
                download = root / "download"
                download.write_bytes(payload + (b"changed" if corrupt else b""))
                env = dict(os.environ, PATH=str(root) + ":/usr/bin:/bin", RUNNER_TEMP=str(root),
                           TEST_DOWNLOAD=str(download), SOURCE_MIRRORS_URL=selected["env"]["SOURCE_MIRRORS_URL"],
                           SOURCE_MIRRORS_SHA256=hashlib.sha256(payload).hexdigest())
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c",
                                         selected["run"] + "\nprintf ready > build-ready\n"],
                                        cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, not corrupt, result.stderr)
                self.assertEqual((root / "build-ready").exists(), not corrupt)
                if not corrupt:
                    self.assertEqual((root / "artifacts/source-mirrors.cfg").read_bytes(), payload)
                    self.assertEqual((root / "artifacts/source-mirrors-url.txt").read_text().strip(),
                                     selected["env"]["SOURCE_MIRRORS_URL"])

    def test_libteam_explicit_override_reaches_queries_and_rebuild_wrapper(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-libteam.yml").read_text())
        selected = next(step for step in workflow["jobs"]["native"]["steps"]
                        if step.get("name") == "Build source outputs and execute native tests")
        script = selected["run"]
        options = re.search(r"(?ms)^options=\(\n.*?^\)", script).group(0)
        function = re.search(r"(?ms)^run_bazel\(\) \{\n.*?^\}", script).group(0)
        sdk_query = re.search(r"(?m)^  output\(\) \{.*$", script).group(0)
        rebuild = re.search(r"(?ms)^  python3 -B src/libteam/verify_rebuild\.py .*?--output artifacts/rebuild", script).group(0)
        with tempfile.TemporaryDirectory(prefix="libteam forwarding ") as directory:
            root = Path(directory)
            commands = root / "bin"
            commands.mkdir()
            for command in ("bazel", "python3"):
                recorder = commands / command
                recorder.write_text("#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
                recorder.chmod(0o755)
            env = dict(os.environ, PATH=str(commands) + ":/usr/bin:/bin", RUNNER_TEMP=str(root),
                       CI_CPU="x86_64", CI_DISTRIBUTION="trixie",
                       SONIC_BAZEL_REGISTRY=selected["env"]["SONIC_BAZEL_REGISTRY"])
            expected = "--experimental_downloader_config=" + str(root / "libteam-source-mirrors.cfg")
            for invocation in ("run_bazel build //src/libteam:libteam_runtime",
                               sdk_query + "\noutput libteam_dev"):
                result = subprocess.run(["bash", "-eu", "-c", options + "\n" + function + "\n" + invocation],
                                        cwd=root, env=env, check=True, capture_output=True, text=True)
                self.assertIn(expected, json.loads(result.stdout))
            result = subprocess.run(["bash", "-eu", "-c", rebuild], cwd=root, env=env,
                                    check=True, capture_output=True, text=True)
            arguments = json.loads(result.stdout)
            forwarded = [argument.removeprefix("--bazel-arg=") for argument in arguments
                         if argument.startswith("--bazel-arg=")]
            self.assertIn(expected, forwarded)
            # Exercise the existing wrapper from another workspace directory;
            # the verified absolute config path must survive that handoff.
            fresh = root / "fresh workspace"
            fresh.mkdir()
            result = subprocess.run(["bash", str(ROOT / "tools/bazel/ci/libteam_draft_bazel.sh"),
                                     "cquery", *forwarded, "//src/libteam:libteam_dev"],
                                    cwd=fresh, env=env, check=True, capture_output=True, text=True)
            self.assertIn(expected, json.loads(result.stdout))

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


class RustSourceMirrorTest(unittest.TestCase):
    def workflow_steps(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-supervisor-rust.yml").read_text())
        steps = workflow["jobs"]["native"]["steps"]
        mirror = next(step for step in steps if step.get("name") == "Configure the verified source mirror")
        build = next(step for step in steps if step.get("name") == "Audit and test source runtime and debug packages")
        self.assertLess(steps.index(mirror), steps.index(build))
        self.assertFalse(any(re.search(r'(?m)^\s*(?:bazel|"\$BAZEL")\s', step.get("run", ""))
                             for step in steps[:steps.index(mirror)]))
        return mirror, build

    def preserve_caches(self, root, home_settings):
        steps = yaml.safe_load((ROOT / ".github/workflows/bazel-supervisor-rust.yml").read_text())["jobs"]["native"]["steps"]
        cache = next(step for step in steps if step.get("name") == "Preserve setup-bazel caches for source checks")
        mirror, _ = self.workflow_steps()
        self.assertLess(steps.index(cache), steps.index(mirror))
        home = root / "home"
        home.mkdir()
        (home / ".bazelrc").write_text(home_settings)
        relative = "tools/bazel/ci/preserve_setup_bazel_cache.py"
        helper = root / relative
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_bytes((ROOT / relative).read_bytes())
        subprocess.run(["bash", "-euc", cache["run"]], cwd=root,
                       env=dict(os.environ, HOME=str(home), BASH_ENV="/dev/null"), check=True)

    def mirror_bytes(self):
        # Exact shared configuration with the observed Rust sysroot fallback.
        marker = r"rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1"
        addition = (
            "# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465\n"
            r"rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100"
            "\n\n"
        )
        data = SHARED_CONFIG_FIXTURE.replace("\n\n" + marker, "\n" + addition + marker, 1).encode()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "b6b7dbd03e20e482718de3f312a015168056f6c422d94723f55175a728259a7b")
        return data

    def configure(self, root, data):
        mirror, _ = self.workflow_steps()
        code = mirror["run"].split("python3 - <<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
        with contextlib.chdir(root), mock.patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as fetch:
                exec(compile(code, "rust-source-mirror", "exec"), {})
            fetch.assert_called_once_with(
                "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
                "6e7b45d19c76d479bc055d86ffd6fa2962948576/ci/source_mirrors.cfg", timeout=60)

    def test_verified_config_precedes_actual_audit_test_and_query_calls(self):
        data = self.mirror_bytes()
        _, build = self.workflow_steps()
        # Exercise the actual three invocations up to evidence copying with an
        # argv recorder in place of Bazel. No compiler or package action runs.
        script = build["run"].split("cp MODULE.bazel.lock", 1)[0]
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory(prefix="rust mirror ") as directory:
                root = Path(directory)
                retained = "common --distdir=/retained/downloads\n"
                (root / ".bazelrc.user").write_text(retained)
                cache_settings = "build --disk_cache=/retained/disk\ncommon --repository_cache=/retained/repository\n"
                self.preserve_caches(root, cache_settings + "build --jobs=99\ncommon --experimental_downloader_config=/ignored/home.cfg\n")
                before = retained + cache_settings
                self.assertEqual((root / ".bazelrc.user").read_text(), before)
                for name in ("tools/bazel/common.bazelrc", CONFIG, "tools/bazel/ci/libteam_draft_bazel.sh",
                             "tools/bazel/ci/bazel_commands.py"):
                    destination = root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((ROOT / name).read_bytes())
                wrapper = root / "tools/bazel/ci/libteam_draft_bazel.sh"
                wrapper.chmod(0o755)
                self.configure(root, data)
                config = root / "sonic-source-mirrors.cfg"
                self.assertEqual(config.read_bytes(), data)
                self.assertEqual((root / ".bazelrc.user").read_text(), before +
                                 "common --experimental_downloader_config=" + str(config) + "\n")
                common = (root / "tools/bazel/common.bazelrc").read_text()
                self.assertLess(common.index("common --experimental_downloader_config="),
                                common.index("try-import %workspace%/.bazelrc.user"))
                recorder = root / "bazel"
                recorder.write_text("#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n"
                    "args=sys.argv[1:]\n"
                    "rc=[a.split('=',1)[1] for a in args if a.startswith('--bazelrc=')]\n"
                    "assert len(rc)==1 and Path(rc[0]).is_file()\n"
                    "assert 'try-import %workspace%/.bazelrc.user' in Path(rc[0]).read_text()\n"
                    "user=Path('.bazelrc.user').read_text()\n"
                    "assert 'build --disk_cache=/retained/disk' in user\n"
                    "assert 'common --repository_cache=/retained/repository' in user\n"
                    "assert 'common --distdir=/retained/downloads' in user\n"
                    "assert '--jobs=99' not in user and '/ignored/home.cfg' not in user\n"
                    "selected=[x.split('=',1)[1] for x in user.splitlines() if x.startswith('common --experimental_downloader_config=')]\n"
                    "assert len(selected)==1 and Path(selected[0]).is_file()\n"
                    "with Path(os.environ['RECORD']).open('a') as out: out.write(json.dumps({'argv':args,'config':selected[0]})+'\\n')\n"
                    "if 'aquery' in args: print(json.dumps({'actions':[],'artifacts':[]}))\n")
                recorder.chmod(0o755)
                record = root / "calls.jsonl"
                env = dict(os.environ, PATH=str(root) + ":/usr/bin:/bin", RECORD=str(record),
                           MACHINE=machine, BAZEL=str(wrapper))
                subprocess.run(["bash", "-eu", "-c", script], cwd=root, env=env,
                               check=True, capture_output=True, text=True)
                calls = [json.loads(line) for line in record.read_text().splitlines()]
                self.assertEqual(len(calls), 3)
                targets = ["//src/sonic-supervisord-utilities-rs:" + name for name in
                           ("unit_tests", "integration_tests", "listener_tests", "package_test")]
                for call, command in zip(calls, ("aquery", "test", "cquery")):
                    argv = call["argv"]
                    self.assertIn(command, argv)
                    self.assertIn("--bazelrc=" + str(root / "tools/bazel/common.bazelrc"), argv)
                    self.assertIn("--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/cfd42fb5eecf0e1a8e45da58598b107754c81ec3", argv)
                    self.assertIn("--platforms=@sonic_build_infra//platforms:" + machine + "_trixie", argv)
                    self.assertIn("--host_platform=@sonic_build_infra//platforms:" + machine + "_trixie", argv)
                    self.assertEqual(call["config"], str(config))
                    if command == "aquery": self.assertIn("deps(set(" + " ".join(targets) + "))", argv)
                    if command == "test": self.assertEqual(argv[-4:], targets)
                self.assertEqual((root / ".bazelrc.user").read_text(), before +
                                 "common --experimental_downloader_config=" + str(config) + "\n")

    def test_corrupt_rust_mirror_stops_before_query_and_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = "common --disk_cache=/retained/cache\n"
            (root / ".bazelrc.user").write_text(before)
            config = root / "sonic-source-mirrors.cfg"
            config.write_bytes(b"previous verified config")
            ready = root / "query-ready"
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                self.configure(root, b"corrupt download")
                ready.write_text("query may start")
            self.assertFalse(ready.exists())
            self.assertEqual(config.read_bytes(), b"previous verified config")
            self.assertEqual((root / ".bazelrc.user").read_text(), before)


if __name__ == "__main__":
    unittest.main()
