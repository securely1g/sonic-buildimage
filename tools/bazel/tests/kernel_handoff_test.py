#!/usr/bin/env python3
"""Exercise CI transfer with real fixture DEBs and no kernel build fallback."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import kernel_handoff as handoff
from tools.bazel.kernel import prepare as kernel
from tools.bazel.tests import kernel_test


class KernelHandoffTest(unittest.TestCase):
    def setUp(self):
        self.fixture = kernel_test.KernelBundleTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.output = self.root / "ci-handoff"
        self.destination = self.root / "imported/debs/trixie"
        self.names = {*self.fixture.packages, kernel.MANIFEST, kernel.PROVENANCE}
        self.architecture = self.fixture.architecture
        for name in ("build", "_build"):
            guard = patch.object(kernel, name, side_effect=AssertionError("CI handoff must never build a kernel"))
            guard.start()
            self.addCleanup(guard.stop)

    def export(self):
        return handoff.export_bundle(self.root, self.fixture.bundle, self.output, self.architecture)

    def restore(self, **options):
        return handoff.import_bundle(self.root, self.output, self.destination,
                                     options.get("architecture", self.architecture))

    def contents(self, directory):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in directory.iterdir() if path.is_file()}

    def test_round_trip_preserves_all_bytes_and_make_ensure_reuses_without_build(self):
        unrelated = self.fixture.bundle / "unrelated.deb"
        unrelated.write_bytes(b"unrelated source package")
        expected = {name: (self.fixture.bundle / name).read_bytes() for name in self.names}
        self.export()
        self.assertEqual({path.name for path in self.output.iterdir()}, self.names)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, expected)
        self.assertEqual(unrelated.read_bytes(), b"unrelated source package")
        self.restore()
        self.assertEqual({path.name: path.read_bytes() for path in self.destination.iterdir()}, expected)
        unrelated = self.destination / "unrelated.deb"
        unrelated.write_bytes(b"preserve destination package")
        before = self.contents(self.destination)
        self.restore()
        self.assertEqual(self.contents(self.destination), before)
        self.assertTrue(kernel.ensure(self.root, architecture=self.architecture,
                                      debs_directory=self.destination)["reused"])
        self.assertEqual(self.contents(self.destination), before)
        self.assertFalse((self.root / "target/bazel-kernel-state").exists())

    def test_transfer_to_another_checkout_of_the_same_commit(self):
        self.export()
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            shutil.copytree(self.root, consumer)
            destination = consumer / kernel.DEBS_DIRECTORY
            shutil.rmtree(destination)
            handoff.import_bundle(consumer, self.output, kernel.DEBS_DIRECTORY, self.architecture)
            self.assertTrue(kernel.ensure(consumer, architecture=self.architecture)["reused"])
            self.assertEqual({path.name for path in destination.iterdir()}, self.names)

    def test_export_refuses_missing_corrupt_or_symlink_packages(self):
        package = self.fixture.bundle / next(iter(self.fixture.packages))
        original = package.read_bytes()
        for damage in ("missing", "corrupt", "symlink"):
            with self.subTest(damage=damage):
                package.unlink()
                if damage == "corrupt":
                    package.write_bytes(original + b"changed")
                elif damage == "symlink":
                    external = self.root / "external.deb"
                    external.write_bytes(original)
                    package.symlink_to(external)
                with self.assertRaises(ValueError):
                    self.export()
                self.assertFalse(self.output.exists())
                package.unlink(missing_ok=True)
                package.write_bytes(original)

    def test_import_rejects_extra_missing_directory_and_symlink_inputs(self):
        self.export()
        extra = self.output / "unexpected"
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "file":
                    extra.write_text("not part of the contract")
                elif kind == "directory":
                    extra.mkdir()
                else:
                    extra.symlink_to(self.fixture.bundle)
                with self.assertRaisesRegex(ValueError, "exactly five DEBs"):
                    self.restore()
                self.assertFalse(self.destination.exists())
                extra.rmdir() if kind == "directory" else extra.unlink()
        package = self.output / next(iter(self.fixture.packages))
        original = package.read_bytes()
        package.unlink()
        with self.assertRaisesRegex(ValueError, "exactly five DEBs"):
            self.restore()
        package.symlink_to(self.fixture.bundle / package.name)
        with self.assertRaisesRegex(ValueError, "regular kernel output"):
            self.restore()
        package.unlink()
        package.write_bytes(original)
        linked = self.root / "linked-handoff"
        linked.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ordinary directory"):
            handoff.import_bundle(self.root, linked, self.destination, self.architecture)

    def test_rejects_invalid_or_ambiguous_metadata_before_publication(self):
        self.export()
        manifest = self.output / kernel.MANIFEST
        original = manifest.read_bytes()
        for data in (b"{", b"[]", b'{"schema": 3, "schema": 3}', b'{"value": NaN}'):
            with self.subTest(data=data):
                manifest.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.restore()
                self.assertFalse(self.destination.exists())
        manifest.write_bytes(original)

    def test_rejects_recorded_credentials_and_environment(self):
        for field in ("access_token", "environment"):
            with self.subTest(field=field):
                self.fixture.provenance[field] = "must-not-be-transferred"
                self.fixture.write_manifest()
                with self.assertRaises(ValueError):
                    self.export()
                self.assertFalse(self.output.exists())
                del self.fixture.provenance[field]

    def test_package_control_is_checked_even_when_the_archive_digest_matches(self):
        name, expected = next(iter(self.fixture.packages.items()))
        self.fixture.write_deb(name, dict(expected, package="different-package"))
        item = next(item for item in self.fixture.manifest["packages"] if item["name"] == name)
        item["package"] = expected["package"]
        self.fixture.manifest["created_packages"] = [dict(item, exported=True)
                                                      for item in self.fixture.manifest["packages"]]
        self.fixture.write_manifest()
        with self.assertRaisesRegex(ValueError, "DEB control differs"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_export_keeps_the_existing_source_and_debug_validation(self):
        for field, value, message in (("source_tree_sha256", "f" * 64, "source inputs differ"),
                                      ("debug", {}, "runtime/debug build identity")):
            with self.subTest(field=field):
                original = self.fixture.manifest[field]
                self.fixture.manifest[field] = value
                self.fixture.write_manifest()
                with self.assertRaisesRegex(ValueError, message):
                    self.export()
                self.assertFalse(self.output.exists())
                self.fixture.manifest[field] = original

    def test_import_rejects_source_configuration_and_architecture_mismatch(self):
        self.export()
        provenance = self.output / kernel.PROVENANCE
        original = provenance.read_bytes()
        for field, value in (("source_commit", "f" * 40),
                             ("consumer_inputs_sha256", "f" * 64),
                             ("kernel_gitlink", "f" * 40),
                             ("kernel_config", {})):
            with self.subTest(field=field):
                changed = json.loads(original)
                changed[field] = value
                provenance.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    self.restore()
                self.assertFalse(self.destination.exists())
        provenance.write_bytes(original)
        other = "arm64" if self.architecture == "amd64" else "amd64"
        with self.assertRaises(ValueError):
            self.restore(architecture=other)

    def test_same_source_commit_is_required_even_when_kernel_inputs_are_unchanged(self):
        self.export()
        (self.root / "unrelated.txt").write_text("an unrelated commit")
        subprocess.run(["git", "-C", str(self.root), "add", "unrelated.txt"], check=True)
        self.fixture.commit(self.root, "Different CI source ref")
        with self.assertRaisesRegex(ValueError, "same source commit"):
            self.restore()
        self.assertFalse(self.destination.exists())

    def test_changed_staging_copy_cannot_replace_existing_packages(self):
        self.export()
        self.restore()
        before = self.contents(self.destination)
        copy_files = handoff.copy_files
        def corrupt_copy(source, destination, names):
            copy_files(source, destination, names)
            package = destination / next(iter(self.fixture.packages))
            package.write_bytes(package.read_bytes() + b"damaged during copy")
        with patch.object(handoff, "copy_files", side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                self.restore()
        self.assertEqual(self.contents(self.destination), before)

    def test_destination_symlinks_and_existing_export_are_rejected(self):
        self.export()
        before = self.contents(self.output)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.export()
        self.assertEqual(self.contents(self.output), before)
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(self.fixture.bundle, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self.restore()
        self.destination.unlink()
        self.destination.mkdir()
        package = self.destination / next(iter(self.fixture.packages))
        package.symlink_to(self.fixture.bundle / package.name)
        with self.assertRaisesRegex(ValueError, "regular files"):
            self.restore()
        self.assertTrue(package.is_symlink())
        self.assertFalse((self.destination / kernel.PROVENANCE).exists())

    def check_cli_round_trip(self, fixture):
        script = ROOT / "tools/bazel/ci/kernel_handoff.py"
        common = [sys.executable, "-B", str(script)]
        # The helper intentionally defaults to AMD64 for the two VS jobs.
        # Exercise that default on AMD64 and select ARM64 in the hosted matrix.
        architecture = [] if fixture.architecture == "amd64" else ["--architecture", fixture.architecture]
        output = fixture.root / "cli-handoff"
        destination = fixture.root / "cli-imported/debs/trixie"
        for command, options in (
            ("export", ["--debs-directory", str(fixture.bundle), "--output", str(output)]),
            ("import", ["--debs-directory", str(destination), "--input", str(output)]),
        ):
            result = subprocess.run(common + [command, "--workspace", str(fixture.root)] + architecture + options,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["verified_packages"], 5)
            self.assertEqual(receipt["architecture"], fixture.architecture)
        self.assertTrue(kernel.ensure(fixture.root, architecture=fixture.architecture,
                                      debs_directory=destination)["reused"])

    def test_cli_exports_and_imports_with_default_native_configuration(self):
        self.check_cli_round_trip(self.fixture)

    def test_cli_round_trip_with_simulated_arm64_package_metadata(self):
        # dpkg-deb can package/inspect ARM64 metadata on either hosted CPU.
        # This catches accidental reliance on the CLI's AMD64 default locally.
        with patch.object(kernel, "native_architecture", return_value="arm64"):
            fixture = kernel_test.KernelBundleTest()
            self.addCleanup(fixture.doCleanups)
            fixture.setUp()
        self.assertEqual(fixture.architecture, "arm64")
        self.check_cli_round_trip(fixture)


if __name__ == "__main__":
    unittest.main()
