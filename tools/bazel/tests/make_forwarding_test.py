#!/usr/bin/env python3
"""Exercise the real outer Makefile without starting Docker or a builder."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = (
    "target/docker-orchagent.gz",
    "target/docker-orchagent-dbg.gz",
    "target/sonic-vs.bin",
    "target/sonic-vs.img.gz",
    "target/sonic-vs.raw",
)


class MakeForwardingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "target").mkdir()
        (self.directory / "scripts").mkdir()
        (self.directory / "scripts/run_with_retry").symlink_to(ROOT / "scripts/run_with_retry")
        self.log = self.directory / "forwarded.log"
        targets = " ".join((*ARTIFACTS, "jessie", "bookworm", "docker-cleanup"))
        # This controlled inner Make records dispatch only. Its phony targets
        # ensure it never masks a successful handoff from the real outer file.
        (self.directory / "Makefile.work").write_text(f""".PHONY: {targets}
{targets}:
	@printf '%s|%s|%s|%s\\n' '$@' '$(BUILD_WITH_BAZEL_WHEN_AVAILABLE)' '$(BLDENV)' '$(EXTRA_DOCKER_TARGETS)' >> forwarded.log
	@test '$@' != '$(FAIL_TARGET)'
""")
        for target in ARTIFACTS:
            (self.directory / target).write_bytes(b"previously built artifact")

    def make(self, *targets, mode="y", **settings):
        self.log.unlink(missing_ok=True)
        environment = dict(os.environ)
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES", "MAKELEVEL"):
            environment.pop(name, None)
        environment["SONIC_BUILD_RETRY_COUNT"] = "0"
        options = {
            "NOJESSIE": "1", "NOSTRETCH": "1", "NOBUSTER": "1",
            "NOBULLSEYE": "1", "NOBOOKWORM": "1", "NOTRIXIE": "0",
            "BUILD_WITH_BAZEL_WHEN_AVAILABLE": mode,
        }
        options.update(settings)
        result = subprocess.run(
            ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), *targets]
            + [f"{key}={value}" for key, value in options.items()],
            cwd=self.directory, env=environment, text=True, capture_output=True,
            check=False,
        )
        calls = [line.split("|") for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, calls

    def state(self, target):
        path = self.directory / target
        return path.read_bytes(), path.stat().st_mtime_ns

    def test_existing_outputs_reach_inner_make_in_both_modes_and_on_repeats(self):
        """Let the inner build check every existing SWSS/VS output, including unchanged builds
        and both builder-switch directions, without touching those outputs in the wrapper.
        """
        for target in ARTIFACTS:
            before = self.state(target)
            for mode in ("y", "y", "n", "n", "y"):
                with self.subTest(target=target, mode=mode):
                    result, calls = self.make(target, mode=mode)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(calls, [[target, mode, "trixie", ""],
                                             ["docker-cleanup", mode, "bookworm", ""]])
                    self.assertEqual(self.state(target), before)

    def test_missing_output_retains_implicit_forwarding_recipe(self):
        """Keep first-time artifact requests using the existing wrapper recipe as well as
        requests for files already on disk.
        """
        for target in ARTIFACTS:
            with self.subTest(target=target):
                (self.directory / target).unlink()
                result, calls = self.make(target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls[0], [target, "y", "trixie", ""])

    def test_unrelated_existing_output_is_not_forced(self):
        """Leave existing outputs outside the SWSS and VS handoff scope up to date.
        """
        target = "target/docker-lldp.gz"
        (self.directory / target).write_bytes(b"unrelated archive")
        before = self.state(target)
        for mode in ("y", "n"):
            with self.subTest(mode=mode):
                result, calls = self.make(target, mode=mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(calls, [])
                self.assertEqual(self.state(target), before)

    def test_default_goal_remains_jessie(self):
        """Keep bare Make on its original default goal instead of selecting the force helper.
        """
        result, calls = self.make(NOJESSIE="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [["jessie", "y", "", ""]])

    def test_distribution_phases_and_requested_target_are_preserved(self):
        """Preserve the Bookworm prerequisite phase and forward the full artifact name to
        Trixie through the unchanged implicit recipe.
        """
        target = ARTIFACTS[0]
        result, calls = self.make(target, NOBOOKWORM="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [["bookworm", "y", "bookworm", "docker-orchagent.gz"],
                                 [target, "y", "trixie", ""],
                                 ["docker-cleanup", "y", "bookworm", ""]])

    def test_inner_failure_propagates_and_preserves_previous_output(self):
        """Report a failed inner build even when the requested artifact already exists, and
        leave that previous artifact intact.
        """
        target = ARTIFACTS[0]
        before = self.state(target)
        result, calls = self.make(target, FAIL_TARGET=target)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [[target, "y", "trixie", ""]])
        self.assertEqual(self.state(target), before)


if __name__ == "__main__":
    unittest.main()
