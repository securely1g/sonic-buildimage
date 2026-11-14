"""Keep image profiles separate while sharing provenance checks."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.bazel.ci import image_worker_receipt as receipt
from tools.bazel.ci.image_worker_pool_test import WorkerPoolReceiptTest
from tools.bazel.ci.p4rt_worker_receipt_test import metadata, request


def syncd_request():
    value = request()
    value.pop('owner')
    return {**value, 'schema': 2, 'image': 'syncd'}


def worker_job_inventory(value):
    return {'total_count': 1, 'jobs': [{'id': 201, 'run_id': 789, 'run_attempt': 1,
        'head_sha': value['controller'], 'name': 'Source image (' + receipt.image(value) + ')',
        'status': 'in_progress', 'conclusion': None, 'runner_id': 31,
        'runner_name': 'sonic-slave-lgh114-1',
        'labels': ['sonic-slave-container-dpool', 'sonic-slave-lgh114-1']}]}


def worker_environment(value):
    return {'GITHUB_RUN_ID': '789', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_ACTOR': 'securely1g', 'GITHUB_TRIGGERING_ACTOR': 'securely1g',
            'WORKER_POOL': 'sonic-slave-container-dpool', 'RUNNER_NAME': 'sonic-slave-lgh114-1'}


class ImageReceiptTest(unittest.TestCase):
    selected = 'syncd'

    def image_request(self):
        return {**syncd_request(), 'image': self.selected}

    def test_policy_keeps_original_bytes_and_allows_only_the_public_constant(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'policy.json'
            value = {'image_validation': {'environment': {'DEBIAN_FRONTEND': 'noninteractive'}}}
            content = json.dumps(value).encode()
            path.write_bytes(content)
            self.assertEqual(receipt.read_evidence(path, self.image_request()), value)
            self.assertEqual(path.read_bytes(), content)
            with self.assertRaises(ValueError):
                receipt.read_evidence(path, request())
            for environment in ({'DEBIAN_FRONTEND': 'interactive'}, {'TOKEN': 'secret'}):
                path.write_text(json.dumps({'image_validation': {'environment': environment}}))
                with self.subTest(environment=environment), self.assertRaises(ValueError):
                    receipt.read_evidence(path, self.image_request())

    def test_profiles_cannot_substitute_requests_or_artifacts(self):
        value = self.image_request()
        self.assertEqual(receipt.validate_request(value), value)
        self.assertEqual(receipt.phases(value), ('build', 'validate', 'incremental', 'loaded'))
        self.assertNotEqual(receipt.acceptance_name(value), receipt.acceptance_name(request()))
        self.assertNotEqual(receipt.origin_name(123, self.selected), receipt.origin_name(123))
        for changed in ({**value, 'image': 'p4rt'}, {**value, 'schema': 1},
                        {**value, 'image': 'unreviewed'}, {**value, 'owner': 'a' * 40},
                        {**request(), 'image': self.selected}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                receipt.validate_request(changed)
        with self.assertRaises(ValueError):
            receipt.same_checkout(value, request())
        other = {**value, 'image': 'gearbox' if self.selected == 'syncd' else 'syncd'}
        with self.assertRaises(ValueError):
            receipt.same_checkout(value, other)
        self.assertNotEqual(receipt.acceptance_name(value), receipt.acceptance_name(other))
        artifact = {'id': 7, 'name': receipt.origin_name(123), 'expired': False, 'size_in_bytes': 1024}
        with self.assertRaises(ValueError):
            receipt.original_artifact([artifact], 123, selected=self.selected)

    def test_syncd_requires_its_job_all_four_phases_and_cleanup(self):
        _, artifact, run, jobs = metadata()
        value = self.image_request()
        contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
        value['contract_sha256'] = receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode())
        run['triggering_actor'] = {'login': 'securely1g'}
        artifact['name'] = receipt.acceptance_name(value)
        jobs[0]['name'] = 'Source image (' + self.selected + ')'
        jobs[0]['steps'] = [{'name': name, 'status': 'completed', 'conclusion': 'success'}
                            for name in receipt.required_steps(value)]
        self.assertEqual(receipt.worker_metadata(value, artifact, run, jobs, contract)['id'], 201)
        for step in receipt.required_steps(value):
            altered = copy.deepcopy(jobs)
            next(row for row in altered[0]['steps'] if row['name'] == step)['conclusion'] = 'skipped'
            with self.subTest(step=step), self.assertRaises(ValueError):
                receipt.worker_metadata(value, artifact, run, altered, contract)
        jobs[0]['name'] = 'Source image (p4rt)'
        with self.assertRaises(ValueError):
            receipt.worker_metadata(value, artifact, run, jobs, contract)

    def test_syncd_checkout_binds_the_whole_merge_without_a_p4rt_gitlink(self):
        value = self.image_request()
        contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
        event = {'pull_request': {'number': 196,
                 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                 'base': {'sha': value['base'], 'repo': {'full_name': receipt.REPOSITORY}}}}
        answers = [value['merge'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge', value['tree']]
        with patch.object(receipt, 'git', side_effect=answers) as git:
            actual = receipt.checkout_request(Path('.'), contract, event=event, run=123, attempt=1)
        self.assertEqual(actual['image'], self.selected)
        self.assertNotIn('owner', actual)
        self.assertEqual(git.call_count, 3)
        self.assertEqual(actual['contract_sha256'], receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()))

    def test_worker_binds_the_selected_syncd_contract_and_rejects_profile_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = self.image_request()
            contract = {key: value[key] for key in ('image', 'controller', 'catalog')}
            value['contract_sha256'] = receipt.sha(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode())
            receipt.write(root / 'payload/request.json', value)
            receipt.write(root / ('tools/bazel/ci/' + self.selected + '_worker_contract.json'), contract)
            receipt.write(root / 'origin.json', {
                'run': {'id': 123, 'head_sha': value['head'], 'run_attempt': 1},
                'artifact': {'id': 456, 'name': receipt.origin_name(123, self.selected)}} , public=False)
            args = SimpleNamespace(directory=root, workspace=root, image=self.selected)
            environment = {'SELECTED_IMAGE': self.selected, 'SOURCE_REVISION': value['merge'],
                           'SOURCE_REGISTRY_REVISION': value['catalog'], 'GITHUB_SHA': value['controller'], **worker_environment(value)}
            pull = {'state': 'open', 'head': {'sha': value['head'], 'repo': {'full_name': receipt.REPOSITORY}},
                    'base': {'sha': value['base'], 'repo': {'full_name': receipt.REPOSITORY}}}
            with patch.dict(os.environ, environment), patch.object(receipt, 'git',
                    side_effect=[value['merge'], value['tree'], 'parent ' + value['base'] + '\nparent ' + value['head'] + '\n\nMerge']), \
                    patch.object(receipt, 'api', side_effect=[pull, worker_job_inventory(value)]):
                receipt.bind_worker(args)
            self.assertEqual(receipt.read(root / 'binding.json')['request'], value)
            with patch.dict(os.environ, {**environment, 'SELECTED_IMAGE': 'p4rt'}), \
                    self.assertRaisesRegex(ValueError, 'Dispatch image differs'):
                receipt.bind_worker(args)


class GearboxReceiptTest(ImageReceiptTest):
    selected = 'gearbox'


class RemoteOnlyReceiptTest(unittest.TestCase):
    def remote_worker(self):
        return {"schema": 2, "cache_mode": "remote_only_run_attempt",
                "remote_cache": copy.deepcopy(receipt.K3S_REMOTE_CACHE_POLICY)}

    def test_remote_variant_is_explicit_and_k3s_only(self):
        value = self.remote_worker()
        self.assertEqual(receipt.verify_worker_cache(value, "sonic-slave-container-k3s"),
                         "separate_remote_only_run_attempt")
        with self.assertRaisesRegex(ValueError, "Remote-only"):
            receipt.verify_worker_cache(value, "sonic-slave-container-dpool")

    def test_legacy_worker_and_owner_modes_are_preserved(self):
        for pool in ("sonic-slave-container-dpool", "sonic-slave-container-k3s"):
            for value in ({"schema": 1, "cache_mode": "persistent_worker_local"},
                          {"cache_mode": "persistent_worker_local"}):
                original = copy.deepcopy(value)
                self.assertEqual(receipt.verify_worker_cache(value, pool), "separate_persistent_worker_local")
                self.assertEqual(value, original)

    def test_new_remote_policy_rejects_changed_origin_helper_probe_or_local_state(self):
        for key, changed in (("endpoint", "https://different.invalid"), ("credential_helper_sha256", "0" * 64),
                             ("authenticated_probe", "failed"), ("action_disk_cache", "enabled"),
                             ("working_state", "persistent"), ("extra", "value")):
            value = self.remote_worker(); value["remote_cache"][key] = changed
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Remote-only"):
                receipt.verify_worker_cache(value, "sonic-slave-container-k3s")
        for changes in ({"schema": 3}, {"schema": True}, {"cache_mode": "persistent_worker_local"},
                        {"remote_cache": None}):
            value = {**self.remote_worker(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                receipt.verify_worker_cache(value, "sonic-slave-container-k3s")

    def test_old_schema_cannot_claim_the_new_policy_or_mode(self):
        for value in ({"schema": 1, "cache_mode": "remote_only_run_attempt"},
                      {**self.remote_worker(), "schema": 1, "cache_mode": "persistent_worker_local"}):
            with self.assertRaisesRegex(ValueError, "Historical"):
                receipt.verify_worker_cache(value, "sonic-slave-container-k3s")


class ManagedCacheReceiptTest(unittest.TestCase):
    def worker(self, endpoint="https://cache.example.invalid/", host="cache.example.invalid"):
        return {"schema": 3, "cache_mode": "remote_only_run_attempt", "remote_cache": {
            **receipt.K3S_MANAGED_REMOTE_CACHE_POLICY, "endpoint": endpoint, "host": host}}

    def test_managed_origin_is_recorded_without_a_hostname_constant(self):
        for endpoint, host in (("https://cache.example.invalid/", "cache.example.invalid"),
                               ("https://replacement.example.invalid:8443/", "replacement.example.invalid")):
            value = self.worker(endpoint, host); original = copy.deepcopy(value)
            self.assertEqual(receipt.verify_worker_cache(value, "sonic-slave-container-k3s"),
                             "separate_remote_only_run_attempt")
            self.assertEqual(value, original)
            self.assertNotIn("authenticated_probe", value["remote_cache"])

    def test_managed_variant_requires_k3s_and_cannot_claim_authentication(self):
        with self.assertRaisesRegex(ValueError, "Managed"):
            receipt.verify_worker_cache(self.worker(), "sonic-slave-container-dpool")
        value = self.worker(); value["remote_cache"]["authenticated_probe"] = "passed"
        with self.assertRaisesRegex(ValueError, "Managed"):
            receipt.verify_worker_cache(value, "sonic-slave-container-k3s")

    def test_managed_policy_refuses_changed_owner_local_cache_or_probe(self):
        for key, changed in (("provider", "legacy_environment"), ("credential_helper_path", "/different/helper.py"),
                             ("credential_helper_sha256", "0" * 64), ("access_probe", "failed"),
                             ("action_disk_cache", "enabled"), ("repository_cache", "enabled"),
                             ("working_state", "persistent")):
            value = self.worker(); value["remote_cache"][key] = changed
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Managed"):
                receipt.verify_worker_cache(value, "sonic-slave-container-k3s")

    def test_managed_origin_refuses_credentials_paths_queries_and_wrong_scope(self):
        for endpoint, host in (("http://cache.example.invalid/", "cache.example.invalid"),
                ("https://user:private@cache.example.invalid/", "cache.example.invalid"),
                ("https://cache.example.invalid/data", "cache.example.invalid"),
                ("https://cache.example.invalid/?key=private", "cache.example.invalid"),
                ("https://cache.example.invalid/#private", "cache.example.invalid"),
                ("https://cache.example.invalid/", "different.invalid"),
                ("https://cache.example.invalid:70000/", "cache.example.invalid"), (True, "cache.example.invalid"),
                ("https://cache.invalid/\n", "cache.invalid"), ("\thttps://cache.invalid/", "cache.invalid"),
                ("https://cache.invalid/\x7f", "cache.invalid"), ("https://cache.invalid/\\", "cache.invalid"),
                ("https://cache.invalid/?", "cache.invalid"), ("https://cache.invalid/#", "cache.invalid"),
                ("https://cache.invalid:/", "cache.invalid"), ("https://bad host/", "bad host"),
                ("https://bad_host/", "bad_host"), ("https://-cache.invalid/", "-cache.invalid"),
                ("https://cache-.invalid/", "cache-.invalid"), ("https://cache..invalid/", "cache..invalid"),
                ("https://cache.invalid./", "cache.invalid."), ("https://caf\u00e9.invalid/", "caf\u00e9.invalid"),
                ("https://[::1]/", "::1"), ("https://" + "x" * 64 + ".invalid/", "x" * 64 + ".invalid")):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                receipt.verify_worker_cache(self.worker(endpoint, host), "sonic-slave-container-k3s")


if __name__ == '__main__':
    unittest.main()
