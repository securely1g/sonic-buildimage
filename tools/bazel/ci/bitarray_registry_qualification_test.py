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


    def test_prepared_launcher_bases_satisfy_original_registry_log_collector(self):
        import ast
        import os
        import sys
        from types import SimpleNamespace
        from tools.bazel.ci import source_image_worker as worker

        owner = Path(os.environ.get('BITARRAY_REGISTRY_CI_OWNER',
                     str(Path(__file__).resolve().parents[3] / 'registry/ci/registry_ci.py')))
        owners = {
            'registry_ci.py': (112220, '11698617660f45425658c37df5b5175c180565f32a8feed768760945f79d9d36'),
            'cross_toolchain.py': (17060, '0838b86ac0a6ff8ac7dacaa3c05c6c094b0338b17e98913cd345379dcb2b6061'),
            'cross_pip.py': (7797, 'dc13e862ff4d078a7bea8135d91b4982d45fdf3561735124f74290c89685857f'),
            'cross_autoconf.py': (1789, '169053824f712ea83a6db84d682638382eeeaeb0c2868b33ec52f86316c01d81'),
            'github_sources.py': (19706, '9c1ad2a9548608d06e49de0c9c119ae19d97ab95cca2505fc1e7e233e62e9b3d'),
        }
        for name, (size, digest) in owners.items():
            subject.exact(owner.with_name(name), {'bytes': size, 'sha256': digest})
        runner = subject.load_owner('bitarray_fixture_registry_ci', owner,
                                    {'bytes': owners['registry_ci.py'][0],
                                     'sha256': owners['registry_ci.py'][1]})
        original_home = os.environ.get('HOME')
        self.assertIsNotNone(original_home)
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary).resolve()
            profile = {'jobs': 2, 'cpu': 4, 'scheduler_memory_mb': 8000, 'jvm_heap_gib': 3}
            binary = str(Path(sys.executable).resolve(strict=True))
            (scratch / 'resources.json').write_text(json.dumps({'selected_profile': profile}))
            (scratch / 'native-bazel').write_text(worker.launcher_text(
                binary, scratch / 'output-base', profile))
            for index in range(5):
                (scratch / 'cache-setup/distdir' / str(index)).mkdir(parents=True)
            support = SimpleNamespace(source_check=mock.Mock(), prepare_cache=mock.Mock())
            plan = {'registry_revision': 'a' * 40, 'registry_files': {},
                    'source_revision': 'b' * 40, 'native_revision': 'c' * 40}
            source, native, registry = (scratch / name for name in ('source', 'native', 'registry'))
            with mock.patch.object(subject.shutil, 'which', return_value=binary), \
                 mock.patch.object(worker, 'remote_cache_configuration', return_value=None), \
                 mock.patch.object(subject, 'operator', return_value={'kind': 'fixture'}):
                subject.prepare(plan, source, native, registry, scratch, {}, scratch / 'mirrors', support)
            support.prepare_cache.assert_called_once()
            setup = json.loads((scratch / 'setup.json').read_bytes())
            self.assertEqual(set(setup['launchers']),
                             {'pass-1', 'pass-2', 'copy-baseline', 'header-edited', 'restored'})
            self.assertEqual(os.environ.get('HOME'), original_home)
            self.assertEqual(setup['home_sha256'], hashlib.sha256(original_home.encode()).hexdigest())
            bases = set()
            for name, identity in setup['launchers'].items():
                with self.subTest(launcher=name):
                    raw_launcher = subject.exact(scratch / (name + '-bazel'), identity)
                    assignments = [node.value for node in ast.parse(raw_launcher).body
                                   if isinstance(node, ast.Assign)
                                   and any(isinstance(t, ast.Name) and t.id == 'base' for t in node.targets)]
                    self.assertEqual(len(assignments), 1)
                    output_base = Path(ast.literal_eval(assignments[0]))
                    bases.add(output_base)
                    work = scratch / ('pass-1' if name == 'pass-1' else 'pass-2') / 'work'
                    logs = output_base / 'execroot/_main/bazel-out/k8-fastbuild/testlogs/bitarray/test'
                    logs.mkdir(parents=True)
                    declared = []
                    expected = {}
                    for filename in ('test.log', 'test.xml'):
                        data = (name + ':' + filename + '\n').encode()
                        path = logs / filename
                        path.write_bytes(data)
                        declared.append({'name': filename, 'uri': path.as_uri()})
                        expected[filename] = data
                    event = {'id': {'testResult': {'label': '@@bitarray+//:upstream_test',
                                                   'run': 1, 'shard': 1, 'attempt': 1}},
                             'testResult': {'status': 'PASSED', 'testActionOutput': declared}}
                    artifacts = scratch / 'collector-fixture' / name
                    artifacts.mkdir(parents=True)
                    bep = artifacts / 'test-events.jsonl'
                    bep.write_text(json.dumps(event) + '\n')
                    runner.collect_test_logs(bep, work, artifacts)
                    rows = json.loads((artifacts / 'test-results.json').read_bytes())
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['test'], event['id']['testResult'])
                    self.assertEqual(rows[0]['status'], 'PASSED')
                    self.assertEqual(len(rows[0]['files']), 2)
                    for record in rows[0]['files']:
                        saved = artifacts / record['path']
                        data = expected[saved.name]
                        self.assertEqual(saved.read_bytes(), data)
                        self.assertEqual(record['size'], len(data))
                        self.assertEqual(record['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(len(bases), 5)
            old_work = scratch / 'old-layout-work'
            old_log = old_work / 'output-base/execroot/_main/bazel-out/k8-fastbuild/testlogs/test/test.log'
            old_log.parent.mkdir(parents=True)
            old_log.write_text('original incompatible layout\n')
            event['testResult']['testActionOutput'] = [{'name': 'test.log', 'uri': old_log.as_uri()}]
            rejected = scratch / 'old-layout-artifacts'; rejected.mkdir()
            bep = rejected / 'test-events.jsonl'; bep.write_text(json.dumps(event) + '\n')
            with self.assertRaisesRegex(runner.RegistryError, "outside Bazel's configuration testlogs"):
                runner.collect_test_logs(bep, old_work, rejected)


if __name__ == '__main__':
    unittest.main()
