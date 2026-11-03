"""Preserve the real LLDP source contract and dget's default verification."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import historical_lldp_inputs as lldp

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "8bf48af011622657a73570a54d1a660b821bb30a"


class SourceContractTest(unittest.TestCase):
    def test_original_recipes_match_all_four_sources(self):
        for source in sorted(lldp.SOURCES):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                for name in lldp.RECIPES:
                    path = workspace / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(subprocess.check_output(["git", "show", source + ":" + name], cwd=ROOT))
                with patch.object(lldp.subprocess, "check_output", return_value=source + "\n"):
                    self.assertEqual(lldp.source_contract(workspace, source), lldp.RECIPES)
                    (workspace / "rules/lldpd.mk").write_text("changed version\n")
                    with self.assertRaisesRegex(ValueError, "recipe/version changed"):
                        lldp.source_contract(workspace, source)

    def test_nonhistorical_runtime_requires_explicit_review_and_exact_head(self):
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            lldp.source_contract(Path("/unused"), "1" * 40)
        with patch.object(lldp.subprocess, "check_output", return_value="2" * 40):
            with self.assertRaisesRegex(ValueError, "HEAD differs"):
                lldp.source_contract(Path("/unused"), "1" * 40, True)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self.recipe = self.workspace / "recipe"
        self.recipe.write_text("original LLDP version and recipe\n")
        self.recipes = {"recipe": hashlib.sha256(self.recipe.read_bytes()).hexdigest()}
        self.bin = self.root / "bin"
        self.bin.mkdir()
        git = self.bin / "git"
        git.write_text("#!/bin/sh\nprintf '%s\\n' " + SOURCE + "\n")
        git.chmod(0o755)
        self.delegate = self.root / "original-dget"
        self.delegate.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        self.delegate.chmod(0o755)
        self.launcher = self.bin / "dget"
        self.inputs = self.root / "source inputs"
        with patch.object(lldp, "RECIPES", self.recipes):
            lldp.write_launcher(self.launcher, self.delegate, self.inputs, self.workspace, SOURCE)
        self.environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def invoke(self, arguments):
        return subprocess.run([str(self.launcher), *arguments], env=self.environment,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_exact_url_only_preserves_default_signature_checks(self):
        result = self.invoke([lldp.ORIGINAL_URL])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["file:" + str(self.inputs / lldp.DSC)])

    def test_all_other_invocations_pass_through_unchanged(self):
        cases = [[], ["--version"], ["-u", lldp.ORIGINAL_URL],
                 ["-q", lldp.ORIGINAL_URL], [lldp.ORIGINAL_URL + "?different=1"],
                 [lldp.ORIGINAL_URL.replace("https:", "http:")],
                 [lldp.ORIGINAL_URL.replace("deb12u1", "deb12u2")],
                 ["-u", "p4lang-p4c_1.2.4.2-3.dsc", "http://download.opensuse.org/original.dsc"]]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.invoke(arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), arguments)

    def test_changed_source_recipe_fails_before_delegating_exact_url(self):
        self.recipe.write_text("different source\n")
        result = self.invoke([lldp.ORIGINAL_URL])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLDP recipe changed", result.stderr)
        self.assertEqual(self.invoke(["--version"]).returncode, 0)


class VerificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "source.dsc").write_bytes(b"signed source fixture")
        (self.root / "source.tar.gz").write_bytes(b"source member fixture")
        self.pins = {p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
                     for p in self.root.iterdir()}

    def test_exclusive_publication_preserves_a_launcher_created_meanwhile(self):
        candidate = self.root / "candidate"
        candidate.write_text("verified candidate\n")
        destination = self.root / "dget"
        destination.write_text("another install won\n")
        with self.assertRaises(FileExistsError):
            lldp.publish_launcher(candidate, destination, exclusive=True)
        self.assertEqual(destination.read_text(), "another install won\n")
        self.assertEqual(list(self.root.glob(".lldp-dget-*")), [])
        self.assertTrue(candidate.exists())

    def test_atomic_publication_exposes_complete_executable(self):
        candidate = self.root / "candidate"
        candidate.write_text("verified candidate\n")
        destination = self.root / "dget"
        destination.write_text("old launcher\n")
        replace = os.replace

        def inspect_before_publication(temporary, target):
            self.assertEqual(temporary.parent, destination.parent)
            self.assertEqual(temporary.read_text(), "verified candidate\n")
            self.assertEqual(temporary.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.read_text(), "old launcher\n")
            return replace(temporary, target)

        with patch.object(lldp.os, "replace", side_effect=inspect_before_publication):
            lldp.publish_launcher(candidate, destination)
        self.assertEqual(destination.read_text(), "verified candidate\n")
        self.assertFalse(candidate.exists())

    def test_missing_or_corrupted_member_rejected_before_signature_command(self):
        (self.root / "source.tar.gz").write_bytes(b"corrupt source member")
        with patch.object(lldp, "PINS", self.pins), patch.object(lldp.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "fingerprint differs"):
                lldp.verify_inputs(self.root)
            run.assert_not_called()

    def test_signature_failure_is_fatal_and_does_not_disable_checks(self):
        result = subprocess.CompletedProcess([], 1, "Bad signature\n")
        with patch.object(lldp, "PINS", self.pins), patch.object(lldp.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(ValueError, "signature/checksum verification failed"):
                lldp.verify_inputs(self.root)
            self.assertEqual(run.call_args.args[0], ["dscverify", "--no-conf", lldp.DSC])

    def test_failed_real_dget_smoke_leaves_existing_launcher_unchanged(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        (inputs / "receipt.json").write_text(json.dumps({
            "schema": 1, "status": "verified", "original_url": lldp.ORIGINAL_URL,
            "recipe_sha256": lldp.RECIPES, "files": {},
        }))
        launcher = self.root / "historical-source-transport/bin/dget"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"original private P4 launcher\n")
        before = lldp.fingerprint(launcher)["sha256"]
        with patch.object(lldp, "source_contract", return_value=lldp.RECIPES), \
             patch.object(lldp, "verify_inputs", return_value={"files": {}}), \
             patch.object(lldp, "smoke_dget", side_effect=ValueError("signature/extraction failed")):
            with self.assertRaisesRegex(ValueError, "signature/extraction failed"):
                lldp.install(self.root, SOURCE, inputs, self.root / "runtime",
                             replace_launcher=launcher, expected_launcher_sha256=before)
        self.assertEqual(lldp.fingerprint(launcher)["sha256"], before)

    def test_changed_previous_launcher_is_never_replaced(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        (inputs / "receipt.json").write_text(json.dumps({
            "schema": 1, "status": "verified", "original_url": lldp.ORIGINAL_URL,
            "recipe_sha256": lldp.RECIPES, "files": {},
        }))
        launcher = self.root / "historical-source-transport/bin/dget"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("new launcher\n")
        with patch.object(lldp, "source_contract", return_value=lldp.RECIPES), \
             patch.object(lldp, "verify_inputs", return_value={"files": {}}):
            with self.assertRaisesRegex(ValueError, "launcher changed"):
                lldp.install(self.root, SOURCE, inputs, self.root / "runtime",
                             replace_launcher=launcher, expected_launcher_sha256="0" * 64)
        self.assertEqual(launcher.read_text(), "new launcher\n")


if __name__ == "__main__":
    unittest.main()
