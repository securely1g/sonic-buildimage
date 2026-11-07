#!/usr/bin/env python3
"""Exercise YANG's worker root-query settings and legacy cache helper compatibility."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/bazel-yang-models.yml"


class YangCacheWorkflowTest(unittest.TestCase):
    def copy_helper(self, work):
        relative = "tools/bazel/ci/preserve_setup_bazel_cache.py"
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)

    def steps(self):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["package"]["steps"]
        self.assertFalse(any(s.get("uses", "").startswith("bazel-contrib/setup-bazel@") for s in steps))
        build = next(s for s in steps if s.get("name") == "Build and verify the installed source distribution")
        return build["run"]

    def test_verified_transport_reaches_both_roots_before_the_query(self):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["package"]["steps"]
        transport = next(s for s in steps if s.get("name") == "Configure the verified source mirror")
        build = next(s for s in steps if s.get("name") == "Build and verify the installed source distribution")
        self.assertLess(steps.index(transport), steps.index(build))
        self.assertNotIn("working-directory", transport)
        script = transport["run"].split("<<'PYCFG'\n", 1)[1].rsplit("\nPYCFG", 1)[0]
        data = b"rewrite upstream.invalid/(.*) mirror.invalid/$1\n"
        prior = "common --repository_cache=/retained/repository\n"
        for valid in (True, False):
            with self.subTest(valid_checksum=valid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "tmp").mkdir()
                paths = [root / ".bazelrc.user", root / "src/sonic-yang-models/.bazelrc.user"]
                for path in paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(prior)
                env = {"RUNNER_TEMP": str(root / "tmp"),
                       "SOURCE_MIRRORS_URL": "https://sources.invalid/mirrors.cfg",
                       "SOURCE_MIRRORS_SHA256": hashlib.sha256(data).hexdigest() if valid else "0" * 64}
                previous = Path.cwd()
                try:
                    os.chdir(root)
                    with patch.dict(os.environ, env), patch("urllib.request.urlopen", return_value=io.BytesIO(data)) as download:
                        if valid:
                            exec(compile(script, str(WORKFLOW), "exec"), {})
                        else:
                            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                                exec(compile(script, str(WORKFLOW), "exec"), {})
                        download.assert_called_once_with(env["SOURCE_MIRRORS_URL"], timeout=60)
                finally:
                    os.chdir(previous)
                config = root / "tmp/sonic-source-mirrors.cfg"
                if valid:
                    self.assertEqual(config.read_bytes(), data)
                    expected = prior + "common --experimental_downloader_config=" + str(config) + "\n"
                    for path in paths:
                        self.assertEqual(path.read_text(), expected)
                else:
                    self.assertFalse(config.exists())
                    for path in paths:
                        self.assertEqual(path.read_text(), prior)

    def test_worker_cache_reaches_the_actual_image_root_query(self):
        build = self.steps()
        query = re.search(r"\(cd \.\./\.\. && (tools/bazel/ci/.*?cquery.*?)\) > artifacts/image-root-targets.txt", build, re.S)
        self.assertIsNotNone(query)
        command = query.group(1)
        wrapper = command.split()[0]
        for mode in ("native", "qemu-aarch64"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                home = work / "home"
                home.mkdir()
                cache = ("common --disk_cache=/retained/disk\n"
                         "common --repository_cache=/retained/repository\n")
                (home / ".bazelrc").write_text(cache)
                policy = "common --experimental_downloader_config=/verified/downloads.cfg\n"
                (work / ".bazelrc.user").write_text(policy)
                for relative in (wrapper, "tools/bazel/common.bazelrc"):
                    target = work / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / relative, target)
                record = work / "record.json"
                binary = work / "bin"
                binary.mkdir()
                recorder = binary / "bazel"
                recorder.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
rcs = [Path(arg.split('=', 1)[1]) for arg in args if arg.startswith('--bazelrc=')]
texts = [rc.read_text() for rc in rcs if rc.is_file()]
for text in list(texts):
    for line in text.splitlines():
        if line.startswith('try-import %workspace%/'):
            path = Path.cwd() / line.removeprefix('try-import %workspace%/')
            if path.is_file():
                texts.append(path.read_text())
Path(os.environ['RECORD']).write_text(json.dumps({'argv': args, 'explicit_rc': texts}))
""")
                recorder.chmod(0o755)
                env = dict(os.environ, HOME=str(home), PATH=str(binary) + os.pathsep + os.environ["PATH"],
                           RECORD=str(record), BASH_ENV="/dev/null", SONIC_CI_EXECUTION_MODE=mode)
                subprocess.run(["bash", "-euc", "bash " + command], cwd=work, env=env, check=True)
                observed = json.loads(record.read_text())
                self.assertIn("--nohome_rc", observed["argv"])
                self.assertIn("--bazelrc=/etc/bazel.bazelrc", observed["argv"])
                self.assertIn("--bazelrc=" + str(home / ".bazelrc"), observed["argv"])
                self.assertIn("cquery", observed["argv"])
                self.assertIn("//src/sonic-yang-models:yang_models_package_test", observed["argv"])
                self.assertIn("--lockfile_mode=update", observed["argv"])
                self.assertIn(cache, observed["explicit_rc"])
                self.assertIn(policy, observed["explicit_rc"])
                self.assertEqual((work / ".bazelrc.user").read_text(), policy)

    def test_legacy_helper_preserves_local_download_policy(self):
        setup = "python3 tools/bazel/ci/preserve_setup_bazel_cache.py"
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            self.copy_helper(work)
            home = work / "home"
            home.mkdir()
            (home / ".bazelrc").write_text("build --disk_cache=/disk\ncommon --repository_cache=/repository\n")
            before = "common --experimental_downloader_config=/verified/downloads.cfg\n"
            (work / ".bazelrc.user").write_text(before)
            subprocess.run(["bash", "-euc", setup], cwd=work, env=dict(os.environ, HOME=str(home), BASH_ENV="/dev/null"), check=True)
            self.assertTrue((work / ".bazelrc.user").read_text().startswith(before))

    def test_missing_or_duplicate_cache_settings_leave_local_rc_unchanged(self):
        setup = "python3 tools/bazel/ci/preserve_setup_bazel_cache.py"
        for settings in ("", "build --disk_cache=/disk\n", "build --disk_cache=/one\nbuild --disk_cache=/two\n"):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                self.copy_helper(work)
                home = work / "home"
                home.mkdir()
                (home / ".bazelrc").write_text(settings)
                before = "common --experimental_downloader_config=/verified/downloads.cfg\n"
                (work / ".bazelrc.user").write_text(before)
                result = subprocess.run(["bash", "-euc", setup], cwd=work, env=dict(os.environ, HOME=str(home), BASH_ENV="/dev/null"), capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cache setting", result.stderr)
                self.assertEqual((work / ".bazelrc.user").read_text(), before)


if __name__ == "__main__":
    unittest.main()
