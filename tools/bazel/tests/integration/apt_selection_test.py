#!/usr/bin/env python3
"""Exercise shared APT selection with local OCI/control TARs and owner callbacks.

The fixtures contain no Debian package archives and require no container build.
Image-specific policy and FIPS replacement authorization stay in owner suites.
"""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.bazel.oci import apt_selection as subject
from tools.bazel.tests.oci_base_fixture import digest, layer_tar, oci_files, write_layout


class Fixture:
    """Supply a layered base, retained package and locked addition for any owner."""

    def __init__(self, root, image="telemetry", architecture="amd64"):
        self.root = root / image
        self.root.mkdir()
        self.architecture = architecture
        self.base = self.root / "base.oci"
        status = ("Package: platform-lib\nVersion: 3.0\nArchitecture: " + architecture +
                  "\nStatus: install ok installed\n\n").encode()
        self.layers = [
            layer_tar({"var/lib/dpkg/status": status, "etc/shared.conf": b"obsolete",
                       "etc/removed.conf": b"removed by next layer"}),
            layer_tar({"etc/shared.conf": b"current", "etc/.wh.removed.conf": b""}),
        ]
        self.write_base()
        self.retained = {"local-driver": {"source_sha256": "a" * 64, "control": {
            "Package": "local-driver", "Version": "2.0", "Architecture": architecture}}}
        self.addition = image + "-addon"
        self.paths, packages, mapping, roots = {}, {}, [], {}
        for name in ("platform-lib", "local-driver", self.addition):
            path = self.root / (name + ".tar")
            path.write_bytes(layer_tar({"etc/shared.conf": b"current",
                                        "etc/removed.conf": b"restored by addition"}))
            fields = {"Package": name, "Version": "1.0", "Architecture": architecture}
            if name == self.addition:
                fields["Depends"] = "platform-lib (>= 3.0), local-driver (>= 2.0)"
            control = self.root / (name + ".control.tar")
            control.write_bytes(layer_tar({"control": "".join(
                key + ": " + value + "\n" for key, value in fields.items()).encode()}))
            key = "/trixie/" + name + ":" + architecture + "=1.0"
            packages[key] = {
                "name": name, "version": "1.0", "architecture": architecture,
                "sha256": hashlib.sha256(name.encode()).hexdigest(), "depends_on": [],
                "payload_sha256": digest(path.read_bytes()).removeprefix("sha256:"),
                "payload_size": path.stat().st_size,
                "control_sha256": digest(control.read_bytes()).removeprefix("sha256:"),
                "control_size": control.stat().st_size,
            }
            roots[key.rsplit("=", 1)[0]] = "1.0"
            mapping.append({"key": key, "payload": str(path), "control": str(control)})
            self.paths[name] = path
        self.lock = self.root / "lock.json"
        self.lock.write_text(json.dumps({"version": 2, "packages": packages,
            "dependency_sets": {group: {"sets": {architecture: roots}}
                                for group in ("runtime", "debug")}}))
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({"architecture": architecture, "locked": mapping}))
        self.policy = self.root / "policy.json"
        self.policy.write_text(json.dumps({"image": image, "architecture": architecture,
                                          "retained_packages": self.retained}))

    def write_base(self):
        config = {"architecture": self.architecture, "os": "linux", "rootfs": {
            "type": "layers", "diff_ids": [digest(layer) for layer in self.layers]}}
        files = oci_files(json.dumps(config).encode(), self.layers)
        self.manifest_digest = json.loads(files["index.json"])["manifests"][0]["digest"]
        write_layout(self.base, files)

    def select(self, **options):
        return subject.select(self.base, self.lock, self.mapping,
                              variant=options.pop("variant", "runtime"),
                              architecture=options.pop("architecture", self.architecture),
                              retained_packages=self.retained, **options)

    def replace_payload(self, entries):
        path = self.paths[self.addition]
        path.write_bytes(layer_tar(entries))
        lock = json.loads(self.lock.read_bytes())
        package = next(item for item in lock["packages"].values() if item["name"] == self.addition)
        package.update(payload_sha256=digest(path.read_bytes()).removeprefix("sha256:"),
                       payload_size=path.stat().st_size)
        self.lock.write_text(json.dumps(lock))


class AptSelectionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-apt-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_owner_state_and_layer_order_are_used_for_both_architectures(self):
        """Use actual retained versions and final OCI contents without an image or AMD64 default."""
        for image, architecture in (("telemetry", "amd64"), ("time-service", "arm64")):
            with self.subTest(image=image, architecture=architecture):
                fixture = Fixture(self.root, image, architecture)
                paths, receipt = fixture.select()
                self.assertEqual(paths, [fixture.paths[fixture.addition]])
                self.assertEqual(receipt["base_manifest_digest"], fixture.manifest_digest)
                self.assertEqual(receipt["skipped_base"][0]["base_version"], "3.0")
                self.assertEqual(receipt["skipped_retained"][0]["source_sha256"], "a" * 64)
                self.assertEqual(receipt["changed_non_elf_base_paths"], [])
                self.assertEqual(receipt["dependency_check"]["status"], "satisfied")
                records = receipt["dependency_check"]["packages"]
                self.assertEqual(set(records), {"platform-lib", "local-driver", fixture.addition})
                self.assertEqual(records["local-driver"]["Version"], "2.0")
                self.assertTrue(all(record["Architecture"] == architecture for record in records.values()))

    def test_platform_mismatch_fails_before_package_selection(self):
        """Reject a base for another architecture before handing its state to infrastructure."""
        fixture = Fixture(self.root)
        with mock.patch.object(subject.selection, "select") as select:
            with self.assertRaisesRegex(ValueError, "platform"):
                fixture.select(architecture="arm64")
        select.assert_not_called()

    def test_added_payload_uses_checked_inventory(self):
        """Allow base whiteouts but reject a reviewed package TAR that would delete inherited files."""
        fixture = Fixture(self.root)
        fixture.replace_payload({"etc/.wh.shared.conf": b""})
        with self.assertRaisesRegex(ValueError, "added OCI layer contains a whiteout"):
            fixture.select()

    def test_ordinary_selection_supports_the_older_infrastructure_api(self):
        """Do not send replacement keywords to pins that only support ordinary retention."""
        fixture = Fixture(self.root)
        metadata = fixture.root / "runtime-receipt.json"

        def older_api(lock, mapping, *, group, architecture, installed, base_files,
                      retained_packages, inspect_payload, check_overlay, base_package_metadata):
            self.assertEqual((lock, mapping, group, architecture),
                             (fixture.lock, fixture.mapping, "debug", "amd64"))
            self.assertEqual(installed["platform-lib"].version, "3.0")
            self.assertIs(retained_packages, fixture.retained)
            self.assertEqual(base_package_metadata, metadata)
            entries = inspect_payload(fixture.paths[fixture.addition])
            self.assertEqual(entries["etc/shared.conf"], base_files["etc/shared.conf"])
            check_overlay(entries, base_files)
            return [fixture.paths[fixture.addition]], {"owner_evidence": "preserved"}

        with mock.patch.object(subject.selection, "select", side_effect=older_api) as select:
            _, receipt = fixture.select(variant="debug", base_package_metadata=metadata)
        select.assert_called_once()
        self.assertEqual(receipt, {"owner_evidence": "preserved",
                                  "base_manifest_digest": fixture.manifest_digest})

    def test_explicit_replacement_authorization_reaches_infrastructure_unchanged(self):
        """Preserve both empty and populated owner authorizations for newer infrastructure pins."""
        fixture = Fixture(self.root)
        for replacements in ({}, {"local-driver": fixture.retained["local-driver"]["control"]}):
            with self.subTest(replacements=replacements):
                with mock.patch.object(subject.selection, "select", return_value=([], {})) as select:
                    fixture.select(retained_replacements=replacements)
                self.assertIs(select.call_args.kwargs["retained_replacements"], replacements)

    def cli_arguments(self, fixture, *, manifest_flag="--retained-manifest", variant="runtime",
                      metadata=None):
        output, receipt = fixture.root / "selected", fixture.root / "receipts" / "selection.json"
        arguments = ["selector", "--base", str(fixture.base), "--lock", str(fixture.lock),
                     manifest_flag, str(fixture.policy), "--mapping", str(fixture.mapping),
                     "--variant", variant, "--out-dir", str(output), "--receipt", str(receipt)]
        if metadata is not None:
            arguments += ["--base-package-metadata", str(metadata)]
        return arguments, output, receipt

    def test_cli_stages_owner_selected_payloads_and_inherited_metadata(self):
        """Keep both manifest spellings and owner receipts working through the real staging path."""
        def owner_policy(base, lock, policy, mapping, *, variant, base_package_metadata):
            document = json.loads(policy.read_bytes())
            paths, receipt = subject.select(
                base, lock, mapping, variant=variant, architecture=document["architecture"],
                retained_packages=document["retained_packages"], base_package_metadata=base_package_metadata)
            receipt["image"] = document["image"]
            return paths, receipt

        cases = (("telemetry", "amd64", "runtime", "--retained-manifest"),
                 ("time-service", "arm64", "debug", "--make-manifest"))
        for image, architecture, variant, flag in cases:
            with self.subTest(image=image, variant=variant):
                fixture = Fixture(self.root, image, architecture)
                metadata = None
                if variant == "debug":
                    _, inherited = fixture.select()
                    metadata = fixture.root / "runtime-receipt.json"
                    metadata.write_text(json.dumps(inherited))
                    fixture.layers.append(fixture.paths[fixture.addition].read_bytes())
                    fixture.write_base()
                arguments, output, receipt = self.cli_arguments(
                    fixture, manifest_flag=flag, variant=variant, metadata=metadata)
                adapter = mock.Mock(wraps=owner_policy)
                with mock.patch.object(sys, "argv", arguments):
                    subject.main(adapter, description="Example owner policy", error_prefix=image)
                adapter.assert_called_once_with(
                    fixture.base, fixture.lock, fixture.policy, fixture.mapping,
                    variant=variant, base_package_metadata=metadata)
                document = json.loads(receipt.read_bytes())
                self.assertEqual(document["image"], image)
                self.assertEqual(document["group"], variant)
                self.assertEqual(document["base_manifest_digest"], fixture.manifest_digest)
                self.assertEqual(document["dependency_check"]["package_count"], 3)
                expected = [] if variant == "debug" else ["000001.tar"]
                self.assertEqual(sorted(path.name for path in output.iterdir()),
                                 ["000000-empty.tar", *expected])
                with tarfile.open(output / "000000-empty.tar") as archive:
                    self.assertEqual(archive.getnames(), [])
                if expected:
                    self.assertEqual((output / expected[0]).read_bytes(),
                                     fixture.paths[fixture.addition].read_bytes())

    def test_cli_policy_failure_does_not_publish_outputs(self):
        """Keep an owner's rejected policy from creating a success receipt or staged archive."""
        fixture = Fixture(self.root)
        arguments, output, receipt = self.cli_arguments(fixture)
        diagnostic = io.StringIO()
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(diagnostic):
            with self.assertRaises(SystemExit) as failure:
                subject.main(mock.Mock(side_effect=ValueError("policy rejected")),
                             description="Example owner policy", error_prefix="time-service APT")
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(diagnostic.getvalue(), "time-service APT: policy rejected\n")
        self.assertFalse(output.exists())
        self.assertFalse(receipt.exists())

    def test_cli_staging_failure_preserves_the_previous_receipt(self):
        """Do not replace prior success evidence when the declared staging directory is occupied."""
        fixture = Fixture(self.root)
        arguments, output, receipt = self.cli_arguments(fixture)
        output.mkdir()
        sentinel = output / "existing.tar"
        sentinel.write_bytes(b"previous output")
        receipt.parent.mkdir()
        receipt.write_bytes(b"previous receipt\n")
        adapter = mock.Mock(return_value=([fixture.paths[fixture.addition]], {"new": "receipt"}))
        diagnostic = io.StringIO()
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(diagnostic):
            with self.assertRaises(SystemExit) as failure:
                subject.main(adapter, description="Example owner policy", error_prefix="telemetry APT")
        self.assertEqual(failure.exception.code, 1)
        self.assertIn("telemetry APT: selected archive directory must be empty", diagnostic.getvalue())
        self.assertEqual(receipt.read_bytes(), b"previous receipt\n")
        self.assertEqual(sentinel.read_bytes(), b"previous output")
        self.assertEqual(list(output.iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
