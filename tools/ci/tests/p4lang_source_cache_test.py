"""Exercise native P4 source selection without downloads or package builds."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[3]
PACKAGES = {"pi": ("PI", "0.1.3", "0.1.3-2"),
            "bmv2": ("BMV2", "1.15.0", "1.15.0-9"),
            "p4c": ("P4C", "1.2.4.2", "1.2.4.2-3")}


class SourceCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p4-source-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "src/p4lang"
        self.source.mkdir(parents=True)
        for name in ("Makefile", "source_cache.py", "source-cache.json", "p4lang-p4c-source.dsc.patch"):
            shutil.copy2(REPO / "src/p4lang" / name, self.source / name)
        self.inputs = self.root / "local inputs"
        self.inputs.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.jsonl"
        script = """#!/usr/bin/env python3
import json, os, pathlib, sys
with open(os.environ["P4_TEST_CALLS"], "a") as output:
    output.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
sys.exit(int(os.environ.get("P4_TEST_COMMAND_STATUS", "0")))
"""
        for name in ("dget", "dpkg-source", "dpkg-buildpackage", "patch"):
            path = self.bin / name
            path.write_text(script)
            path.chmod(0o755)
        self.environment = dict(os.environ, P4_TEST_CALLS=str(self.calls), LC_ALL="C")
        for name in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "P4LANG_SOURCE_DIR"):
            self.environment.pop(name, None)
        self.environment["PATH"] = str(self.bin) + os.pathsep + os.environ["PATH"]
        # Small fixture bytes use the production verifier and manifest format;
        # the separate offline integration proof uses all nine real archives.
        self.manifest = json.loads((self.source / "source-cache.json").read_text())
        for package in self.manifest["packages"].values():
            for filename in package["files"]:
                data = (filename + "\n").encode()
                (self.inputs / filename).write_bytes(data)
                package["files"][filename] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        (self.source / "source-cache.json").write_text(json.dumps(self.manifest))

    def run_make(self, target, *assignments, status=0):
        variables = ["DEST=output", "CONFIGURED_ARCH=amd64"]
        for variable, version, full in PACKAGES.values():
            variables += [f"P4LANG_{variable}_VERSION={version}", f"P4LANG_{variable}_VERSION_FULL={full}"]
        return subprocess.run(["make", "--no-print-directory", target, *variables, *assignments],
                              cwd=self.source, env=dict(self.environment, P4_TEST_COMMAND_STATUS=str(status)),
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def recorded_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def reset_calls(self):
        self.calls.unlink(missing_ok=True)

    def test_preflight_verifies_all_nine_without_changing_inputs(self):
        before = {path.name: path.read_bytes() for path in self.inputs.iterdir()}
        result = subprocess.run([sys.executable, "-B", str(self.source / "source_cache.py"), "verify",
                                 "--source-dir", str(self.inputs)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(len(receipt["files"]), 9)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.inputs.iterdir()})
        self.assertEqual(self.recorded_calls(), [])

    def test_default_download_is_one_url_for_each_source_and_package_target(self):
        for package, (_, _, full) in PACKAGES.items():
            for target in (f"prepare-{package}-source", f"output/p4lang-{package}_{full}_amd64.deb"):
                with self.subTest(target=target):
                    self.reset_calls()
                    result = self.run_make(target, status=71)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.recorded_calls(), [["dget", "-u",
                        f"http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-{package}_{full}.dsc"]])

    def test_preflight_rejects_a_bad_last_input_without_a_success_receipt(self):
        filename = next(reversed(self.manifest["packages"]["p4lang-p4c"]["files"]))
        (self.inputs / filename).write_bytes(b"corrupt")
        result = subprocess.run([sys.executable, "-B", str(self.source / "source_cache.py"), "verify",
                                 "--source-dir", str(self.inputs)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_default_download_keeps_legacy_version_selection(self):
        result = self.run_make("prepare-pi-source", "P4LANG_PI_VERSION_FULL=99.99-1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.recorded_calls()[0][-1].endswith("p4lang-pi_99.99-1.dsc"))

    def test_local_inputs_are_verified_before_extraction_and_never_downloaded(self):
        before = {path.name: path.read_bytes() for path in self.inputs.iterdir()}
        for package, (_, _, full) in PACKAGES.items():
            with self.subTest(package=package):
                self.reset_calls()
                result = self.run_make(f"prepare-{package}-source", f"P4LANG_SOURCE_DIR={self.inputs}")
                self.assertEqual(result.returncode, 0, result.stdout)
                expected = [["dpkg-source", "--require-strong-checksums", "-x", f"p4lang-{package}_{full}.dsc"]]
                if package == "p4c":
                    expected.insert(0, ["patch", "--fuzz=0", "--batch", "--forward", "--reject-file=-", "-p0",
                                        "-i", "p4lang-p4c-source.dsc.patch"])
                self.assertEqual(self.recorded_calls(), expected)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.inputs.iterdir()})

    def test_each_corrupt_input_fails_before_copy_or_extraction(self):
        for package, (_, _, full) in PACKAGES.items():
            for filename in self.manifest["packages"]["p4lang-" + package]["files"]:
                for target in (f"prepare-{package}-source", f"output/p4lang-{package}_{full}_amd64.deb"):
                    with self.subTest(filename=filename, target=target):
                        path = self.inputs / filename
                        original = path.read_bytes()
                        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                        self.reset_calls()
                        result = self.run_make(target, f"P4LANG_SOURCE_DIR={self.inputs}")
                        path.write_bytes(original)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("SHA256 mismatch", result.stdout)
                        self.assertEqual(self.recorded_calls(), [])
                        self.assertFalse((self.source / filename).exists())

    def test_changed_working_copy_fails_before_extraction(self):
        copier = self.bin / "cp"
        copier.write_text("#!/usr/bin/env python3\nimport pathlib, subprocess, sys\n"
                          "subprocess.run(['/bin/cp', *sys.argv[1:]], check=True)\n"
                          "p = pathlib.Path(pathlib.Path(sys.argv[1]).name)\n"
                          "data = p.read_bytes()\n"
                          "p.write_bytes(bytes([data[0] ^ 1]) + data[1:])\n")
        copier.chmod(0o755)
        result = self.run_make("prepare-pi-source", f"P4LANG_SOURCE_DIR={self.inputs}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256 mismatch", result.stdout)
        self.assertEqual(self.recorded_calls(), [])

    def test_extraction_failure_stops_the_native_package_target(self):
        result = self.run_make("output/p4lang-pi_0.1.3-2_amd64.deb",
                               f"P4LANG_SOURCE_DIR={self.inputs}", status=71)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.recorded_calls(), [["dpkg-source", "--require-strong-checksums", "-x",
                                                  "p4lang-pi_0.1.3-2.dsc"]])

    def test_missing_symlink_and_directory_inputs_are_rejected(self):
        for kind in ("missing", "symlink", "directory"):
            for package, metadata in self.manifest["packages"].items():
                for filename in metadata["files"]:
                    with self.subTest(kind=kind, filename=filename):
                        path = self.inputs / filename
                        original = path.read_bytes()
                        path.unlink()
                        if kind == "symlink":
                            (self.root / "link-target").write_bytes(original)
                            path.symlink_to(self.root / "link-target")
                        elif kind == "directory":
                            path.mkdir()
                        self.reset_calls()
                        result = self.run_make(f"prepare-{package.removeprefix('p4lang-')}-source",
                                               f"P4LANG_SOURCE_DIR={self.inputs}")
                        if path.is_symlink():
                            path.unlink()
                        elif path.is_dir():
                            path.rmdir()
                        path.write_bytes(original)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(self.recorded_calls(), [])

    def test_unpinned_versions_fail_before_copy(self):
        for package, (variable, _, _) in PACKAGES.items():
            for suffix in ("VERSION", "VERSION_FULL"):
                with self.subTest(package=package, suffix=suffix):
                    result = self.run_make(f"prepare-{package}-source", f"P4LANG_SOURCE_DIR={self.inputs}",
                                           f"P4LANG_{variable}_{suffix}=99.99")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unsupported pinned local P4 source version", result.stdout)
                    self.assertEqual(self.recorded_calls(), [])

    def test_descriptor_patch_changes_only_three_orig_checksum_rows(self):
        patch = (self.source / "p4lang-p4c-source.dsc.patch").read_text().splitlines()
        body = patch[3:]
        original = "Format: 3.0 (quilt)\nSource: p4lang-p4c\nVersion: 1.2.4.2-3\n" + "\n".join(
            line[1:] for line in body if line.startswith((" ", "-"))) + "\n"
        expected = "Format: 3.0 (quilt)\nSource: p4lang-p4c\nVersion: 1.2.4.2-3\n" + "\n".join(
            line[1:] for line in body if line.startswith((" ", "+"))) + "\n"
        self.assertEqual(len([line for line in body if line.startswith("-")]), 3)
        self.assertEqual(len([line for line in body if line.startswith("+")]), 3)
        for line in body:
            if line.startswith(("-", "+")):
                self.assertTrue(line.endswith("p4lang-p4c_1.2.4.2.orig.tar.gz"))
        descriptor = self.source / "p4lang-p4c_1.2.4.2-3.dsc"
        descriptor.write_text(original)
        result = subprocess.run(["/usr/bin/patch", "--fuzz=0", "--batch", "--forward", "-p0", "-i",
                                 "p4lang-p4c-source.dsc.patch"], cwd=self.source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(descriptor.read_text(), expected)
        self.assertIn("Checksums-Sha1:\n", expected)
        self.assertIn("Checksums-Sha256:\n", expected)

    def test_already_repaired_input_descriptor_is_rejected(self):
        body = (self.source / "p4lang-p4c-source.dsc.patch").read_text().splitlines()[3:]
        original = "\n".join(line[1:] for line in body if line.startswith((" ", "-"))) + "\n"
        repaired = "\n".join(line[1:] for line in body if line.startswith((" ", "+"))) + "\n"
        filename = "p4lang-p4c_1.2.4.2-3.dsc"
        self.manifest["packages"]["p4lang-p4c"]["files"][filename] = {
            "bytes": len(original.encode()), "sha256": hashlib.sha256(original.encode()).hexdigest()}
        (self.source / "source-cache.json").write_text(json.dumps(self.manifest))
        (self.inputs / filename).write_text(repaired)
        result = self.run_make("prepare-p4c-source", f"P4LANG_SOURCE_DIR={self.inputs}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256 mismatch", result.stdout)
        self.assertEqual(self.recorded_calls(), [])

    def test_package_cache_tracks_source_selection_and_all_source_files(self):
        (self.root / "rules").mkdir()
        for name in ("p4lang.mk", "p4lang.dep"):
            shutil.copy2(REPO / "rules" / name, self.root / "rules" / name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "src", "rules"], cwd=self.root, check=True)
        harness = self.root / "cache.mk"
        harness.write_text("SRC_PATH := src\nCONFIGURED_ARCH := amd64\nSONIC_COMMON_FLAGS_LIST := base\n"
                           "include rules/p4lang.mk\ninclude rules/p4lang.dep\nshow:\n"
                           "\t@$(foreach p,$(SONIC_MAKE_DEBS),printf '%s\\n' '$(p)|$($(p)_DEP_FLAGS)|$($(p)_DEP_FILES)';)\n")
        outputs = []
        for choice in ("", "/cache/source-a", "/cache/source-b"):
            result = subprocess.run(["make", "--no-print-directory", "-f", str(harness), "show",
                                     "P4LANG_SOURCE_DIR=" + choice], cwd=self.root, env=self.environment,
                                    text=True, capture_output=True, check=True)
            outputs.append(result.stdout)
            for row in result.stdout.splitlines():
                _, flags, files = row.split("|")
                if choice:
                    self.assertIn(choice, flags.split())
                for name in ("source_cache.py", "source-cache.json", "p4lang-p4c-source.dsc.patch"):
                    self.assertIn("src/p4lang/" + name, files.split())
        self.assertEqual(len(set(outputs)), 3)


if __name__ == "__main__":
    unittest.main()
