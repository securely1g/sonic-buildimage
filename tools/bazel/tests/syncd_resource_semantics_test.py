"""Resource-only public fixtures; no worker measurements, network, builds or DEBs."""
import copy
import unittest
from unittest import mock

from tools.bazel.ci import source_worker_preflight, syncd_receipt_semantics


class SyncdResourceSemanticsTest(unittest.TestCase):
    # Literal public fields from original Syncd worker-preflight.json, SHA
    # bd7ba9269418220a4c0bea652fd1fce8df74983353ce7eba2bddb90dce1baf7d.
    PROFILE = {'name': 'two_actions', 'status': 'pilot', 'minimum_memory_bytes': 21474836480, 'minimum_cpu': 4, 'measured_memory_limit_bytes': 21474836480, 'measured_cpu_limit': 6.0, 'jvm_heap_gib': 3, 'jobs': 2, 'cpu': 2, 'scheduler_memory_mb': 8000, 'loaded_container_memory_gib': 4}
    FACTS = {'cpu_max': '600000 100000', 'cpu_limit': 6.0, 'memory_limit_bytes': 21474836480}

    def fixture(self):
        return copy.deepcopy(self.PROFILE), copy.deepcopy(self.FACTS)

    def test_genuine_six_cpu_profile_uses_existing_owner_policy(self):
        profile, facts = self.fixture()
        with mock.patch.object(source_worker_preflight, 'cpu_limit', wraps=source_worker_preflight.cpu_limit) as limit:
            with mock.patch.object(source_worker_preflight, 'select_build_profile', wraps=source_worker_preflight.select_build_profile) as select:
                syncd_receipt_semantics.verify_worker_resources(profile, facts)
        limit.assert_called_once_with('600000 100000')
        select.assert_called_once_with(20 * 1024**3, 6.0)

    def test_four_cpu_profile_remains_valid(self):
        profile, facts = self.fixture()
        facts.update(cpu_max='400000 100000', cpu_limit=4.0)
        profile['measured_cpu_limit'] = 4.0
        syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_two_action_minimum_is_enforced(self):
        for cpu, quota in ((2.0, '200000 100000'), (3.99, '399000 100000')):
            with self.subTest(cpu=cpu):
                profile, facts = self.fixture()
                facts.update(cpu_max=quota, cpu_limit=cpu)
                profile['measured_cpu_limit'] = cpu
                with self.assertRaises((AssertionError, ValueError)):
                    syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_existing_six_cpu_upper_bound_is_preserved(self):
        profile, facts = self.fixture()
        facts.update(cpu_max='700000 100000', cpu_limit=7.0)
        profile['measured_cpu_limit'] = 7.0
        with self.assertRaises(ValueError):
            syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_nonfinite_and_non_numeric_measurements_are_refused(self):
        for cpu in (float('nan'), float('inf'), float('-inf'), True, '6', None):
            with self.subTest(kind=type(cpu).__name__):
                profile, facts = self.fixture()
                facts['cpu_limit'] = cpu
                with self.assertRaises(AssertionError):
                    syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_unbounded_invalid_or_oversized_cgroup_quota_is_refused(self):
        for quota in ('max 100000', '0 100000', '-1 100000', '600000 0', '600000', '9' * 129, None):
            with self.subTest(kind=type(quota).__name__):
                profile, facts = self.fixture()
                facts['cpu_max'] = quota
                with self.assertRaises((AssertionError, ValueError)):
                    syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_recorded_measurement_must_match_original_cgroup_quota(self):
        profile, facts = self.fixture()
        facts['cpu_limit'] = 4.0
        profile['measured_cpu_limit'] = 4.0
        with self.assertRaises(AssertionError):
            syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_profile_measurement_must_match_preflight(self):
        profile, facts = self.fixture()
        profile['measured_cpu_limit'] = 4.0
        with self.assertRaises(AssertionError):
            syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_minimum_and_scheduling_profile_cannot_be_relabelled(self):
        for key, value in (('minimum_cpu', 2), ('jobs', 1), ('jvm_heap_gib', 4), ('scheduler_memory_mb', 4000)):
            with self.subTest(field=key):
                profile, facts = self.fixture()
                profile[key] = value
                with self.assertRaises(AssertionError):
                    syncd_receipt_semantics.verify_worker_resources(profile, facts)

    def test_existing_memory_limit_and_measurement_are_preserved(self):
        for memory in (19 * 1024**3, 21 * 1024**3, True):
            with self.subTest(memory=memory):
                profile, facts = self.fixture()
                facts['memory_limit_bytes'] = memory
                profile['measured_memory_limit_bytes'] = memory
                with self.assertRaises((AssertionError, ValueError)):
                    syncd_receipt_semantics.verify_worker_resources(profile, facts)


if __name__ == '__main__':
    unittest.main()
