"""The import verification must reject actions that could create new packages."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
