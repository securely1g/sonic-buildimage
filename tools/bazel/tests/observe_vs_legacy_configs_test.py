"""Fixed-roster and ambiguity checks; no packages or images are generated."""
import copy
import json
import sys
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


    def run_driver(self, effects):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); artifact = root / "artifact"; artifact.mkdir()
            rows = [{"required_name": f"docker-{i}.gz", "cache_name": f"object-{i}.tgz",
                     "cache_artifact_path": f"execution/object-{i}.tgz",
                     "cache_sha256": str(i) * 64, "member_sha256": str(i + 2) * 64}
                    for i in range(2)]
            manifest = {"rows": rows, "artifact": {"id": 123}, "tag_contract": "fixed"}
            argv = ["observe", "--manifest", str(root / "manifest.json"), "--artifact", str(artifact),
                    "--scratch", str(root / "scratch"), "--report", str(root / "report.json")]
            with patch.object(sys, "argv", argv), patch.object(subject, "load_manifest", return_value=manifest), \
                    patch.object(subject.binaries, "inspect_cached", side_effect=effects) as inspect:
                code = subject.main()
            self.assertFalse((root / "scratch").exists())
            return code, json.loads((root / "report.json").read_text()), inspect.call_count

    def test_object_failure_is_retained_while_later_object_is_observed(self):
        code, result, calls = self.run_driver([ValueError("PRIVATE_CONFIG=never-publish"), [{"name": "docker-1.gz"}]])
        self.assertEqual(code, 1); self.assertEqual(calls, 2)
        self.assertEqual(result["status"], "LEGACY_CONFIGURATION_OBSERVATION_INCOMPLETE")
        self.assertEqual(result["results"], [{"name": "docker-1.gz"}])
        self.assertEqual(len(result["errors"]), 1)
        error = result["errors"][0]
        self.assertEqual(error["name"], "docker-0.gz")
        self.assertEqual(error["error_type"], "ValueError")
        self.assertTrue(error["sites"])
        self.assertEqual(error["sites"][0]["module"], "observe_vs_legacy_configs.py")
        self.assertEqual(error["sites"][0]["function"], "main")
        self.assertNotIn("PRIVATE_CONFIG", json.dumps(result))
        self.assertFalse(result["source_admission"]); self.assertFalse(result["VS_assembly_admission"])

    def test_all_objects_success_keeps_observation_only_status(self):
        code, result, calls = self.run_driver([[{"name": "docker-0.gz"}], [{"name": "docker-1.gz"}]])
        self.assertEqual(code, 0); self.assertEqual(calls, 2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["status"], "LEGACY_CONFIGURATION_OBSERVED_NOT_ADMITTED")
        self.assertFalse(result["source_admission"]); self.assertFalse(result["VS_assembly_admission"])

    def test_whole_observation_timeout_stops_later_objects(self):
        code, result, calls = self.run_driver([TimeoutError("deadline"), [{"name": "must-not-run"}]])
        self.assertEqual(code, 1); self.assertEqual(calls, 1)
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertEqual(result["failed_object"], "object-0.tgz")
        self.assertEqual(result["errors"], []); self.assertEqual(result["results"], [])



if __name__ == "__main__":
    unittest.main()
