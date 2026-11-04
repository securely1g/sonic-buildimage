#!/usr/bin/env python3
"""Guard fixed registry workers without running Bazel or package installers."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, Mock
import yaml
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tools.bazel.ci import registry_native_worker as worker

class RegistryWorkerTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.environment={'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REPOSITORY':'securely1g/sonic-buildimage',
            'GITHUB_ACTOR':'securely1g','GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','RUNNER_NAME':worker.WORKERS[0]}
        self.profile=worker.PROFILES['fips']
        self.spec={'binary':'/usr/bin/fake-bazel','work':str(self.root/'work'),'artifacts':str(self.root/'artifacts'),
            'repository_cache':'/bazel_cache/registry-native/worker/repository','disk_cache':'/bazel_cache/registry-native/worker/disk',
            'profile':{'jobs':2,'cpu':2,'scheduler_memory_mb':8000},
            'config':{'build_targets':['@sonic-fips//:runtime'],'test_targets':['@sonic-fips//:runtime_test']}}
        self.startup=['--ignore_all_rc_files','--output_user_root='+str(self.root/'work/bazel')]
    def selection(self,**kwargs):
        return worker.selection(kwargs.get('consumer','fips'),kwargs.get('revision',self.profile['revision']),
            kwargs.get('worker',worker.WORKERS[0]),kwargs.get('environment',self.environment))
    def test_selection_is_fixed_and_owned(self):
        self.assertEqual(self.selection(),self.profile)
        for key,value in [('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REPOSITORY','foreign/sonic-buildimage'),
                          ('GITHUB_ACTOR','other'),('RUNNER_NAME',worker.WORKERS[1]),('GITHUB_RUN_ID','0'),('GITHUB_RUN_ATTEMPT','x')]:
            with self.subTest(key=key),self.assertRaises(ValueError): self.selection(environment={**self.environment,key:value})
        for change in [{'consumer':'other'},{'revision':'master'},{'revision':'a'*40},{'worker':'ubuntu-24.04'}]:
            with self.subTest(change=change),self.assertRaises(ValueError): self.selection(**change)
    def test_common_requires_actual_provisioner_identity(self):
        marker={'base_image':'sha256:'+'a'*64}
        self.assertIsNone(worker.container_reference({},marker))
        with self.assertRaisesRegex(ValueError,'provisioner-verified'):
            worker.container_reference({'test_runtime_preparation':{'script':'checked.py'}},marker)
        self.assertEqual(worker.SLAVE_IMAGE_REFERENCES,{})
    def test_child_environment_excludes_credentials(self):
        result=worker.child_environment({**self.environment,'GH_TOKEN':'private','ACTIONS_RUNTIME_TOKEN':'private',
            'GIT_CONFIG_GLOBAL':'private','HTTP_PROXY':'http://private.invalid','OTHER':'private'},Path('/actions/_work/registry'),Path('/bazel_cache/registry-native/w'))
        self.assertFalse({'GH_TOKEN','ACTIONS_RUNTIME_TOKEN','GIT_CONFIG_GLOBAL','OTHER'} & result.keys())
        self.assertEqual(result['HTTP_PROXY'],'http://private.invalid')
        self.assertEqual(result['GIT_CONFIG_VALUE_0'],'/actions/_work/registry')
        self.assertEqual(result['GIT_CONFIG_VALUE_1'],str(ROOT))
        self.assertEqual(result['USE_BAZEL_VERSION'],'8.5.1')
    def test_wrapper_preserves_output_root_and_resource_precedence(self):
        command=[*self.startup,'build','--jobs=99','--remote_cache=https://example.invalid',*self.spec['config']['build_targets']]
        startup,verb,flags=worker.adjusted_bazel(self.spec,command)
        self.assertEqual(startup[:3],[self.spec['binary'],*self.startup]);self.assertEqual(verb,'build')
        self.assertEqual(flags[-6:],['--repository_cache='+self.spec['repository_cache'],'--disk_cache='+self.spec['disk_cache'],
            '--remote_cache=','--jobs=2','--local_resources=cpu=2','--local_resources=memory=8000'])
        for arguments in [['--output_base=/tmp/other','build'],['--ignore_all_rc_files','--output_user_root=/tmp/other','build']]:
            with self.assertRaises(ValueError): worker.adjusted_bazel(self.spec,arguments)
    def test_info_shutdown_share_startup_without_build_flags(self):
        for verb in ['info','shutdown']:
            startup,actual,flags=worker.adjusted_bazel(self.spec,[*self.startup,verb])
            self.assertEqual(startup[:3],[self.spec['binary'],*self.startup])
            self.assertEqual(actual,verb);self.assertEqual(flags,[])
    def test_audit_covers_full_original_union(self):
        auditor=Mock()
        worker.audit_before_build(self.spec,[*self.startup,'test','--nocache_test_results','--test_output=errors',
            '--build_event_json_file=/private/raw.jsonl',*self.spec['config']['test_targets']],auditor)
        self.assertEqual(auditor.call_args.args[2],self.spec['config']['build_targets']+self.spec['config']['test_targets'])
        self.assertFalse(any('build_event_json' in f or 'test_output=' in f for f in auditor.call_args.args[1]))
        with self.assertRaises(ValueError):
            worker.audit_before_build(self.spec,[*self.startup,'build','@sonic-fips//:other'],auditor)
        self.assertEqual(auditor.call_count,1)
    def test_real_deb_graph_rejection_prevents_exec(self):
        spec=self.root/'spec.json';spec.write_text(json.dumps(self.spec))
        graph={'pathFragments':[{'id':1,'label':'unsafe.deb'}],'artifacts':[{'id':1,'pathFragmentId':1}],
            'actions':[{'outputIds':[1],'arguments':['/usr/bin/tool']}]}
        def query(*args,**kwargs):
            kwargs['stdout'].write(json.dumps(graph));return NS(returncode=0)
        with patch.dict(os.environ,{'REGISTRY_WORKER_SPEC':str(spec)}),\
             patch.object(sys,'argv',['helper','bazel',*self.startup,'build',*self.spec['config']['build_targets']]),\
             patch('subprocess.run',side_effect=query),patch.object(os,'execv') as execute:
            with self.assertRaisesRegex(ValueError,'DEB'): worker.bazel_main()
            execute.assert_not_called()
        self.assertEqual(json.loads((Path(self.spec['artifacts'])/'no-deb-build.json').read_text())['deb_outputs'],['unsafe.deb'])

    def test_cache_descendant_symlinks_are_rejected(self):
        cache=self.root/'cache';cache.mkdir()
        outside=self.root/'outside';outside.mkdir()
        (cache/'repository').symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlinks'): worker.cache_directory(cache,'repository')
        self.assertEqual(worker.cache_directory(cache,'disk'),cache/'disk')
    def test_early_failure_retains_safe_failed_phase_receipt(self):
        artifacts=self.root/'phase-artifacts'
        arguments=['helper','run','--consumer','fips','--revision',self.profile['revision'],
                   '--worker',worker.WORKERS[0],'--workspace',str(self.root/'checkout'),'--artifacts',str(artifacts)]
        original_relative=Path.is_relative_to
        with patch.dict(os.environ,self.environment),patch.object(sys,'argv',arguments),             patch.object(Path,'is_relative_to',lambda p,o: str(o)=='/actions/_work' or original_relative(p,o)),             patch.object(worker,'clean_source',side_effect=ValueError('private credential text')):
            with self.assertRaises(ValueError): worker.main()
        receipt=json.loads((artifacts/'worker-run.json').read_text())
        self.assertEqual(receipt['status'],'failed');self.assertEqual(receipt['error_class'],'ValueError')
        self.assertNotIn('private',json.dumps(receipt))

    def test_shutdown_uses_actual_consumer_and_observes_exact_process_stop(self):
        work=self.root/'work';consumer=work/'consumer';consumer.mkdir(parents=True)
        pidfile=work/'bazel/identity/server/server.pid.txt';pidfile.parent.mkdir(parents=True)
        child=subprocess.Popen(['/usr/bin/sleep','60'])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        pidfile.write_text(str(child.pid))
        def info(command,**kwargs):
            self.assertEqual(kwargs['cwd'],consumer);self.assertEqual(command[-2:],['info','server_pid'])
            return str(child.pid)
        def shutdown(command,**kwargs):
            self.assertEqual(kwargs['cwd'],consumer);self.assertEqual(command[-1],'shutdown')
            child.terminate();child.wait();return NS(returncode=0)
        with patch.object(subprocess,'check_output',side_effect=info),patch.object(subprocess,'run',side_effect=shutdown):
            result=worker.shutdown_consumer(self.root/'wrapper',work)
        self.assertEqual(result['status'],'stopped');self.assertIsNone(worker.server_process(child.pid))
    def test_shutdown_does_not_start_server_before_consumer_exists(self):
        with patch.object(subprocess,'check_output') as info,patch.object(subprocess,'run') as run:
            self.assertEqual(worker.shutdown_consumer(self.root/'wrapper',self.root/'work')['status'],'not_started')
            info.assert_not_called();run.assert_not_called()
    def test_shutdown_zero_exit_does_not_accept_a_live_server(self):
        work=self.root/'work';(work/'consumer').mkdir(parents=True)
        pidfile=work/'bazel/identity/server/server.pid.txt';pidfile.parent.mkdir(parents=True);pidfile.write_text('1234')
        process={'state':'S','started':40,'rss_pages':20}
        with patch.object(worker,'server_process',return_value=process),             patch.object(subprocess,'check_output',return_value='1234'),             patch.object(subprocess,'run',return_value=NS(returncode=0)):
            with self.assertRaisesRegex(ValueError,'remains alive'):
                worker.shutdown_consumer(self.root/'wrapper',work)

    def test_original_lock_bytes_allow_only_selected_local_registry(self):
        workspace=self.root/'registry';workspace.mkdir()
        original={'lockFileVersion':26,'registryFileHashes':{
            workspace.as_uri()+'/modules/sonic-fips/version/MODULE.bazel':'a'*64},'moduleExtensions':{}}
        before=json.dumps(original)
        worker.validate_registry_lock(original,workspace,self.profile['revision'])
        self.assertEqual(json.dumps(original),before)
        for url in ['file:///root/private','file:///actions/another/modules/x/MODULE.bazel',
                    workspace.as_uri()+'/../private',workspace.as_uri()+'/credentials']:
            value=copy.deepcopy(original);value['registryFileHashes']={url:'a'*64}
            with self.subTest(url=url),self.assertRaises(ValueError):
                worker.validate_registry_lock(value,workspace,self.profile['revision'])
    def fixture(self):
        artifacts=self.root/'evidence';artifacts.mkdir();(artifacts/'outputs').mkdir()
        payload=artifacts/'outputs/runtime.tar';payload.write_bytes(b'actual declared native payload')
        index={'@sonic-fips//:runtime':[{'path':'outputs/runtime.tar','size':payload.stat().st_size,'sha256':worker.digest(payload)}]}
        (artifacts/'outputs.json').write_text(json.dumps(index))
        return artifacts,payload,index
    def test_outputs_reject_wrong_bytes_size_extra_paths_and_symlinks(self):
        artifacts,payload,index=self.fixture();self.assertTrue(worker.verify_outputs(artifacts))
        for field,value in [('size',0),('sha256','0'*64),('path','../outside'),('path','')]:
            broken=copy.deepcopy(index);broken['@sonic-fips//:runtime'][0][field]=value
            (artifacts/'outputs.json').write_text(json.dumps(broken))
            with self.subTest(field=field),self.assertRaises(ValueError): worker.verify_outputs(artifacts)
        (artifacts/'outputs.json').write_text(json.dumps(index))
        extra=artifacts/'outputs/unlisted';extra.write_text('extra')
        with self.assertRaisesRegex(ValueError,'Undeclared'): worker.verify_outputs(artifacts)
        extra.unlink();payload.unlink();payload.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError): worker.verify_outputs(artifacts)
    def test_publish_actual_archive_and_test_evidence(self):
        artifacts,payload,_=self.fixture()
        (artifacts/'worker-registry.json').write_text('{"status":"passed"}')
        (artifacts/'build.log').write_text('private command environment')
        (artifacts/'test-results').mkdir();(artifacts/'test-results/test.xml').write_text('<testsuite tests="1"/>')
        workspace=self.root/'registry';(workspace/'ci').mkdir(parents=True)
        (workspace/'ci/archive_artifacts.sh').write_text('#!/bin/sh\nset -eu\ntar -cf "$2" -C "$1" .\n')
        worker.publish(NS(artifacts=artifacts,workspace=workspace))
        with tarfile.open(self.root/'registry-native-evidence.tar') as archive:
            names=archive.getnames()
            self.assertIn('./outputs/runtime.tar',names);self.assertIn('./test-results/test.xml',names)
            self.assertNotIn('./build.log',names)
            self.assertEqual(archive.extractfile('./outputs/runtime.tar').read(),payload.read_bytes())
        self.assertNotEqual(payload.stat().st_ino,(artifacts/'public/outputs/runtime.tar').stat().st_ino)
    def test_partial_failure_receipts_exclude_unindexed_outputs(self):
        artifacts=self.root/'evidence';artifacts.mkdir();(artifacts/'outputs').mkdir()
        (artifacts/'outputs/partial').write_text('unverified')
        (artifacts/'worker-registry.json').write_text('{"status":"failed","error_class":"ValueError"}')
        with patch.object(subprocess,'run'): worker.publish(NS(artifacts=artifacts,workspace=self.root))
        self.assertFalse((artifacts/'public/outputs').exists())
        self.assertEqual(json.loads((artifacts/'public/worker-registry.json').read_text())['status'],'failed')
    def test_publication_rejects_private_json_and_unknown_jsonl(self):
        for filename,content in [('secret.json','{"env":{"TOKEN":"secret"}}'),('arbitrary.jsonl','{"argv":["private"]}')]:
            artifacts=self.root/filename.replace('.','-');artifacts.mkdir();(artifacts/filename).write_text(content)
            with self.subTest(filename=filename),self.assertRaises(ValueError),patch.object(subprocess,'run'):
                worker.publish(NS(artifacts=artifacts,workspace=self.root))

def namespace(value):
    return NS(**{k:namespace(v) if isinstance(v,dict) else v for k,v in value.items()})
def condition(expression,event='workflow_dispatch',mode='registry-native',actor='securely1g',repo='securely1g/sonic-buildimage',
              branch='codex/registry-native-workers',head_repo='securely1g/sonic-buildimage',consumer='fips'):
    text=expression.strip()
    if text.startswith('$'+'{{'): text=text[3:-2].strip()
    text=text.replace('&&',' and ').replace('||',' or ');text=re.sub(r'!(?!=)','not ',text)
    return bool(eval(text,{'__builtins__':{}},{
        'github':namespace({'event_name':event,'repository':repo,'actor':actor,'head_ref':branch,'ref':'refs/heads/test',
            'event':{'pull_request':{'number':9999,'head':{'repo':{'full_name':head_repo}}}}}),
        'inputs':namespace({'source_image':mode,'registry_consumer':consumer,'source_inputs_run':'','skip_vs':True}),
        'needs':namespace({'source_image_inputs':{'result':'skipped'}}),
        'always':lambda:True,'cancelled':lambda:False,'contains':lambda items,item:item in items,'fromJSON':json.loads,'false':False,'true':True}))

class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.flow=yaml.safe_load((ROOT/'.github/workflows/bazel-oci.yml').read_text())
    def test_registry_only_routes_owned_manual_dispatch_without_hosted_needs(self):
        jobs=self.flow['jobs'];self.assertNotIn('needs',jobs['registry_native'])
        self.assertEqual([name for name,job in jobs.items() if condition(str(job.get('if','true')))],['registry_native'])
        for changes in [{'event':'pull_request'},{'event':'push'},{'actor':'other'},{'repo':'foreign/repo'},
                        {'mode':'sysmgr'},{'consumer':'none'}]:
            self.assertFalse(condition(jobs['registry_native']['if'],**changes))
        self.assertEqual(jobs['registry_native']['runs-on'],['sonic-slave-container-dpool','$'+'{{ inputs.source_worker }}'])
    def test_existing_image_choices_and_jobs_remain(self):
        jobs=self.flow['jobs']
        self.assertEqual(set(jobs),{'registry_native','source_image_inputs','source_image','source_worker_preflight','archive','checks','vs','vs_container'})
        for choice in ['sysmgr','dash-ha','orchagent','sflow','otel','syncd','gearbox','dash-engine','gnmi','gnmi-no-dialout']:
            self.assertTrue(condition(jobs['source_image_inputs']['if'],mode=choice))
        self.assertFalse(condition(jobs['source_image_inputs']['if']));self.assertFalse(condition(jobs['source_image']['if']))
    def test_owned_pr_disables_deb_fixtures_and_full_vs(self):
        guards=[step['env']['SOURCE_ONLY_NO_DEBS'] for job in self.flow['jobs'].values()
            for step in job.get('steps',[]) if 'SOURCE_ONLY_NO_DEBS' in step.get('env',{})]
        self.assertEqual(len(guards),2)
        for guard in guards:
            self.assertTrue(condition(guard,event='pull_request'))
            self.assertFalse(condition(guard,event='pull_request',head_repo='foreign/repo'))
            self.assertFalse(condition(guard,event='pull_request',branch='codex/registry-native-workers-extra'))
        self.assertFalse(condition(self.flow['jobs']['vs_container']['if'],event='pull_request'))
    def test_fixed_checkout_permissions_and_no_hosted_cache_dependency(self):
        job=self.flow['jobs']['registry_native'];self.assertEqual(job['permissions'],{'contents':'read'})
        checkouts=[s for s in job['steps'] if s.get('uses','').startswith('actions/checkout@')]
        self.assertEqual(len(checkouts),2);self.assertTrue(all(s['with']['persist-credentials'] is False for s in checkouts))
        self.assertEqual(checkouts[1]['with']['repository'],'securely1g/sonic-bazel-registry')
        self.assertEqual(checkouts[1]['with']['ref'],'$'+'{{ inputs.source_revision }}')
        self.assertFalse(any('setup-bazel' in s.get('uses','') or 'download-artifact' in s.get('uses','') for s in job['steps']))

if __name__=='__main__': unittest.main()
