"""Exercise targeted repository prewarming without downloading a toolchain."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class VsRepositoryPrewarmTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vs repository prewarm ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").symlink_to(ROOT / "tools", target_is_directory=True)
        self.cache = self.root / "shared contents"
        self.bazel = self.root / "fake bazel"
        self.bazel.write_text("#!" + sys.executable + "\n" + r'''
import json, os, pathlib, sys
args = sys.argv[1:]
base = pathlib.Path(args.pop(0).split("=", 1)[1])
with pathlib.Path("calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"base": str(base), "args": args}) + "\n")
mode = os.environ.get("PREWARM_MODE")
if args[0] == "fetch":
    if mode == "fetch-failure":
        sys.exit(17)
    cache = pathlib.Path(next(arg.split("=", 1)[1] for arg in args
                              if arg.startswith("--repo_contents_cache=")))
    external = base / "external"
    external.mkdir()
    repo = external / "sonic-fips++clang+symcrypt_llvm_llvm"
    if mode == "not-cached":
        repo.mkdir()
    else:
        destination = cache / "digest" / "0"
        destination.mkdir(parents=True, exist_ok=True)
        repo.symlink_to(destination, target_is_directory=True)
elif mode == "shutdown-failure":
    sys.exit(19)
''')
        self.bazel.chmod(0o755)
        self.environment = dict(
            os.environ, BAZEL=str(self.bazel), BAZEL_REMOTE_AUTH="",
            SONIC_BAZEL_REMOTE_CACHE="", SONIC_BAZEL_REMOTE_UPLOAD="n",
            BAZEL_CONTAINER_CACHE_DIR=str(self.root / "cache"),
            BAZEL_CONTAINER_ARGS=f"--repo_contents_cache='{self.cache}' --remote_timeout=600s",
        )

    def run_prewarm(self, *args, **overrides):
        return subprocess.run(
            ["bash", str(ROOT / "tools/bazel/ci/prewarm_vs_repositories.sh"), *args],
            cwd=self.root, env=dict(self.environment, **overrides),
            capture_output=True, text=True)

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_fetches_only_llvm_into_shared_cache_and_shuts_down(self):
        result = self.run_prewarm()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fetch, shutdown = self.calls()
        self.assertEqual(fetch["args"][0], "fetch")
        self.assertIn("--remote_timeout=600s", fetch["args"])
        self.assertEqual(fetch["args"][-1], "--repo=@@sonic-fips++clang+symcrypt_llvm_llvm")
        self.assertFalse(any(arg in ("--all", "--force") for arg in fetch["args"]))
        self.assertEqual(shutdown["args"], ["shutdown"])
        self.assertEqual(fetch["base"], shutdown["base"])
        self.assertFalse(Path(fetch["base"]).exists())
        timing, = (self.root / "artifacts/vs/image-timings/repository-prewarm").iterdir()
        self.assertEqual(json.loads((timing / "recipe.json").read_text()),
                         {"bazel": ["fetch", "shutdown"], "profiles": ["fetch"], "exports": []})
        self.assertTrue(any(arg.startswith("--profile=") for arg in fetch["args"]))
        for phase in ("fetch", "shutdown"):
            self.assertEqual(json.loads((timing / f"{phase}.timing.json").read_text())["exit_code"], 0)
        self.assertTrue((self.cache / "digest/0").is_dir())

    def test_fetch_failure_is_not_hidden_by_successful_shutdown(self):
        result = self.run_prewarm(PREWARM_MODE="fetch-failure")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()], ["fetch", "shutdown"])
        self.assertFalse(Path(self.calls()[0]["base"]).exists())

    def test_ineligible_repository_fails_instead_of_claiming_cache_success(self):
        result = self.run_prewarm(PREWARM_MODE="not-cached")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("LLVM repository was not shared", result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()], ["fetch", "shutdown"])

    def test_shutdown_failure_is_reported_and_preserves_base(self):
        result = self.run_prewarm(PREWARM_MODE="shutdown-failure", TMPDIR=str(self.root))
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertIn("Could not clean up private Bazel server", result.stderr)
        self.assertTrue(Path(self.calls()[0]["base"]).exists())

    def test_missing_or_relative_contents_cache_is_rejected(self):
        for args in ("", "--repo_contents_cache=relative"):
            with self.subTest(args=args):
                result = self.run_prewarm(BAZEL_CONTAINER_ARGS=args)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("requires an absolute", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_extra_arguments_are_rejected(self):
        result = self.run_prewarm("--all")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.calls(), [])
