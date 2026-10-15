"""Keep service-image APT checks tied to every exact AMD64 runtime/debug archive."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import validate_syncd_image as validation


class ServiceDpkgValidationTest(unittest.TestCase):
    def run_images(self, fail_first=False):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            paths = {}
            for prefix in ("", "orchagent_"):
                for variant in ("runtime", "debug"):
                    paths[prefix + variant] = output / (prefix + variant)
                    paths[prefix + variant + "_archive"] = output / (prefix + variant + ".gz")
                    paths[prefix + variant + "_archive"].write_bytes((prefix + variant).encode())
            calls = []

            def probe(**kwargs):
                calls.append(kwargs)
                if fail_first and len(calls) == 1:
                    raise ValueError("incomplete package state")
                kwargs["output"].mkdir()
                (kwargs["output"] / "receipt.json").write_text('{"status":"passed"}\n')
                return {"status": "passed"}

            layout = SimpleNamespace(manifest={"config": {"digest": "sha256:" + "a" * 64}},
                                     descriptor={"digest": "sha256:" + "b" * 64})
            with mock.patch.object(validation, "validate_layout", return_value=layout) as inspect, \
                    mock.patch.object(validation, "validate_dpkg_state", side_effect=probe):
                if fail_first:
                    with self.assertRaisesRegex(ValueError, "transactions failed"):
                        validation.validate_dpkg_images(paths, output)
                else:
                    validation.validate_dpkg_images(paths, output)
                self.assertEqual(len(calls), 4)
                self.assertTrue(all(call.args[1] == "linux/amd64" for call in inspect.call_args_list))
                self.assertTrue(all(call["architecture"] == "amd64" for call in calls))
                self.assertEqual([call["archive"] for call in calls],
                                 [paths[name + "_archive"] for name in ("runtime", "debug", "orchagent_runtime", "orchagent_debug")])
                for call in calls:
                    self.assertEqual(call["archive_sha256"], validation.sha(call["archive"]))
                    self.assertEqual(call["config_digest"], "sha256:" + "a" * 64)
                    self.assertEqual(call["upgrade_packages"], ["socat"])
                self.assertEqual(calls[0]["installed_receipts"], ["/usr/share/sonic/bazel/syncd-dpkg-state.json"])
                self.assertEqual(calls[3]["installed_receipts"], ["/usr/share/sonic/bazel/orchagent-debug-dpkg-state.json"])
                self.assertNotIn("tcpdump", calls[0]["remove_packages"])  # P4C depends on it.
                self.assertIn("tcpdump", calls[2]["remove_packages"])
                self.assertIn("vim", calls[1]["remove_packages"])
                self.assertNotIn("vim", calls[3]["remove_packages"])  # Orchagent imports no full Vim.
                self.assertIn("vim-tiny", calls[3]["remove_packages"])
                result = json.loads((output / "dpkg-images.json").read_text())
                self.assertEqual(result["architecture"], "amd64")
                return result

    def test_each_runtime_and_debug_archive_has_independent_transaction_checks(self):
        result = self.run_images()
        self.assertEqual({row["status"] for row in result["images"].values()}, {"passed"})

    def test_failed_first_image_does_not_hide_later_image_results(self):
        result = self.run_images(fail_first=True)
        self.assertEqual(result["images"]["syncd-runtime"]["status"], "failed")
        self.assertEqual(result["images"]["orchagent-debug"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
