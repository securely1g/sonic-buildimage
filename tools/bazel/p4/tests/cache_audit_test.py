"""The import verification must reject actions that could create new packages."""

import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "p4_verify_cache", Path(__file__).resolve().parents[1] / "verify_cache.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class ActionAuditTest(unittest.TestCase):
    def test_rejects_packaging_action_even_if_import_also_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "actions.json"
            graph.write_text(json.dumps({"actions": [{
                "mnemonic": "Genrule",
                "arguments": ["make", "p4lang-pi_0.1.3-2_amd64.deb"],
            }]}))
            with self.assertRaisesRegex(ValueError, "registered build actions"):
                VERIFY.assert_no_actions(graph)

    def test_accepts_import_without_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "actions.json"
            graph.write_text(json.dumps({"targets": [{"label": VERIFY.TARGET}]}))
            self.assertEqual(VERIFY.assert_no_actions(graph)["registered_actions"], 0)

    def test_fresh_checkouts_share_explicit_sources_for_import_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            override = "--override_module=example=/reviewed/source"
            args = SimpleNamespace(artifacts=artifacts, bazel="bazel", bazel_arg=[override])

            def checkout(_source, destination):
                (destination / VERIFY.LOCK).parent.mkdir(parents=True)
                (destination / VERIFY.LOCK).write_text(json.dumps({
                    "architecture": "amd64", "packages": [{"filename": "existing.deb"}]}))
                (destination / "MODULE.bazel.lock").write_text("{}")

            def stage(command, _source, _log, **_kwargs):
                self.assertIn("--bazel-arg=" + override, command)
                return {"exit_code": 0}

            def audit(command, **kwargs):
                self.assertIn(override, command)
                json.dump({"actions": []}, kwargs["stdout"])

            with patch.object(VERIFY, "checkout", side_effect=checkout), \
                    patch.object(VERIFY, "run", side_effect=stage) as imports, \
                    patch.object(VERIFY, "package_details", return_value={}), \
                    patch.object(VERIFY, "verify_sbom", return_value={}), \
                    patch.object(VERIFY.subprocess, "run", side_effect=audit) as audits:
                VERIFY.phase(args, root, "cold", root / "cache")
                VERIFY.phase(args, root, "warm", root / "cache", offline=True)
            self.assertNotIn("--bazel-arg=--repository_disable_download", imports.call_args_list[0].args[0])
            self.assertIn("--bazel-arg=--repository_disable_download", imports.call_args_list[1].args[0])
            self.assertNotIn("--repository_disable_download", audits.call_args_list[0].args[0])
            self.assertIn("--repository_disable_download", audits.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
