"""Pure prerequisite-policy tests; no runner probe or package operation."""
import unittest
from unittest import mock
from tools.bazel.ci import vs_worker_inspection as inspection


def observation():
    measured = lambda value: {"observed": True, "value": value}
    worker = "ks-fixture"
    return {"schema": 1, "status": "OBSERVATION_NOT_QUALIFICATION", "pool": inspection.POOL,
            "worker": worker, "build_or_package_generation": False, "host_kernel_configuration_changed": False,
            "resources": {"schema": 1, "scope": "resource_and_namespace_preflight_only",
                "status": "passed", "checks": dict.fromkeys(inspection.resources.required_checks(inspection.POOL), True),
                "image_build_executed": False, "deb_generation_executed": False,
                "facts": {"memory_limit_bytes": 20 * 1024**3, "cpu_limit": 6,
                          "worker": worker, "pool": inspection.POOL,
                          "workspace_storage": {"free_bytes": 100 * 1024**3},
                          "cache_root": {"namespace": "actions_work_source_images",
                              "worker_directory": "source-images/" + worker, "backing_mount": "actions",
                              "shared_with_workspace": True, "lifetime": "current_worker_mount",
                              "cross_pod_reuse": "not_established", "uid": 1000, "gid": 1000,
                              "mode": "0o700", "writable": True, "write_probe_closed": True,
                              "work_root_uid": 1000, "work_root_gid": 1000, "work_root_mode": "0o755"},
                          "cache_storage": {"filesystem": "ext4", "free_bytes": 100 * 1024**3,
                              "backing_mount": "actions", "shared_with_workspace": True}}},
            "vs_observations": {"tools": dict.fromkeys(inspection.TOOLS, True),
                "docker_group": measured(True), "cgroup_type": measured("domain"),
                "cgroup_delegation_writable": measured(True), "sonic_directory_empty": measured(True),
                "private_docker_routes_clear": measured(True), "sudo_available": measured(True),
                "docker_endpoint_absent": True, "private_docker_state_absent": True,
                "marker": measured({"manager_revision": "a" * 40}),
                "cgroup_controllers": measured(["cpu", "memory", "pids"])}}


class InspectionPolicyTest(unittest.TestCase):
    def test_matching_observation_is_not_daemon_qualification(self):
        value = inspection.check_reviewed_requirements(observation(), "a" * 40)
        self.assertEqual(value["status"], "MEASURED_PREREQUISITES_MATCH")
        self.assertIs(value["private_daemon_qualified"], False)

    def test_unreviewed_manager_cannot_be_inferred_from_observation(self):
        for expected in (None, "", "b" * 40):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                inspection.check_reviewed_requirements(observation(), expected)

    def test_failed_resources_or_altered_allocation_stop(self):
        for key, value in (("cpu_limit", 4), ("memory_limit_bytes", 21 * 1024**3),
                           ("workspace_storage", {"free_bytes": 89 * 1024**3})):
            row = observation()
            row["resources"]["facts"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                inspection.check_reviewed_requirements(row, "a" * 40)
        row = observation()
        row["resources"]["checks"]["user_mount_namespace"] = False
        with self.assertRaises(ValueError):
            inspection.check_reviewed_requirements(row, "a" * 40)

    def test_each_missing_canonical_resource_check_stops(self):
        self.assertEqual(len(inspection.resources.required_checks(inspection.POOL)), 28)
        for name in inspection.resources.required_checks(inspection.POOL):
            row = observation()
            del row["resources"]["checks"][name]
            with self.subTest(check=name), self.assertRaises(ValueError):
                inspection.check_reviewed_requirements(row, "a" * 40)

    def test_wrong_worker_pool_schema_or_execution_flags_stop(self):
        cases = (((), "worker", "ks-other"), ((), "pool", "sonic-slave-container-dpool"),
                 ((), "schema", True), ((), "build_or_package_generation", True),
                 ((), "host_kernel_configuration_changed", True),
                 (("resources",), "schema", True), (("resources",), "scope", "other"),
                 (("resources",), "image_build_executed", True),
                 (("resources",), "deb_generation_executed", True),
                 (("resources", "facts"), "worker", "ks-other"),
                 (("resources", "facts"), "pool", "sonic-slave-container-dpool"))
        for path, key, value in cases:
            row = observation()
            target = row
            for part in path:
                target = target[part]
            target[key] = value
            with self.subTest(path=path, key=key), self.assertRaises((ValueError, RuntimeError)):
                inspection.check_reviewed_requirements(row, "a" * 40)

    def test_each_missing_tool_and_isolation_gate_stops(self):
        for name in inspection.TOOLS:
            row = observation()
            row["vs_observations"]["tools"][name] = False
            with self.subTest(tool=name), self.assertRaises(ValueError):
                inspection.check_reviewed_requirements(row, "a" * 40)
        for name in ("docker_group", "cgroup_type", "cgroup_delegation_writable",
                     "sonic_directory_empty", "private_docker_routes_clear", "sudo_available", "marker"):
            row = observation()
            row["vs_observations"][name] = {"observed": False, "error_type": "OSError"}
            with self.subTest(gate=name), self.assertRaises(ValueError):
                inspection.check_reviewed_requirements(row, "a" * 40)

    def test_existing_daemon_and_missing_controllers_stop(self):
        for name in ("docker_endpoint_absent", "private_docker_state_absent"):
            row = observation()
            row["vs_observations"][name] = False
            with self.subTest(gate=name), self.assertRaises(ValueError):
                inspection.check_reviewed_requirements(row, "a" * 40)
        row = observation()
        row["vs_observations"]["cgroup_controllers"]["value"] = ["cpu", "memory"]
        with self.assertRaises(ValueError):
            inspection.check_reviewed_requirements(row, "a" * 40)

    def test_observation_error_does_not_leak_exception_text(self):
        value = inspection.observation(mock.Mock(side_effect=ValueError("private-host-path")))
        self.assertEqual(value, {"observed": False, "error_type": "ValueError"})

    def test_routes_are_checked_without_exporting_network_details(self):
        with mock.patch.object(inspection, "command", return_value='[{"dst":"192.168.240.0/24"}]'):
            self.assertFalse(inspection.routes_clear())
        with mock.patch.object(inspection, "command", return_value='[{"dst":"default"},{"dst":"10.2.0.0/24"}]'):
            self.assertTrue(inspection.routes_clear())


if __name__ == "__main__":
    unittest.main()
