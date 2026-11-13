"""Inert source-shape/redaction fixtures; never inspect the host configuration."""

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ci"))
import observe_native_apt_sources as observation

SOURCE = (b"Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\n"
          b"Components: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n")


class SourceShapeTest(unittest.TestCase):
    def test_stock_http_and_https(self):
        for data in (SOURCE, SOURCE.replace(b"http:", b"https:")):
            row = observation.describe(data)["rows"][0]
            self.assertTrue(all(row[key] for key in ("fields_exact", "types_deb_only", "keyring_exact", "suites_expected", "components_expected")))
            self.assertEqual(row["uris"][0]["host"], "deb.debian.org")

    def test_source_packages_are_identified_without_admission(self):
        data = SOURCE.replace(b"Types: deb", b"Types: deb deb-src")
        row = observation.describe(data)["rows"][0]
        self.assertTrue(row["types_deb_and_source"])
        self.assertFalse(row["types_deb_only"])
        with self.assertRaises(ValueError):
            observation.policy.official_sources(data)

    def test_other_official_security_origin_is_only_observed(self):
        value = observation.uri_shape("https://security.debian.org/debian-security")
        self.assertEqual(value["host"], "security.debian.org")
        self.assertEqual(value["path"], "/debian-security")

    def test_backports_is_distinguished_from_current_policy(self):
        row = observation.describe(SOURCE.replace(b"trixie trixie-updates", b"trixie-backports"))["rows"][0]
        self.assertFalse(row["suites_expected"])
        self.assertTrue(row["suites_with_backports_only"])

    def test_unknown_source_values_are_not_emitted(self):
        secret = b"private-canary"
        data = SOURCE.replace(b"http://deb.debian.org/debian", b"https://private-canary:private-canary@private-canary.invalid/private-canary?private-canary")
        data += b"Unknown-private-canary: private-canary\n"
        raw = json.dumps(observation.describe(data))
        self.assertNotIn(secret.decode(), raw)
        self.assertFalse(observation.describe(data)["rows"][0]["fields_exact"])

    def test_invalid_port_does_not_escape_or_emit_value(self):
        self.assertFalse(observation.uri_shape("https://deb.debian.org:private-canary/debian")["connection_fields_safe"])

    def test_duplicate_and_continuation_lines_have_fixed_code(self):
        for value in (SOURCE + b"Types: private-canary\n", SOURCE + b" private-canary\n"):
            self.assertEqual(observation.describe(value)["syntax"], "unsupported_line_or_duplicate_field")

    def test_input_and_paragraph_bounds(self):
        with self.assertRaises(ValueError):
            observation.describe(b"x" * 16385)
        self.assertEqual(observation.describe((SOURCE + b"\n") * 5)["syntax"], "too_many_paragraphs")

    def test_invalid_utf8_has_fixed_code(self):
        self.assertEqual(observation.describe(b"\xff")["syntax"], "invalid_encoding")

    def invoke(self, names, changed=False):
        data = SOURCE + b"#" + b"x" * (437 - len(SOURCE) - 2) + b"\n"
        with mock.patch.object(observation.os, "getuid", return_value=0), mock.patch.object(observation, "SOURCE_SHA", hashlib.sha256(data).hexdigest()), mock.patch.object(observation.policy, "read_public_file", return_value=(data + (b"x" if changed else b""), {"sha256": "fixture"})), mock.patch.object(observation.policy, "SOURCE_DIR") as directory:
            directory.iterdir.return_value = iter(Path(name) for name in names)
            return observation.observe()

    def test_extra_inventory_has_only_count_and_fixed_rejection(self):
        result = self.invoke(["debian.sources", "private-canary.list"])
        self.assertEqual(result["rejection_code"], "source_inventory")
        self.assertTrue(result["current_source_parser_accepts"])
        self.assertEqual(result["source_file_count"], 2)
        self.assertNotIn("private-canary", json.dumps(result))

    def test_source_identity_change_is_fatal(self):
        with self.assertRaises(ValueError):
            self.invoke(["debian.sources"], changed=True)

    def test_inventory_bound(self):
        with self.assertRaises(ValueError):
            self.invoke(["ignored-" + str(i) for i in range(65)])


if __name__ == "__main__":
    unittest.main()
