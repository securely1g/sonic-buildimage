"""Exercise the audit/default boundary with unchanged tiny original fixtures."""
import copy
import unittest
from unittest import mock
from tools.bazel.ci import observe_vs_original_installed_policy as subject
from tools.bazel.ci import observe_vs_original_installed_policy_test as support


class AuditModeTests(unittest.TestCase):
    setUp = support.ArchiveInspectionTests.setUp

    def test_audit_uses_original_label_and_selected_inventory_without_make_claim(self):
        callback = mock.Mock(return_value=({'selected-layer-only': {}}, {}))
        result = subject.inspect_archive(self.row, self.root, self.scratch, None,
            self.proof, self.owners, audit_only=True, inventory_reader=callback)
        callback.assert_called_once()
        self.checks.image_inventory.assert_not_called()
        self.assertTrue(result['audit_only'])
        self.assertFalse(result['Make_manifest_equivalence_claimed'])
        self.assertEqual(result['snapshot']['config_digest'], self.proof['config_digest'])
        self.assertEqual(result['files'], {'selected-layer-only': {}})

    def test_wrong_config_digest_stops_before_label_snapshot_and_callback(self):
        proof = copy.deepcopy(self.proof)
        proof['config_digest'] = 'sha256:'+'0'*64
        callback = mock.Mock()
        with mock.patch.object(self.owners[1], 'archive_snapshot') as snapshot:
            with self.assertRaisesRegex(ValueError, 'before reading its label'):
                subject.inspect_archive(self.row, self.root, self.scratch, None,
                    proof, self.owners, audit_only=True, inventory_reader=callback)
            snapshot.assert_not_called()
        callback.assert_not_called()

    def test_default_mode_rejects_alternate_inventory_before_reading(self):
        with self.assertRaisesRegex(ValueError, 'Alternate inventory is audit-only'):
            subject.inspect_archive(self.row, self.root, self.scratch, self.manifest,
                self.proof, self.owners, inventory_reader=mock.Mock())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_audit_mode_rejects_supplied_make_manifest_before_reading(self):
        with self.assertRaisesRegex(ValueError, 'does not accept a Make manifest'):
            subject.inspect_archive(self.row, self.root, self.scratch, self.manifest,
                self.proof, self.owners, audit_only=True)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_audit_observation_cannot_produce_installed_policy(self):
        checks = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'cannot produce installed policy'):
            subject.policy_from_observations([], {'image': {'audit_only': True}}, checks)
        self.assertEqual(checks.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
