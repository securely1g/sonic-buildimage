#!/usr/bin/env python3
"""Offline byte/identity and failure checks for P4 CI source transport."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("p4_inputs", Path(__file__).resolve().parents[1] / "p4_source_inputs.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def hashes(data):
    return {"bytes": len(data), **{name: hashlib.new(name, data).hexdigest() for name in helper.ALGORITHMS}}


class SourceInputsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "sources"
        self.directory.mkdir()
        self.manifest_path = self.root / "manifest.json"
        self.receipt = self.root / "receipt.json"
        self.manifest = {"schema": 1, "base_url": helper.BASE_URL, "packages": {}, "known_inconsistencies": []}
        for package, version in helper.VERSIONS.items():
            upstream = version.rsplit("-", 1)[0]
            orig = f"{package}_{upstream}.orig.tar.gz"
            debian = f"{package}_{version}.debian.tar.xz"
            dsc = f"{package}_{version}.dsc"
            actual = {}
            for name in [orig, debian]:
                data = ("published bytes for " + name).encode()
                (self.directory / name).write_bytes(data)
                actual[name] = hashes(data)
            declared = copy.deepcopy(actual)
            if package == "p4lang-p4c":
                declared[orig] = hashes(b"different archive declared by this exact DSC")
            text = f"Format: 3.0 (quilt)\nSource: {package}\nVersion: {version}\n"
            for header, algorithm in [("Files", "md5"), ("Checksums-Sha1", "sha1"), ("Checksums-Sha256", "sha256")]:
                text += header + ":\n"
                for name in [orig, debian]:
                    value = declared[name]
                    text += f" {value[algorithm]} {value['bytes']} {name}\n"
            (self.directory / dsc).write_text(text)
            actual[dsc] = hashes(text.encode())
            self.manifest["packages"][package] = {"version": version, "upstream_version": upstream,
                "dsc_consistent": package != "p4lang-p4c",
                "files": {name: {k: value[k] for k in ["bytes", "sha256"]} for name, value in actual.items()}}
            if package == "p4lang-p4c":
                self.manifest["known_inconsistencies"] = [{"package": package, "descriptor": dsc,
                    "descriptor_sha256": actual[dsc]["sha256"], "archive": orig,
                    "declared": declared[orig], "published": actual[orig], "explanation": "Synthetic exact mismatch"}]
        self.save_manifest()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def execute(self, expected):
        with patch.object(helper.subprocess, "run") as network:
            self.assertEqual(helper.run(self.manifest_path, self.directory, self.receipt), expected)
            network.assert_not_called()
        return json.loads(self.receipt.read_text())

    def update_dsc(self, package, transform, update_known_binding=False):
        p = self.manifest["packages"][package]
        name = f"{package}_{p['version']}.dsc"
        path = self.directory / name
        path.write_text(transform(path.read_text()))
        value = hashes(path.read_bytes())
        p["files"][name] = {k: value[k] for k in ["bytes", "sha256"]}
        if update_known_binding:
            self.manifest["known_inconsistencies"][0]["descriptor_sha256"] = value["sha256"]
        self.save_manifest()

    def test_exact_pins_pass_and_report_known_mismatch_without_repair(self):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        receipt = self.execute(0)
        self.assertEqual(receipt["status"], "verified_published_inputs")
        self.assertTrue(receipt["all_published_pins_match"])
        self.assertFalse(receipt["all_dsc_checksums_match"])
        self.assertFalse(receipt["descriptor_modified"])
        self.assertFalse(receipt["packages"]["p4lang-p4c"]["dsc_consistent"])
        self.assertEqual(len(receipt["known_inconsistencies"]), 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})

    def test_same_size_payload_mutation_fails(self):
        p = self.directory / "p4lang-pi_0.1.3.orig.tar.gz"
        data = p.read_bytes(); p.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        self.assertIn("input_sha256_mismatch", self.execute(1)["error"])

    def test_missing_and_extra_files_fail(self):
        (self.directory / "unexpected").write_bytes(b"stale")
        self.assertEqual(self.execute(1)["error"], "staged_file_inventory")
        self.receipt.unlink(); (self.directory / "unexpected").unlink()
        (self.directory / "p4lang-pi_0.1.3.orig.tar.gz").unlink()
        self.assertEqual(self.execute(1)["error"], "staged_file_inventory")

    def test_symlink_payload_refused(self):
        path = self.directory / "p4lang-pi_0.1.3.orig.tar.gz"
        target = self.root / "elsewhere"; path.rename(target); path.symlink_to(target)
        self.assertEqual(self.execute(1)["status"], "failed")

    def test_manifest_version_and_origin_cannot_drift(self):
        self.manifest["packages"]["p4lang-pi"]["version"] = "0.1.3-3"; self.save_manifest()
        self.assertEqual(self.execute(1)["error"], "manifest_source_version")
        self.receipt.unlink(); self.manifest["packages"]["p4lang-pi"]["version"] = "0.1.3-2"
        self.manifest["base_url"] = "http://downloadcontent.opensuse.org/"; self.save_manifest()
        self.assertEqual(self.execute(1)["error"], "manifest_schema_or_origin")

    def test_manifest_basename_and_traversal_refused(self):
        files = self.manifest["packages"]["p4lang-pi"]["files"]
        pin = files.pop("p4lang-pi_0.1.3.orig.tar.gz")
        for name in ["../outside", "/absolute", "wrong.tar.gz"]:
            files[name] = pin; self.save_manifest()
            self.assertEqual(self.execute(1)["error"], "manifest_basename_inventory")
            self.receipt.unlink(); del files[name]

    def test_descriptor_identity_checked_after_hash_pin(self):
        self.update_dsc("p4lang-pi", lambda text: text.replace("Version: 0.1.3-2", "Version: 0.1.3-3"))
        self.assertEqual(self.execute(1)["error"], "descriptor_source_identity")

    def test_unexpected_descriptor_discrepancy_refused_even_if_hash_pin_updated(self):
        value = self.manifest["packages"]["p4lang-bmv2"]["files"]["p4lang-bmv2_1.15.0.orig.tar.gz"]["sha256"]
        self.update_dsc("p4lang-bmv2", lambda text: text.replace(value, "0" * 64))
        self.manifest["packages"]["p4lang-bmv2"]["dsc_consistent"] = False; self.save_manifest()
        self.assertEqual(self.execute(1)["error"], "unexpected_descriptor_inconsistency")

    def test_known_mismatch_bound_to_descriptor_and_declared_fingerprints(self):
        value = self.manifest["known_inconsistencies"][0]["declared"]["sha256"]
        self.update_dsc("p4lang-p4c", lambda text: text.replace(value, "0" * 64))
        self.assertEqual(self.execute(1)["error"], "known_descriptor_binding")
        self.receipt.unlink()
        self.update_dsc("p4lang-p4c", lambda text: text, update_known_binding=True)
        self.assertEqual(self.execute(1)["error"], "unexpected_descriptor_inconsistency")

    def test_no_generic_descriptor_ignore(self):
        self.manifest["known_inconsistencies"].append(copy.deepcopy(self.manifest["known_inconsistencies"][0])); self.save_manifest()
        self.assertEqual(self.execute(1)["error"], "known_inconsistency_inventory")

    def test_descriptor_duplicate_archive_and_traversal_refused(self):
        for replacement in ["../outside", "p4lang-pi_0.1.3-2.debian.tar.xz"]:
            original = (self.directory / "p4lang-pi_0.1.3-2.dsc").read_text()
            with self.assertRaises(helper.InputError):
                helper.descriptor_contract(original.replace("p4lang-pi_0.1.3.orig.tar.gz", replacement).encode(),
                    "p4lang-pi", "0.1.3-2", {"p4lang-pi_0.1.3.orig.tar.gz", "p4lang-pi_0.1.3-2.debian.tar.xz"})

    def test_duplicate_json_and_receipt_overwrite_refused(self):
        self.manifest_path.write_text(self.manifest_path.read_text().replace('"schema": 1', '"schema": 1, "schema": 1'))
        self.assertEqual(self.execute(1)["error"], "duplicate_json_key")
        before = self.receipt.read_bytes()
        with self.assertRaises(FileExistsError):
            helper.run(self.manifest_path, self.directory, self.receipt)
        self.assertEqual(before, self.receipt.read_bytes())

    def test_fetch_uses_same_verifier_and_https_without_credentials(self):
        destination = self.root / "fresh"
        def curl(command, **kwargs):
            self.assertEqual(command[:2], ["/usr/bin/curl", "--disable"])
            self.assertEqual(command[command.index("--proto") + 1], "=https")
            self.assertEqual(command[command.index("--proto-redir") + 1], "=https")
            self.assertLessEqual(float(command[command.index("--max-time") + 1]), 120)
            self.assertLessEqual(kwargs["timeout"], 122)
            self.assertTrue(command[-1].startswith(helper.BASE_URL))
            name = command[-1].removeprefix(helper.BASE_URL)
            data = (self.directory / name).read_bytes()
            Path(command[command.index("--output") + 1]).write_bytes(data)
            return subprocess.CompletedProcess(command, 0, f"200 {len(data)} 0.1 0".encode(), b"")
        with patch.object(helper.subprocess, "run", side_effect=curl) as network:
            self.assertEqual(helper.run(self.manifest_path, destination, self.receipt, True), 0)
            self.assertEqual(network.call_count, 9)
        self.assertEqual({p.name for p in destination.iterdir()}, {p.name for p in self.directory.iterdir()})

    def test_failed_fetch_has_bounded_attempts_and_sanitized_receipt(self):
        failure = subprocess.CompletedProcess([], 28, b"000 0 15 0", b"private-proxy-secret")
        with patch.object(helper.subprocess, "run", return_value=failure) as network, patch.object(helper.time, "sleep"):
            self.assertEqual(helper.run(self.manifest_path, self.root / "fresh", self.receipt, True), 1)
            self.assertEqual(network.call_count, 3)
        self.assertNotIn("private-proxy-secret", self.receipt.read_text())
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_transport_fallback_preserves_pins_and_records_actual_sources(self):
        def curl(command, **kwargs):
            url = command[-1]
            self.assertNotIn('--insecure', command)
            self.assertEqual(command[command.index('--proto-redir') + 1], '=https')
            if url.startswith(helper.BASE_URL):
                return subprocess.CompletedProcess(command, 35, b'000 0 0.1 0', b'TLS unavailable')
            self.assertTrue(url.startswith(helper.ARCHIVE_MIRROR))
            name = url.removeprefix(helper.ARCHIVE_MIRROR)
            self.assertFalse(name.endswith('.dsc'))
            data = (self.directory / name).read_bytes()
            Path(command[command.index('--output') + 1]).write_bytes(data)
            return subprocess.CompletedProcess(command, 0, f'200 {len(data)} 0.1 1'.encode(), b'')
        with patch.object(helper.subprocess, 'run', side_effect=curl) as network, \
                patch.object(helper, 'DESCRIPTORS', self.directory), patch.object(helper.time, 'sleep'):
            self.assertEqual(helper.run(self.manifest_path, self.root / 'fresh', self.receipt, True), 0)
        self.assertEqual(network.call_count, 15)
        receipt = json.loads(self.receipt.read_text())
        self.assertTrue(receipt['all_published_pins_match'])
        self.assertFalse(receipt['descriptor_modified'])
        self.assertEqual(len(receipt['known_inconsistencies']), 1)
        for name, attempts in receipt['transport'].items():
            self.assertEqual(attempts[0]['source'], helper.BASE_URL + name)
            self.assertEqual(attempts[0]['source_kind'], 'canonical')
            if name.endswith('.dsc'):
                self.assertEqual(attempts[1]['source_kind'], 'tracked_descriptor')
                self.assertEqual(attempts[1]['provenance'], helper.DESCRIPTOR_PROVENANCE)
                self.assertEqual(attempts[1]['sha256'], receipt['files'][name]['sha256'])
            else:
                self.assertEqual(attempts[1]['source'], helper.ARCHIVE_MIRROR + name)

    def test_descriptor_fallback_rejects_modified_or_symlinked_fixture(self):
        name = 'p4lang-pi_0.1.3-2.dsc'
        pin = self.manifest['packages']['p4lang-pi']['files'][name]
        fixtures = self.root / 'fixtures'; fixtures.mkdir()
        original = (self.directory / name).read_bytes()
        path = fixtures / name
        failure = subprocess.CompletedProcess([], 35, b'000 0 0.1 0', b'')
        for kind in ['modified', 'symlink']:
            with self.subTest(kind=kind):
                if kind == 'modified':
                    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                else:
                    path.unlink(); path.symlink_to(self.directory / name)
                with patch.object(helper, 'DESCRIPTORS', fixtures), \
                        patch.object(helper.subprocess, 'run', return_value=failure), \
                        patch.object(helper.time, 'sleep'), self.assertRaises((helper.InputError, OSError)):
                    helper.fetch(self.root / kind, {name: pin}, {'transport': {}}, lambda: None)

    def test_nonretryable_response_and_rate_limit_do_not_select_fallback(self):
        for status, exit_code in [('404', 22), ('429', 22), ('403', 56), ('429', 28), ('000', 60)]:
            with self.subTest(status=status, exit_code=exit_code):
                response = subprocess.CompletedProcess([], exit_code, f'{status} 0 0.1 0'.encode(), b'')
                with patch.object(helper.subprocess, 'run', return_value=response) as network:
                    self.assertEqual(helper.run(self.manifest_path, self.root / (status + '-' + str(exit_code)),
                                                self.root / (status + '-' + str(exit_code) + '.json'), True), 1)
                self.assertEqual(network.call_count, 1)
                self.assertTrue(network.call_args.args[0][-1].startswith(helper.BASE_URL))

    def test_published_descriptor_fixtures_match_the_unchanged_source_manifest(self):
        manifest = json.loads((helper.DESCRIPTORS.parent / 'source-inputs.json').read_text())
        inventory = helper.manifest_contract(manifest)
        self.assertEqual({p.name for p in helper.DESCRIPTORS.glob('*.dsc')},
                         {name for name in inventory if name.endswith('.dsc')})
        for name, pin in inventory.items():
            if name.endswith('.dsc'):
                actual, _ = helper.fingerprints(helper.DESCRIPTORS / name, pin['bytes'])
                self.assertEqual(actual['sha256'], pin['sha256'])


if __name__ == "__main__":
    unittest.main()
