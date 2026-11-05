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


# Exact Eventd/OCI archive transport input; other jobs retain their existing pins.
SOURCE_IMAGE_CONFIG_FIXTURE = r"""# BCR pigz@2.8.bcr.2 keeps its original source integrity and upstream patches.
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
# SHA256: 53a9b789bb02415d758f652ef5ac913d89fed29782610e9fe0fd2488e4f2e465
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/g/glibc/libc6-dev_2\.41\-12\+deb13u3_amd64\.deb https://snapshot.debian.org/file/fbb9e46e11e1a8365a4f067eb9d6ec10251df100

# Exact AMD64 Graphviz dependency selected by libteam's native analysis.
# SHA256: cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/u/util-linux/libmount1_2\.41-5_amd64\.deb https://snapshot.debian.org/file/a82622f9dbedc5d438d8791f4e3a59f6f9976d88

# Exact AMD64 base-image input selected by FRR's Sysmgr image analysis.
# SHA256: c6464bdc20086222fa9295481367e9a52f3fb78120a14089bae5f0d36f1b8a74
rewrite snapshot\.debian\.org/archive/debian/20260727T143429Z/pool/main/j/jq/jq_1\.7\.1-6\+deb13u2_amd64\.deb https://snapshot.debian.org/file/5556211154d1adb33a69780c9b9385f7cf79fda9

rewrite (snapshot\.debian\.org/archive/debian/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://$1
rewrite (snapshot\.debian\.org/archive/debian-security/[0-9]{8}T[0-9]{6}Z/pool/([^?#]+\.deb)) https://deb.debian.org/debian-security/pool/$2
# Exact Trixie headers selected by FRR's Eventd AMD64 analysis.
# SHA256: c514b81492fc01c573ecfc809f9b460023d50e5006848c4caf78997bf1f776a5
rewrite snapshot\.debian\.org/archive/debian-security/20260726T121236Z/pool/updates/main/l/linux/linux-libc-dev_6\.12\.96-1_all\.deb https://snapshot.debian.org/file/66b33fe93d5d572e06cdabdd49d01c7b4947cfc7
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
            steps = [(job_name, step["run"]) for job_name, job in workflow["jobs"].items()
                     for step in job.get("steps", [])
                     if step.get("name") == "Configure the verified source mirror"]
            self.assertEqual(len(steps), expected)
            found.extend((name, job_name, script) for job_name, script in steps)
        return found

    def mirror_bytes(self, workflow=None, job=None):
        # Keep each workflow on its separately verified immutable transport input.
        if workflow == "bazel-eventd.yml" or (workflow == "bazel-oci.yml" and job == "archive"):
            data = SOURCE_IMAGE_CONFIG_FIXTURE.encode()
            expected = "d71c0e8152d6f717de11615b7d20d9ff47f1b0004fd908902d163654f2a09a00"
        else:
            data = SHARED_CONFIG_FIXTURE.encode()
            expected = "9fbb19c33cc9564d439a86dc36e0833244ccb57cfc1511ac35a2923f908dfbc9"
        self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        return data

    def test_all_four_workflow_overrides_preserve_pigz_and_pool_transport(self):
        for name, job, script in self.mirror_steps():
            data = self.mirror_bytes(name, job)
            selected = name == "bazel-eventd.yml" or (name == "bazel-oci.yml" and job == "archive")
            revision = ("1609822832b591fcd31e8266a8a0dee5922e5685" if selected
                        else "b7b92bcfa5857685ee7cd7953fdbf55264989932")
            url = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/" + revision + "/ci/source_mirrors.cfg"
            with self.subTest(workflow=name, job=job), tempfile.TemporaryDirectory() as directory:
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
        for name, job, script in self.mirror_steps():
            with self.subTest(workflow=name, job=job), tempfile.TemporaryDirectory() as directory:
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


class YangSourceMirrorTest(unittest.TestCase):
    def workflow_steps(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/bazel-yang-models.yml").read_text())
        steps = workflow["jobs"]["package"]["steps"]
        mirror = next(step for step in steps if step.get("name") == "Configure the verified source mirror")
        build = next(step for step in steps if step.get("name") == "Build and verify the installed source distribution")
        self.assertLess(steps.index(mirror), steps.index(build))
        return mirror, build

    def mirror_bytes(self):
        # Exact shared configuration with the observed YANG sysroot fallback.
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
                exec(compile(code, "yang-source-mirror", "exec"), {})
            fetch.assert_called_once_with(
                "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/"
                "6e7b45d19c76d479bc055d86ffd6fa2962948576/ci/source_mirrors.cfg", timeout=60)

    def test_actual_initial_query_loads_verified_mirror_without_changing_cache_options(self):
        data = self.mirror_bytes()
        _, build = self.workflow_steps()
        # Stop at the first query. Every Bazel call is an argv recorder.
        query = build["run"].split("git rev-parse HEAD", 1)[0]
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                before = "common --repository_cache=/retained/repository\ncommon --disk_cache=/retained/disk\ncommon --distdir=/retained/downloads\n"
                (root / ".bazelrc.user").write_text(before)
                for name in ("tools/bazel/common.bazelrc", CONFIG, "tools/bazel/ci/frr_draft_bazel.sh"):
                    destination = root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((ROOT / name).read_bytes())
                (root / "tools/bazel/ci/frr_draft_bazel.sh").chmod(0o755)
                self.configure(root, data)
                config = root / "sonic-source-mirrors.cfg"
                self.assertEqual(config.read_bytes(), data)
                self.assertEqual((root / ".bazelrc.user").read_text(), before +
                                 "common --experimental_downloader_config=" + str(config) + "\n")
                self.assertIn("try-import %workspace%/.bazelrc.user",
                              (root / "tools/bazel/common.bazelrc").read_text())
                executables = {
                    "bazel": "#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n",
                    "git": "#!/bin/sh\nexit 0\n",
                    "uname": "#!/bin/sh\nprintf '%s\\n' " + machine + "\n",
                }
                for name, content in executables.items():
                    executable = root / name
                    executable.write_text(content)
                    executable.chmod(0o755)
                working = root / build["working-directory"]
                working.mkdir(parents=True)
                subprocess.run(["bash", "-eu", "-c", query.replace("${{ matrix.machine }}", machine)],
                               cwd=working, env={"PATH": str(root) + ":/usr/bin:/bin"},
                               check=True, capture_output=True, text=True)
                argv = json.loads((working / "artifacts/image-root-targets.txt").read_text())
                self.assertEqual(argv, [
                    "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
                    "--bazelrc=" + str(root / "tools/bazel/common.bazelrc"), "cquery",
                    "--registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/cfd42fb5eecf0e1a8e45da58598b107754c81ec3",
                    "--registry=https://bcr.bazel.build", "--lockfile_mode=update", "--lockfile_mode=update",
                    "//src/sonic-yang-models:yang_models_package_test",
                ])

    def test_corrupt_yang_mirror_cannot_replace_config_or_cache_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = "common --disk_cache=/retained/cache\n"
            (root / ".bazelrc.user").write_text(before)
            config = root / "sonic-source-mirrors.cfg"
            config.write_bytes(b"previous verified config")
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                self.configure(root, b"corrupt download")
            self.assertEqual(config.read_bytes(), b"previous verified config")
            self.assertEqual((root / ".bazelrc.user").read_text(), before)


if __name__ == "__main__":
    unittest.main()
