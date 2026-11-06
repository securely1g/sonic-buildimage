#!/usr/bin/env python3
"""Check that worker exports retain test coverage without private XML/log data."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import p4rt_public


class P4rtPublicTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.owner = self.directory / 'owner-output'
        self.owner.mkdir()
        self.targets = ['//p4rt_app:runtime_package_test', '//p4rt_app/sonic:packetio_impl_test']
        self.revision = 'a' * 40
        self.source = {'commit': self.revision, 'bazel': '6.4.0', 'architecture': 'amd64',
                       'test_targets': self.targets, 'sdk_inventory': {},
                       'tar_sha256': {}, 'contract_sha256': {}}
        self.write(self.owner / 'source.json', self.source)
        self.write(self.directory / 'owner-tests.json', {'status': 'passed', 'owner_revision': self.revision})
        self.write(self.owner / 'test-results.json', dict.fromkeys(self.targets, 'PASSED'))
        (self.owner / 'test-targets.txt').write_text('\n'.join(self.targets) + '\n')
        self.xml = []
        for target, count in zip(self.targets, (1, 3)):
            path = self.owner / 'test-results' / target.removeprefix('//').replace(':', '/') / 'test.xml'
            path.parent.mkdir(parents=True)
            cases = ''.join(f'<testcase name="case-{i}"/>' for i in range(count))
            path.write_text(f'<testsuites><testsuite tests="{count}" failures="0" errors="0">'
                            f'{cases}<system-out>/private/worker/token</system-out></testsuite></testsuites>')
            self.xml.append(path)

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def test_complete_summary_counts_actual_cases_without_xml_contents(self):
        result = p4rt_public.owner_test_summary(self.directory)
        self.assertEqual((result['target_count'], result['testcase_count']), (2, 4))
        self.assertEqual(result['tests'][self.targets[1]]['tests'], 3)
        self.assertEqual(result['tests'][self.targets[0]]['xml_sha256'],
                         hashlib.sha256(self.xml[0].read_bytes()).hexdigest())
        self.assertNotIn('/private/', json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))

    def test_owner_revision_must_match(self):
        self.write(self.directory / 'owner-tests.json', {'status': 'passed', 'owner_revision': 'b' * 40})
        with self.assertRaisesRegex(ValueError, 'differs from the built source'):
            p4rt_public.owner_test_summary(self.directory)

    def test_bep_statuses_must_cover_exact_selection_and_pass(self):
        for statuses in ({self.targets[0]: 'PASSED'},
                         {**dict.fromkeys(self.targets, 'PASSED'), '//p4rt_app:extra': 'PASSED'},
                         {self.targets[0]: 'PASSED', self.targets[1]: 'FAILED'}):
            with self.subTest(statuses=statuses):
                self.write(self.owner / 'test-results.json', statuses)
                with self.assertRaisesRegex(ValueError, 'passing BEP status'):
                    p4rt_public.owner_test_summary(self.directory)

    def test_failed_skipped_or_incomplete_xml_cannot_produce_passed_summary(self):
        for xml in ('<testsuite tests="1"><testcase><failure/></testcase></testsuite>',
                    '<testsuite tests="1" skipped="1"><testcase><skipped/></testcase></testsuite>',
                    '<testsuite tests="2"><testcase/></testsuite>'):
            with self.subTest(xml=xml):
                self.xml[0].write_text(xml)
                with self.assertRaises(ValueError):
                    p4rt_public.owner_test_summary(self.directory)

    def test_external_xml_symlink_is_rejected(self):
        external = self.directory / 'unrelated.xml'
        self.xml[0].rename(external)
        self.xml[0].symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'escaped its retained directory'):
            p4rt_public.owner_test_summary(self.directory)

    def test_export_includes_checked_worker_and_loaded_evidence(self):
        for name in ('worker.json', 'worker-build.json', 'docker-sonic-p4rt.runtime.json', 'loaded-validation.json'):
            self.write(self.directory / name, {'status': 'passed'})
        self.write(self.directory / 'worker-private.json', {'env': {'TOKEN': 'private'}})
        public = p4rt_public.prepare(self.directory)
        for name in ('worker.json', 'worker-build.json', 'docker-sonic-p4rt.runtime.json',
                     'loaded-validation.json', 'owner-test-summary.json'):
            self.assertTrue((public / name).is_file(), name)
        self.assertFalse((public / 'worker-private.json').exists())
        self.assertFalse(list(public.rglob('*.xml')))

    def test_worker_receipts_cannot_export_private_environment(self):
        self.write(self.directory / 'worker-build.json', {'environment': {'TOKEN': 'private'}})
        with self.assertRaises(ValueError):
            p4rt_public.prepare(self.directory)


if __name__ == '__main__':
    unittest.main()
