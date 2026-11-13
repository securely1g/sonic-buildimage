"""Boundary fixtures; no Bazel, source acquisition or package operation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.bazel.ci import qualify_bitarray_registry_module as subject
from tools.bazel.ci import remote_cache_smoke as reader


class QualificationBoundaryTest(unittest.TestCase):
    def inputs(self):
        return {source: {'sha256': str(index + 1) * 64}
                for index, source in enumerate(subject.CPP.values())}

    def records(self, hit=True):
        result = []
        for index, (target, source) in enumerate(subject.CPP.items()):
            result.append({'mnemonic': 'CppCompile', 'targetLabel': '@@bitarray+//:' + target,
                           'exitCode': 0, 'cacheHit': hit,
                           'runner': 'remote cache hit' if hit else 'linux-sandbox',
                           'digest': {'hash': str(index + 3) * 64},
                           'inputs': [{'path': 'external/bitarray+/' + source,
                                       'digest': {'hash': self.inputs()[source]['sha256']}},
                                      {'path': 'external/bitarray+/' + subject.HEADER,
                                       'digest': {'hash': 'a' * 64}}],
                           'actualOutputs': [{'path': 'bazel-out/k8/bin/' + str(index) + '.pic.o'}]})
        return result

    def evidence(self, records, hit=True, header='a' * 64):
        raw = '\n'.join(json.dumps(x) for x in records).encode()
        return subject.compile_evidence(raw, reader, header, self.inputs(), hit)['actions']

    def test_two_remote_cpp_actions_use_qualified_delimited_decoder(self):
        result = self.evidence(self.records())
        self.assertEqual(set(result), set(subject.CPP))
        self.assertTrue(all(x['cache_hit'] and x['runner'] == 'remote cache hit' for x in result.values()))

    def test_local_first_pass_is_observation_but_cannot_be_remote_proof(self):
        records = self.records(False)
        self.assertEqual(len(self.evidence(records, False)), 2)
        with self.assertRaises(ValueError):
            self.evidence(records)

    def test_cache_hit_without_remote_runner_is_rejected(self):
        records = self.records(); records[0]['runner'] = 'linux-sandbox'
        with self.assertRaises(ValueError):
            self.evidence(records)

    def test_missing_or_duplicate_compile_action_is_rejected(self):
        for records in (self.records()[:1], self.records() + self.records()[:1]):
            with self.subTest(count=len(records)), self.assertRaises(ValueError):
                self.evidence(records)

    def test_unexpected_compile_owner_is_rejected(self):
        records = self.records(); records[0]['targetLabel'] = '@@other+//:extension'
        with self.assertRaises(ValueError):
            self.evidence(records)

    def test_wrong_header_or_source_digest_is_rejected(self):
        for index in (0, 1):
            records = self.records(); records[0]['inputs'][index]['digest']['hash'] = 'f' * 64
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.evidence(records)

    def test_edited_header_requires_actual_edited_action_input(self):
        records = self.records()
        for record in records:
            record['inputs'][1]['digest']['hash'] = 'b' * 64
        self.assertEqual(len(self.evidence(records, header='b' * 64)), 2)
        with self.assertRaises(ValueError):
            self.evidence(records)

    def test_ambiguous_proto_fields_use_existing_rejection(self):
        records = self.records(); records[0]['cache_hit'] = True
        with self.assertRaises(ValueError):
            self.evidence(records)

    def test_duplicate_json_and_oversize_logs_are_rejected(self):
        with self.assertRaises(ValueError):
            subject.compile_evidence(b'{"mnemonic":"CppCompile","mnemonic":"CppCompile"}', reader,
                                     'a' * 64, self.inputs(), True)
        original = subject.LOG_LIMIT
        try:
            subject.LOG_LIMIT = 8
            with self.assertRaises(ValueError):
                subject.compile_evidence(b'123456789', reader, 'a' * 64, self.inputs(), True)
        finally:
            subject.LOG_LIMIT = original

    def test_only_original_fixed_runner_command_is_instrumented(self):
        work = Path('/scratch/pass-1/work'); launcher = Path('/scratch/pass-1-bazel')
        prefix = [str(launcher), '--ignore_all_rc_files', '--output_user_root=' + str(work / 'bazel'), '--batch']
        command = prefix + ['build', '--repository_cache=', *subject.BUILD]
        startup, phase, options, targets = subject.command_parts(command, launcher, work)
        self.assertEqual((phase, options, targets), ('build', ['--repository_cache='], subject.BUILD))
        for changed in (command[:-1] + ['//:unexpected'], prefix + ['test', *subject.BUILD],
                        [str(launcher), '--batch', 'build', *subject.BUILD],
                        prefix + ['build', '--execution_log_json_file=other', *subject.BUILD]):
            with self.subTest(command=changed), self.assertRaises(ValueError):
                subject.command_parts(changed, launcher, work)

    def test_supported_graph_rejects_old_pip_bitarray_repository(self):
        result = subject.graph_labels('INFO: analysis complete\n@@bitarray+//:whl (abc)\n//:installed_layer')
        self.assertEqual(result['target_count'], 2)
        self.assertFalse(result['repository_time_pip_bitarray_reachable'])
        with self.assertRaises(ValueError):
            subject.graph_labels('@@rules_python++pip+config_engine_pip_313_bitarray//:pkg')

    def test_disposable_source_copy_does_not_modify_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'original'; source.mkdir()
            original = b'/* original */\n'; (source / 'header.h').write_bytes(original)
            inputs = {'header.h': {'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest()}}
            target = root / 'disposable'; subject.source_copy(source, target, inputs)
            (target / 'header.h').write_bytes(b'changed')
            self.assertEqual((source / 'header.h').read_bytes(), original)

    def test_source_copy_rejects_links_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'original'; source.mkdir()
            (source / 'file').write_bytes(b'x'); (source / 'link').symlink_to('file')
            with self.assertRaises(ValueError):
                subject.source_copy(source, root / 'copy', {})
            self.assertFalse((root / 'copy').exists())

    def test_header_is_restored_even_when_build_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'header.h'; original = b'original\n'; path.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, 'simulated build failure'):
                with subject.changed_header(path):
                    self.assertEqual(path.read_bytes(), original + subject.COMMENT)
                    raise RuntimeError('simulated build failure')
            self.assertEqual(path.read_bytes(), original)

    def test_unpublished_registry_placeholder_is_not_executable(self):
        plan = {'module': subject.MODULE, 'version': subject.VERSION, 'architecture': 'amd64',
                'build_targets': subject.BUILD, 'test_targets': subject.TEST,
                'registry_revision': 'PENDING_REVIEWED_BITARRAY_REGISTRY_COMMIT'}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'plan.json'; raw = json.dumps(plan).encode(); path.write_bytes(raw)
            with self.assertRaises(ValueError):
                subject.plan_data(path, hashlib.sha256(raw).hexdigest())

    def test_phase_reports_preserve_completed_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / 'reports').mkdir(); artifacts = root / 'private'; artifacts.mkdir()
            original = {'status': 'passed'}; (artifacts / 'validation.json').write_text(json.dumps(original))
            with mock.patch.object(subject, 'operator', return_value={'kind': 'fixture'}):
                subject.retain_phase(root, 'pass-1', artifacts, {'whl': {'sha256': 'a' * 64, 'bytes': 1}})
            result = json.loads((root / 'reports/pass-1/phase.json').read_bytes())
            self.assertEqual(json.loads((root / 'reports/pass-1/validation.json').read_bytes()), original)
            self.assertFalse(result['full_qualification_passed'])
            with self.assertRaises(FileExistsError):
                subject.retain_phase(root, 'pass-1', artifacts, {})


if __name__ == '__main__':
    unittest.main()
