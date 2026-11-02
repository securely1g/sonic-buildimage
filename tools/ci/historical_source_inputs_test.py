"""Exercise original recipes, Git-object transport and narrow dget rewriting."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import historical_source_inputs as sources

ROOT = Path(__file__).resolve().parents[2]


class HistoricalContractTest(unittest.TestCase):
    def test_selectors_and_urls_match_all_three_original_sources(self):
        for source in sources.INPUTS.keys() - sources.P4_INPUTS.keys():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                for name in sources.SOURCE_FILES:
                    path = workspace / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(subprocess.check_output(["git", "show", f"{source}:{name}"], cwd=ROOT))
                with patch.object(sources, "git", return_value=source):
                    self.assertEqual(set(sources.source_contract(workspace, source)), set(sources.SOURCE_FILES))
                    path = workspace / "rules/monit.mk"
                    path.write_text(path.read_text().replace("5.34.3-1", "5.35.0-1"))
                    with self.assertRaisesRegex(ValueError, "Monit selector changed"):
                        sources.source_contract(workspace, source)


class GitBundleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "origin"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        (self.repository / "source").write_text("original source\n")
        self.command("add", "source")
        self.command("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "Original source")
        self.commit = self.command("rev-parse", "HEAD")
        self.command("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "tag", "-am", "Original selector", "debian/1%5.34.3-1")
        self.spec = copy.deepcopy(sources.GIT_SOURCES["monit"])
        self.bundle = self.root / "monit.bundle"
        self.receipt = sources.create_bundle(self.repository, self.bundle, self.spec)

    def command(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.repository), *arguments], text=True).strip()

    def test_original_clone_and_reset_use_identical_git_objects(self):
        mirror = self.root / "mirror.git"
        sources.verify_bundle(self.bundle, self.receipt, self.spec, mirror)
        environment = {**os.environ, "GIT_CONFIG_COUNT": "2",
                       "GIT_CONFIG_KEY_0": "url." + mirror.as_uri() + ".insteadOf",
                       "GIT_CONFIG_VALUE_0": self.spec["url"],
                       "GIT_CONFIG_KEY_1": "protocol.file.allow", "GIT_CONFIG_VALUE_1": "always"}
        clone = self.root / "recipe-clone"
        subprocess.run(["git", "clone", self.spec["url"], str(clone)], env=environment, check=True)
        subprocess.run(["git", "-C", str(clone), "reset", "--hard", "debian/1%5.34.3-1"], check=True)
        self.assertEqual(sources.git(clone, "rev-parse", "HEAD"), self.commit)
        self.assertEqual((clone / "source").read_text(), "original source\n")
        self.assertEqual(sources.git_identity(clone, self.spec), sources.git_identity(self.repository, self.spec))

    def test_rejects_corrupted_bundle_before_clone(self):
        self.bundle.write_bytes(self.bundle.read_bytes() + b"corruption")
        with self.assertRaisesRegex(ValueError, "bundle bytes differ"):
            sources.verify_bundle(self.bundle, self.receipt, self.spec, self.root / "bad.git")
        self.assertFalse((self.root / "bad.git").exists())

    def test_rejects_changed_selected_commit(self):
        changed = {**self.receipt, "commit": "0" * 40}
        with self.assertRaisesRegex(ValueError, "selector/commit/tree differs"):
            sources.verify_bundle(self.bundle, changed, self.spec, self.root / "wrong.git")


class DgetTransportTest(unittest.TestCase):
    def test_rewrites_only_exact_original_arguments(self):
        directory = Path("/private/input with spaces")
        for package, version in sources.p4.VERSIONS.items():
            name = f"{package}_{version}.dsc"
            arguments = ["-u", name, sources.P4_URL + name]
            self.assertEqual(sources.dget_arguments(arguments, directory),
                             ["-u", name, "file:" + str(directory / name)])
            for other in (["-d", *arguments], ["-q", *arguments[1:]],
                          [*arguments[:2], arguments[2].replace("http:", "https:")],
                          [*arguments[:2], arguments[2] + "?different"],
                          ["-u", "other.dsc", arguments[2]],
                          ["-u", name, "https://example.invalid/" + name]):
                with self.subTest(arguments=other):
                    self.assertEqual(sources.dget_arguments(other, directory), other)

    @unittest.skipUnless(shutil.which("dget") and shutil.which("dpkg-source"), "requires the Debian source tools")
    def test_actual_dget_extracts_local_files_without_changing_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            source = inputs / "p4lang-pi-0.1.3"
            source.mkdir(parents=True)
            (source / "payload").write_text("original source bytes\n")
            with tarfile.open(inputs / "p4lang-pi_0.1.3.orig.tar.gz", "w:gz") as archive:
                archive.add(source, arcname=source.name)
            (source / "debian/source").mkdir(parents=True)
            (source / "debian/source/format").write_text("3.0 (quilt)\n")
            (source / "debian/control").write_text(
                "Source: p4lang-pi\nSection: devel\nPriority: optional\n"
                "Maintainer: Fixture <fixture@example.invalid>\nStandards-Version: 4.5.0\n\n"
                "Package: p4lang-pi\nArchitecture: any\nDescription: Transport fixture\n"
                " Tiny source archive for a dget transport test.\n")
            (source / "debian/changelog").write_text(
                "p4lang-pi (0.1.3-2) unstable; urgency=medium\n\n  * Fixture.\n\n"
                " -- Fixture <fixture@example.invalid>  Sun, 01 Nov 2026 00:00:00 +0000\n")
            subprocess.run(["dpkg-source", "-b", str(source)], cwd=inputs, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            launcher = root / "dget-local"
            sources.write_dget_launcher(launcher, shutil.which("dget"), inputs)
            receipt = sources.smoke_dget(launcher, inputs, root)
            self.assertEqual(receipt["status"], "passed")
            self.assertTrue(receipt["input_files_unchanged"])
            self.assertFalse(list(root.glob("dget-smoke-*")))


if __name__ == "__main__":
    unittest.main()
