#!/usr/bin/env python3
"""Actual file permission and fixed recovery tests; no native package actions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tempfile
import tarfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import yaml
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import registry_native_worker as worker
from tools.bazel.ci import registry_native_export as export
from tools.bazel.tests.registry_native_worker_test import condition


class ExportPermissionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root/'artifacts'
        self.file = self.artifacts/'outputs/trees'/('a'*64+'.tar')
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b'original declared tree archive')
        self.file.chmod(0o600)
        self.item = {'path': self.file.relative_to(self.artifacts).as_posix(),
                     'size': self.file.stat().st_size, 'sha256': worker.digest(self.file),
                     'type': 'tree_archive', 'root': 'sdk'}
        self.index = self.artifacts/'outputs.json'
        self.index.write_text(json.dumps({'@owner//:sdk': [self.item]}))
        self.secret = self.artifacts/'private-provenance.json'
        self.secret.write_text('unchanged private'); self.secret.chmod(0o400)

    def apply(self):
        with patch.object(os, 'geteuid', return_value=0):
            return worker.readable_tree_archives(self.artifacts)

    def test_exact_tree_bytes_inode_and_private_modes_preserved(self):
        before = self.file.stat(); private = self.secret.read_bytes()
        result = self.apply()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(self.file.stat().st_ino, before.st_ino)
        self.assertEqual(worker.digest(self.file), self.item['sha256'])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o644)
        self.assertEqual(self.secret.read_bytes(), private)
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o400)
        self.assertEqual(self.apply()['tree_archives'][0]['previous_mode'], 0o644)

    def test_hash_size_undeclared_and_bad_mode_reject_without_chmod(self):
        for mutation in ('hash', 'size', 'extra', 'mode'):
            with self.subTest(mutation=mutation):
                self.file.chmod(0o600); item = dict(self.item)
                extra = self.artifacts/'outputs/extra'
                if mutation == 'hash': item['sha256'] = '0'*64
                elif mutation == 'size': item['size'] += 1
                elif mutation == 'extra': extra.write_text('undeclared')
                else: self.file.chmod(0o755)
                self.index.write_text(json.dumps({'@owner//:sdk': [item]}))
                with self.assertRaises(ValueError): self.apply()
                self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o755 if mutation == 'mode' else 0o600)
                extra.unlink(missing_ok=True)

    def test_symlink_and_hardlink_are_not_permission_targets(self):
        for kind in ('symlink', 'hardlink'):
            self.file.unlink()
            target = self.root/'target'; target.write_bytes(b'original declared tree archive'); target.chmod(0o600)
            if kind == 'symlink': self.file.symlink_to(target)
            else: os.link(target, self.file)
            with self.subTest(kind=kind), self.assertRaises(ValueError): self.apply()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_nonroot_cannot_admit_export_and_file_records_are_untouched(self):
        with patch.object(os, 'geteuid', return_value=1000), self.assertRaises(ValueError):
            worker.readable_tree_archives(self.artifacts)
        item = dict(self.item); item['type'] = 'file'
        self.index.write_text(json.dumps({'@owner//:sdk': [item]}))
        self.assertEqual(self.apply()['tree_archives'], [])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    def test_recovery_export_preserves_failed_partial_public_and_original_tar(self):
        old_public=self.artifacts/'public';old_public.mkdir()
        old_file=old_public/'partial';old_file.write_bytes(b'failed stage retained')
        old_tar=self.artifacts.parent/'registry-native-evidence.tar';old_tar.write_bytes(b'failed TAR retained')
        registry=self.root/'registry';(registry/'ci').mkdir(parents=True)
        script=registry/'ci/archive_artifacts.sh'
        script.write_text('#!/bin/sh\nset -eu\ntar -cf "$2" -C "$1" .\n')
        output=self.root/'control/artifacts/registry-export';output.mkdir(parents=True)
        self.file.chmod(0o644)
        self.secret.unlink() # This fixture tests ordinary public output, not secret metadata.
        with patch.object(worker,'ROOT',self.root/'control'):
            worker.publish(NS(artifacts=self.artifacts,workspace=registry), recovery=True)
            with self.assertRaisesRegex(ValueError,'fresh'):
                worker.publish(NS(artifacts=self.artifacts,workspace=registry), recovery=True)
        self.assertEqual(old_file.read_bytes(),b'failed stage retained')
        self.assertEqual(old_tar.read_bytes(),b'failed TAR retained')
        self.assertEqual((output/'public'/self.item['path']).read_bytes(),self.file.read_bytes())
        with tarfile.open(output/'registry-native-evidence.tar') as archive:
            self.assertEqual(archive.extractfile('./'+self.item['path']).read(),self.file.read_bytes())

    def test_root_symlink_rejected(self):
        link = self.root/'linked'; link.symlink_to(self.artifacts, target_is_directory=True)
        with patch.object(os, 'geteuid', return_value=0), self.assertRaises(ValueError):
            worker.readable_tree_archives(link)


class RecoveryTests(unittest.TestCase):
    def original(self):
        run = {'id': export.ORIGINAL_RUN, 'head_sha': export.ORIGINAL_CONTROL,
               'event': 'workflow_dispatch', 'run_attempt': 1,
               'repository': {'full_name': 'securely1g/sonic-buildimage'},
               'path': '.github/workflows/bazel-oci.yml', 'status': 'completed', 'conclusion': 'failure'}
        job = {'id': export.ORIGINAL_JOB, 'run_id': export.ORIGINAL_RUN, 'status': 'completed',
               'conclusion': 'failure', 'runner_name': export.WORKER, 'steps': [
                   {'name': name, 'status': 'completed', 'conclusion': result} for name, result in (
                       ('Run the original native registry build and tests', 'success'),
                       ('Stage declared native outputs and public receipts', 'failure'),
                       ('Upload ordinary registry consumer evidence', 'success'))]}
        return run, job

    def test_exact_remote_original_phase_and_mutations(self):
        run, job = self.original()
        self.assertEqual(export.check_job(run, job)['original_run'], export.ORIGINAL_RUN)
        for field, bad in [('head_sha', '0'*40), ('id', 12), ('run_attempt', 2), ('event', 'pull_request')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                export.check_job({**run, field: bad}, job)
        for changed in ('runner', 'native', 'duplicate'):
            bad = copy.deepcopy(job)
            if changed == 'runner': bad['runner_name'] = 'sonic-slave-lgh114-1'
            elif changed == 'native': bad['steps'][0]['conclusion'] = 'failure'
            else: bad['steps'].append(bad['steps'][0])
            with self.subTest(changed=changed), self.assertRaises(ValueError): export.check_job(run, bad)

    def test_missing_original_evidence_fails_without_sudo_or_native_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); out = root/'artifacts/registry-export'; out.mkdir(parents=True)
            run, job = self.original()
            (out/'worker-export-origin.json').write_text(json.dumps(export.check_job(run, job)))
            environment = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REPOSITORY': 'securely1g/sonic-buildimage',
                           'GITHUB_ACTOR': 'securely1g', 'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1',
                           'RUNNER_NAME': export.WORKER}
            arguments = ['export.py', 'recover', '--consumer', 'dash', '--revision', export.REVISION,
                         '--worker', export.WORKER, '--workspace', '/actions/_work/registry']
            with patch.object(export, 'ROOT', root), patch.object(export, 'ORIGINAL_ARTIFACTS', root/'missing'), \
                 patch.object(export, 'PRIVATE', root/'cache'), patch.dict(os.environ, environment), \
                 patch.object(sys, 'argv', arguments), patch.object(os, 'getuid', return_value=1000), \
                 patch.object(os, 'getgid', return_value=1000), \
                 patch.object(worker, 'measure', return_value={'status':'passed'}), \
                 patch.object(subprocess, 'run') as commands:
                with self.assertRaisesRegex(ValueError, 'unavailable'): export.main()
                commands.assert_not_called()
            receipt = json.loads((out/'worker-export-recover.json').read_text())
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['failed_stage'], 'retained_evidence')
            self.assertFalse(receipt['native_execution'])
            self.assertFalse(receipt['available']['original_output_index_present'])

    def test_no_package_execution_or_old_receipt_overwrite_in_recovery(self):
        source = (ROOT/'tools/bazel/ci/registry_native_export.py').read_text()
        self.assertNotIn('prepare_tools(', source)
        self.assertNotIn('runner.run(', source)
        self.assertNotIn('runner.collect_outputs(', source)
        self.assertNotIn("'worker-publish.json',", source.split('def main():')[1])
        self.assertIn("worker.publish(", source)
        worker_source = (ROOT/'tools/bazel/ci/registry_native_worker.py').read_text()
        self.assertLess(worker_source.index("readable_tree_archives(args.artifacts)"),
                        worker_source.index("state.update(status='passed',no_oom_events=True)"))


class RetainedNativeValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.artifacts = self.root/'artifacts'; self.artifacts.mkdir()
        self.private = self.root/'private'; self.private.mkdir()
        self.workspace = self.root/'registry'
        self.config = {'build_targets':['@sonic-dash//:runtime'], 'test_targets':['@sonic-dash//bazel:runtime_test']}
        state = {'status':'passed', 'dispatcher_revision':export.ORIGINAL_CONTROL, 'registry_revision':export.REVISION,
                 'worker':export.WORKER, 'server_shutdown':{'status':'stopped'}, 'no_oom_events':True,
                 **self.config}
        records = {'worker-registry.json':state, 'worker-publish.json':{'status':'failed','error_class':'PermissionError'},
                   'worker-run.json':{'status':'passed'}, 'worker-execute.json':{'status':'passed'}}
        for name, value in records.items(): (self.artifacts/name).write_text(json.dumps(value))
        self.hashes = {name:worker.digest(self.artifacts/name) for name in records}
        (self.private/'spec.json').write_text(json.dumps({'work':str(self.private/'work'),
                      'artifacts':str(self.artifacts), 'config':self.config}))
        output = self.artifacts/'outputs/runtime.tar'; output.parent.mkdir(); output.write_bytes(b'declared TAR')
        item = {'path':'outputs/runtime.tar','size':output.stat().st_size,'sha256':worker.digest(output)}
        (self.artifacts/'outputs.json').write_text(json.dumps({self.config['build_targets'][0]:[item]}))
        for phase in ('build','test'):
            events = [{'id':{'started':{}}, 'started':{'command':phase,'buildToolVersion':'8.5.1'}},
                      {'id':{'targetCompleted':{'label':self.config['build_targets'][0]}},'completed':{'success':True}},
                      {'id':{'buildFinished':{}},'finished':{'overallSuccess':True,'exitCode':{'code':0}}}]
            (self.artifacts/(phase+'-events.jsonl')).write_text(''.join(json.dumps(row)+'\n' for row in events))
        self.runner = NS(normalized_label=lambda x:x, verify_tests=Mock())
    def validate(self):
        with patch.object(export,'RECEIPT_HASHES',self.hashes), \
             patch.object(worker,'registry_config',return_value=(self.runner,self.config)), \
             patch.object(shutil,'disk_usage',return_value=NS(free=10**12)):
            return export.validate_native(self.artifacts,self.private,self.workspace)
    def test_complete_retained_evidence_passes_without_commands(self):
        with patch.object(subprocess,'run') as commands:
            proof=self.validate();commands.assert_not_called()
        self.assertEqual(proof['declared_files'],1)
        self.assertEqual(proof['outputs_sha256'],worker.digest(self.artifacts/'outputs.json'))
        self.runner.verify_tests.assert_called_once_with(self.artifacts/'test-events.jsonl',self.config['test_targets'])
    def test_changed_original_receipt_and_missing_or_failed_bep_reject(self):
        original=(self.artifacts/'worker-registry.json').read_bytes()
        (self.artifacts/'worker-registry.json').write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'receipt'):self.validate()
        (self.artifacts/'worker-registry.json').write_bytes(original)
        path=self.artifacts/'build-events.jsonl';original=path.read_bytes()
        path.unlink()
        with self.assertRaises(FileNotFoundError):self.validate()
        path.write_bytes(original.replace(b'"overallSuccess": true',b'"overallSuccess": false'))
        with self.assertRaisesRegex(ValueError,'BEP'):self.validate()
    def test_live_original_server_inventory_change_and_payload_tamper_reject(self):
        server=self.private/'work/bazel/base/server/server.pid.txt';server.parent.mkdir(parents=True);server.write_text('999')
        with patch.object(worker,'server_process',return_value={'state':'S','rss_pages':100}), \
             self.assertRaisesRegex(ValueError,'still live'):self.validate()
        server.unlink()
        path=self.artifacts/'outputs.json';original=path.read_text();path.write_text(original.replace('@sonic-dash//:runtime','@other//:runtime'))
        with self.assertRaisesRegex(ValueError,'target inventory'):self.validate()
        path.write_text(original);(self.artifacts/'outputs/runtime.tar').write_bytes(b'declared BAD')
        with self.assertRaisesRegex(ValueError,'size or hash'):self.validate()
    def test_original_test_failure_and_export_capacity_reject(self):
        self.runner.verify_tests.side_effect=ValueError('original tests failed')
        with self.assertRaisesRegex(ValueError,'tests failed'):self.validate()
        self.runner.verify_tests.side_effect=None
        with patch.object(export,'RECEIPT_HASHES',self.hashes), \
             patch.object(worker,'registry_config',return_value=(self.runner,self.config)), \
             patch.object(shutil,'disk_usage',return_value=NS(free=0)), \
             self.assertRaisesRegex(ValueError,'capacity'):
            export.validate_native(self.artifacts,self.private,self.workspace)


class WorkflowTests(unittest.TestCase):
    def test_exact_dispatch_only_and_no_other_jobs(self):
        flow = yaml.safe_load((ROOT/'.github/workflows/bazel-oci.yml').read_text())
        jobs = flow['jobs']; job = jobs['registry_native_export']
        context = {'mode': 'registry-native-export', 'consumer': 'dash'}
        self.assertNotIn('needs', job)
        self.assertEqual([name for name, body in jobs.items() if condition(str(body.get('if','true')), **context)],
                         ['registry_native_export'])
        for bad in ({'event':'pull_request'}, {'actor':'foreign'}, {'repo':'foreign/repo'},
                    {'revision':'0'*40}, {'worker_name':'sonic-slave-lgh114-1'}, {'consumer':'fips'}):
            with self.subTest(bad=bad): self.assertFalse(condition(job['if'], **{**context, **bad}))
        self.assertFalse(any('setup-bazel' in step.get('uses','') or 'actions/cache' in step.get('uses','')
                             for step in job['steps']))
        self.assertEqual(job['permissions'], {'contents':'read','actions':'read'})
        for step in job['steps']:
            if 'run' in step:
                subprocess.run(['bash','-n'], input=step['run'], text=True, check=True)

    def test_owned_feature_keeps_no_deb_fixtures_and_vs_guard(self):
        jobs = yaml.safe_load((ROOT/'.github/workflows/bazel-oci.yml').read_text())['jobs']
        branch = 'codex/registry-native-export-permissions'
        guards = [step['env']['SOURCE_ONLY_NO_DEBS'] for job in jobs.values() for step in job.get('steps',[])
                  if 'SOURCE_ONLY_NO_DEBS' in step.get('env', {})]
        self.assertEqual(len(guards), 2)
        for guard in guards:
            self.assertTrue(condition(guard, event='pull_request', branch=branch))
            self.assertFalse(condition(guard, event='pull_request', branch=branch+'-other'))
            self.assertFalse(condition(guard, event='pull_request', branch=branch, head_repo='foreign/repo'))
        self.assertFalse(condition(jobs['vs_container']['if'], event='pull_request', branch=branch))


if __name__ == '__main__':
    unittest.main()
