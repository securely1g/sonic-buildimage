"""Fixed-roster and ambiguity checks; no packages or images are generated."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bazel.ci import observe_vs_legacy_configs as subject


class LegacyConfigurationTest(unittest.TestCase):
    def test_roster_tampering_is_rejected_before_archive_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"rows": []}\n')
            with self.assertRaises(ValueError):
                subject.load_manifest(path)

    def test_dhcp_retains_both_tag_observations_without_admission(self):
        row = {"required_name": "docker-dhcp-relay.gz", "family": "docker-dhcp-relay",
               "cache_sha256": "a" * 64, "member_sha256": "b" * 64,
               "entrypoint": ["/usr/bin/docker_init.sh"],
               "expected_tags": ["docker-dhcp-relay:latest", "docker-dhcp-relay:bazel-ci"]}
        values = [{"checks": {"tag_matches": False, "image_version_matches": False}},
                  {"checks": {"tag_matches": True, "image_version_matches": False}}]
        with patch.object(subject.archives, "observe", side_effect=copy.deepcopy(values)) as inspect:
            result = subject.observe_member(Path("held-original"), row, Path("scratch"))
        self.assertEqual(result["observations"], values)
        self.assertFalse(result["source_admission"])
        self.assertFalse(result["VS_assembly_admission"])
        self.assertEqual([call.args[1] for call in inspect.call_args_list], row["expected_tags"])
        self.assertTrue(all(call.args[0] == Path("held-original") and
                            call.args[2] == row["entrypoint"] and
                            call.kwargs == {"expected_sha256": row["member_sha256"]}
                            for call in inspect.call_args_list))

    def test_inspector_failure_does_not_become_a_configuration_match(self):
        row = {"expected_tags": ["docker-nat:latest"], "entrypoint": ["/usr/local/bin/supervisord"],
               "member_sha256": "a" * 64}
        with patch.object(subject.archives, "observe", side_effect=ValueError("Invalid original archive")):
            with self.assertRaises(ValueError):
                subject.observe_member(Path("held-original"), row, Path("scratch"))


if __name__ == "__main__":
    unittest.main()
