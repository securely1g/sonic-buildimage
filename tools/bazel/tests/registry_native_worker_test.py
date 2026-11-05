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

    def test_each_fixed_consumer_accepts_only_its_reviewed_sha(self):
        self.assertEqual(worker.PROFILES['libteam']['revision'], '096b904fb34cecdacf497301ad00b712d53aefd6')
        with self.assertRaisesRegex(ValueError, 'Unreviewed registry selection'):
            self.selection(consumer='libteam', revision='4ed05183275951b03342d89db98f709779487ec0')
        for name,profile in worker.PROFILES.items():
            with self.subTest(consumer=name):
                self.assertEqual(self.selection(consumer=name,revision=profile['revision']),profile)
                for other in worker.PROFILES.values():
                    if other['revision']!=profile['revision']:
                        with self.assertRaises(ValueError):
                            self.selection(consumer=name,revision=other['revision'])
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
    def test_source_input_numeric_context_propagates_without_credentials(self):
        for value in ['41689621493','']:
            result=worker.child_environment({**self.environment,'SOURCE_INPUTS_RUN':value,'GH_TOKEN':'secret'},
                self.root/'registry',self.root/'cache')
            self.assertEqual(result.get('SOURCE_INPUTS_RUN',''),value)
            self.assertNotIn('GH_TOKEN',result)
        for value in ['0','https://example.invalid','123;command','1'*21]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                worker.child_environment({'SOURCE_INPUTS_RUN':value},self.root,self.root)
    def test_reused_source_inventory_preparation_uses_confined_root_cache(self):
        from tools.bazel.ci import source_image_worker as source
        artifacts=self.root/'artifacts';artifacts.mkdir()
        cache=self.root/'cache';cache.mkdir()
        provenance={'run':123,'repository':'securely1g/sonic-buildimage','producer_status':'success','artifact_name':'bazel-repository-inputs'}
        (artifacts/'worker-input-run.json').write_text(json.dumps(provenance))
        directories=[cache/'distdir'/(name+'-'+version) for name,(version,_) in source.SOURCES.items()]
        with patch.dict(os.environ,{'SOURCE_INPUTS_RUN':'123','RUNNER_TEMP':str(self.root/'temporary')}), \
             patch.object(source,'prepare_source_downloads',return_value=(directories,[{'module':'ape'}])) as prepare:
            result=worker.verified_source_distdirs(cache,artifacts)
            prepare.assert_called_once_with(self.root/'temporary/source-image-inputs',cache)
        self.assertEqual(result,[str(p) for p in directories])
        self.assertTrue(all(p.is_dir() for p in directories))
        self.assertEqual(json.loads((artifacts/'worker-source-downloads.json').read_text()),[{'module':'ape'}])
        provenance['run']=124;(artifacts/'worker-input-run.json').write_text(json.dumps(provenance))
        with patch.dict(os.environ,{'SOURCE_INPUTS_RUN':'123'}),patch.object(source,'prepare_source_downloads') as prepare:
            with self.assertRaisesRegex(ValueError,'provenance'):worker.verified_source_distdirs(cache,artifacts)
            prepare.assert_not_called()
    def test_source_cache_symlink_rejected_before_integrity_tool(self):
        from tools.bazel.ci import source_image_worker as source
        artifacts=self.root/'artifacts';artifacts.mkdir()
        (artifacts/'worker-input-run.json').write_text(json.dumps({'run':123,'repository':'securely1g/sonic-buildimage',
            'producer_status':'success','artifact_name':'bazel-repository-inputs'}))
        cache=self.root/'cache';directory=cache/'distdir/ape-1.0.1';directory.mkdir(parents=True)
        (directory/'src.tar.gz').symlink_to(self.root/'outside')
        with patch.dict(os.environ,{'SOURCE_INPUTS_RUN':'123'}),patch.object(source,'prepare_source_downloads') as prepare:
            with self.assertRaisesRegex(ValueError,'symlinks'):worker.verified_source_distdirs(cache,artifacts)
            prepare.assert_not_called()
    def test_no_reused_inputs_preserves_original_path(self):
        with patch.dict(os.environ,{'SOURCE_INPUTS_RUN':''}):
            self.assertEqual(worker.verified_source_distdirs(self.root/'missing',self.root/'missing'),[])
        self.assertFalse((self.root/'missing').exists())
    def test_mirror_config_checks_original_and_preserves_bytes(self):
        from tools.bazel.ci import source_image_worker as source
        registry=self.root/'registry';(registry/'ci').mkdir(parents=True)
        original=registry/'ci/source_mirrors.cfg';original.write_bytes(b'original config')
        private=self.root/'private';private.mkdir();artifacts=self.root/'artifacts';artifacts.mkdir()
        with patch.object(source,'mirror_configuration',side_effect=ValueError('checksum mismatch')):
            with self.assertRaisesRegex(ValueError,'checksum'):worker.verified_mirror_config(registry,private,artifacts)
        self.assertFalse((private/'source-mirrors.cfg').exists())
        with patch.object(source,'mirror_configuration',return_value=b'checked config') as configure:
            path=worker.verified_mirror_config(registry,private,artifacts)
            configure.assert_called_once_with(b'original config')
        self.assertEqual(original.read_bytes(),b'original config')
        self.assertEqual(Path(path).read_bytes(),b'checked config')
        self.assertTrue(json.loads((artifacts/'worker-source-mirrors.json').read_text())['archive_integrity_unchanged'])
    def test_mirror_flag_applies_to_audit_and_build_after_original(self):
        self.spec['downloader_config']=str(self.root/'private/source-mirrors.cfg')
        original='--experimental_downloader_config=/registry/ci/source_mirrors.cfg'
        auditor=Mock()
        _,_,flags=worker.audit_before_build(self.spec,[*self.startup,'build',original,*self.spec['config']['build_targets']],auditor)
        expected=[original,'--experimental_downloader_config='+self.spec['downloader_config']]
        self.assertEqual([f for f in flags if f.startswith('--experimental_downloader_config=')],expected)
        self.assertEqual([f for f in auditor.call_args.args[1] if f.startswith('--experimental_downloader_config=')],expected)

    def test_distdirs_reach_audit_and_build_without_changing_targets(self):
        self.spec['distdirs']=[str(self.root/'distdir/ape-1.0.1'),str(self.root/'distdir/rules_coreutils-1.0.1')]
        auditor=Mock()
        startup,verb,flags=worker.audit_before_build(self.spec,[*self.startup,'build',*self.spec['config']['build_targets']],auditor)
        expected=['--distdir='+path for path in self.spec['distdirs']]
        self.assertEqual([f for f in flags if f.startswith('--distdir=')],expected)
        self.assertEqual([f for f in auditor.call_args.args[1] if f.startswith('--distdir=')],expected)
        self.assertEqual(auditor.call_args.args[2],self.spec['config']['build_targets']+self.spec['config']['test_targets'])

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
        mirror={'original_sha256':'a'*64,'configured_sha256':'b'*64,'archive_integrity_unchanged':True}
        (artifacts/'worker-source-mirrors.json').write_text(json.dumps(mirror))
        (artifacts/'build.log').write_text('private command environment')
        (artifacts/'test-results').mkdir();(artifacts/'test-results/test.xml').write_text('<testsuite tests="1"/>')
        workspace=self.root/'registry';(workspace/'ci').mkdir(parents=True)
        (workspace/'ci/archive_artifacts.sh').write_text('#!/bin/sh\nset -eu\ntar -cf "$2" -C "$1" .\n')
        worker.publish(NS(artifacts=artifacts,workspace=workspace))
        with tarfile.open(self.root/'registry-native-evidence.tar') as archive:
            names=archive.getnames()
            self.assertIn('./outputs/runtime.tar',names);self.assertIn('./test-results/test.xml',names)
            self.assertNotIn('./build.log',names)
            self.assertEqual(json.load(archive.extractfile('./worker-source-mirrors.json')),mirror)
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

    def test_workflow_consumer_enum_matches_fixed_profile_allowlist(self):
        trigger=self.flow.get('on',self.flow.get(True))
        self.assertEqual(trigger['workflow_dispatch']['inputs']['registry_consumer']['options'],
                         ['none',*worker.PROFILES])
        for name in worker.PROFILES:
            self.assertEqual([job for job,body in self.flow['jobs'].items()
                              if condition(str(body.get('if','true')),consumer=name)],['registry_native'])
    def test_existing_image_choices_and_jobs_remain(self):
        jobs=self.flow['jobs']
        self.assertEqual(set(jobs),{'registry_native','source_image_inputs','source_image','source_worker_preflight','worker_provenance','archive','checks','vs','vs_container'})
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
        for guard in guards:
            self.assertTrue(condition(guard,event='pull_request',branch='codex/libteam-registry-worker'))
            self.assertFalse(condition(guard,event='pull_request',branch='codex/libteam-registry-worker',head_repo='foreign/repo'))
            self.assertFalse(condition(guard,event='pull_request',branch='codex/libteam-registry-worker-extra'))
        self.assertFalse(condition(self.flow['jobs']['vs_container']['if'],event='pull_request',branch='codex/libteam-registry-worker'))
    def test_fixed_checkout_permissions_and_no_hosted_cache_dependency(self):
        job=self.flow['jobs']['registry_native'];self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        checkouts=[s for s in job['steps'] if s.get('uses','').startswith('actions/checkout@')]
        self.assertEqual(len(checkouts),2);self.assertTrue(all(s['with']['persist-credentials'] is False for s in checkouts))
        self.assertEqual(checkouts[1]['with']['repository'],'securely1g/sonic-bazel-registry')
        self.assertEqual(checkouts[1]['with']['ref'],'$'+'{{ inputs.source_revision }}')
        self.assertFalse(any('setup-bazel' in s.get('uses','') or 'actions/cache' in s.get('uses','') for s in job['steps']))
        download=[s for s in job['steps'] if 'download-artifact' in s.get('uses','')]
        self.assertEqual(len(download),1)
        self.assertEqual(download[0]['with']['name'],'bazel-repository-inputs')
        self.assertEqual(download[0]['with']['repository'],'securely1g/sonic-buildimage')
        self.assertEqual(download[0]['with']['path'],'DOLLAR{{ runner.temp }}/source-image-inputs'.replace('DOLLAR','$'))
        names=[s['name'] for s in job['steps']]
        self.assertLess(names.index('Verify the existing successful source-download producer'),names.index(download[0]['name']))
        self.assertLess(names.index(download[0]['name']),names.index('Run the original native registry build and tests'))
        self.assertEqual(download[0]['if'],"inputs.source_inputs_run != ''")


class LibteamWorkerTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='registry worker ');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.profile=copy.deepcopy(worker.PROFILES['libteam'])
        self.names=['libteam_runtime','libteam_runtime.debug_symbols','libteamdctl_runtime',
                    'libteamdctl_runtime.debug_symbols','libteam_utils_runtime','libteam_utils_runtime.debug_symbols',
                    'libteam_dev','runtime_packages.json','library_runtime_packages.json']
        self.targets=['@libteam//:'+name for name in self.names]
        self.private=self.root/'private';self.private.mkdir()
        self.artifacts=self.root/'artifacts';self.artifacts.mkdir()
        self.work=self.root/'work';(self.work/'consumer').mkdir(parents=True)
        self.output_base=self.work/'bazel/hash';self.execution_root=self.output_base/'execroot/_main'
        self.execution_root.mkdir(parents=True)
        self.script=self.output_base/'external/libteam+/bazel/verify_rebuild.py';self.script.parent.mkdir(parents=True)
        self.script.write_text('# selected owner fixture\n')
        self.config=self.script.parent/'config.h.in';self.config.write_bytes(b'original config')
        self.profile['probe_sha256']=worker.digest(self.script)
        self.flags=['--registry='+self.root.as_uri(),'--registry=https://bcr.bazel.build/',
                    '--platforms=@sonic_build_infra//platforms:x86_64_trixie','--lockfile_mode=update']
        self.startup=['--ignore_all_rc_files','--output_user_root='+str(self.work/'bazel')]
        self.spec={'binary':'/usr/bin/fake-bazel','work':str(self.work),'artifacts':str(self.artifacts),
            'repository_cache':str(self.root/'cache/repository'),'disk_cache':str(self.root/'cache/disk'),
            'profile':{'jobs':1,'cpu':1,'scheduler_memory_mb':4000},'config':{'build_targets':self.targets,
            'test_targets':['@libteam//:'+x for x in ['consumer_test','package_test','sdk_timestamp_test','verify_rebuild_test']]},
            'libteam_probe':str(self.private)}
        self.wrapper=self.root/'wrapper'
        self.wrapper.write_text('#!/usr/bin/python3\nimport json,sys\nopen('+repr(str(self.root/'adapter-command.json'))+
                                ',"w").write(json.dumps(sys.argv[1:]))\n')
        self.wrapper.chmod(0o755)
        self.spec_path=self.private/'spec.json'
        self.spec_path.write_text(json.dumps(self.spec))

    def freeze(self):
        auditor=Mock()
        worker.audit_before_build(self.spec,[*self.startup,'build',*self.flags,
            '--build_event_json_file='+str(self.artifacts/'build-events.jsonl'),*self.targets],auditor)
        return self.private/'ordinary-options.json'

    def test_option_record_is_frozen_and_rejects_later_drift(self):
        path=self.freeze();original=path.read_bytes()
        self.freeze();self.assertEqual(path.read_bytes(),original)
        with self.assertRaisesRegex(ValueError,'drifted'):
            worker.audit_before_build(self.spec,[*self.startup,'build',*self.flags,'--jobs=17',*self.targets],Mock())
        self.assertEqual(path.read_bytes(),original)
        self.spec['libteam_probe_active']=True
        for phase in worker.LIBTEAM_PHASES:
            auditor=Mock()
            _,_,flags=worker.audit_before_build(self.spec,[*self.startup,'build','--jobs=2',*self.flags,
                '--build_event_json_file='+str(self.private/'rebuild'/(phase+'-events.json')),*self.targets],auditor)
            self.assertEqual(flags[-3:],['--jobs=1','--local_resources=cpu=1','--local_resources=memory=4000'])
            self.assertEqual(auditor.call_args.kwargs['output'],self.artifacts/('libteam-no-deb-'+phase+'.json'))
        self.assertEqual(path.read_bytes(),original)
        for extra in [[],['--jobs=99'],['--jobs=2','--registry=https://unreviewed.invalid']]:
            with self.subTest(extra=extra),self.assertRaisesRegex(ValueError,'changed'):
                worker.audit_before_build(self.spec,[*self.startup,'build',*extra,*self.flags,
                    '--build_event_json_file='+str(self.private/'rebuild/baseline-events.json'),*self.targets],Mock())
        with self.assertRaisesRegex(ValueError,'phase'):
            worker.audit_before_build(self.spec,[*self.startup,'build','--jobs=2',*self.flags,
                '--build_event_json_file=/tmp/other-events.json',*self.targets],Mock())
        self.assertEqual(path.read_bytes(),original)

    def test_profile_mirror_bytes_and_cosmos_owner_helper_are_preserved(self):
        registry=self.root/'registry';(registry/'ci').mkdir(parents=True)
        mirror=registry/'ci/source_mirrors.cfg';mirror.write_bytes(b'fixed selected source mirrors\n')
        helper=registry/'ci/prepare_cosmos_distdir.py';helper.write_text('# maintained helper fixture\n')
        self.profile['source_mirrors_sha256']=worker.digest(mirror)
        self.profile['cosmos_helper_sha256']=worker.digest(helper)
        from tools.bazel.ci import source_image_worker
        with patch.object(source_image_worker,'mirror_configuration') as old:
            output=worker.verified_mirror_config(registry,self.private,self.artifacts,self.profile)
            old.assert_not_called()
        self.assertEqual(Path(output).read_bytes(),mirror.read_bytes())
        cache=self.root/'cache';cache.mkdir()
        with patch.object(subprocess,'run') as call:
            result=worker.libteam_cosmos(registry,self.profile,cache,self.artifacts)
        self.assertEqual(result,[str(cache/'cosmos-distdir')])
        self.assertEqual(call.call_args.args[0],[sys.executable,'-B',str(helper),'--distdir',str(cache/'cosmos-distdir'),
            '--cache-dir',str(cache/'cosmos-downloads'),'--repository-cache',str(cache/'repository'),
            '--receipt',str(self.artifacts/'cosmos-distdir.json')])
        mirror.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'mirror'):worker.verified_mirror_config(registry,self.private,self.artifacts,self.profile)
        helper.write_text('# corrupt')
        with patch.object(subprocess,'run') as call,self.assertRaisesRegex(ValueError,'helper'):
            worker.libteam_cosmos(registry,self.profile,cache,self.artifacts)
        call.assert_not_called()
        self.assertEqual(worker.libteam_cosmos(registry,worker.PROFILES['fips'],cache,self.artifacts),[])

    def owner_result(self):
        evidence=self.private/'rebuild';evidence.mkdir()
        output_dir=self.output_base/'execroot/_main/bazel-out';output_dir.mkdir()
        (self.artifacts/'outputs').mkdir()
        outputs={};index={}
        for name,label in zip(self.names,self.targets):
            path=output_dir/name;path.write_text('payload '+name)
            outputs[str(path)]=worker.digest(path)
            uploaded=self.artifacts/'outputs'/name;uploaded.write_bytes(path.read_bytes())
            index[label]=[{'path':'outputs/'+name,'size':uploaded.stat().st_size,'sha256':worker.digest(uploaded)}]
        (self.artifacts/'outputs.json').write_text(json.dumps(index))
        builds=[]
        for phase in worker.LIBTEAM_PHASES:
            events=evidence/(phase+'-events.json')
            events.write_text(json.dumps({'id':{'buildFinished':{}},'finished':{'overallSuccess':True,'exitCode':{'name':'SUCCESS'}}})+'\n')
            phase_outputs=copy.deepcopy(outputs)
            if phase=='config-edit':
                for name in self.names[4:6]+['runtime_packages.json']:
                    phase_outputs[str(output_dir/name)]='a'*64
            builds.append({'phase':phase,'seconds':0.1,'outputs_sha256':phase_outputs,'config_sha256':worker.digest(self.config),
                           'events':events.name,'events_sha256':worker.digest(events)})
        result={'status':'passed','source_restored':True,'targets':self.targets,'bazel_arguments':self.flags,
                'builds':builds,'owner_source_sha256':{'bazel/config.h.in':worker.digest(self.config)}}
        (evidence/'rebuild.json').write_text(json.dumps(result))
        return result

    def exercise(self,change=None,queries=None):
        self.freeze();calls=[]
        def query(command,**kwargs):
            calls.append(command)
            self.assertEqual(kwargs['cwd'],self.work/'consumer')
            self.assertEqual(command[0:3],[str(self.wrapper),*self.startup])
            if command[3]=='info':
                self.assertEqual(command[4:],[*self.flags,'output_base'])
                return (queries or {}).get('info',str(self.output_base)+'\n')
            self.assertEqual(command[3:],['cquery',*self.flags,worker.LIBTEAM_SCRIPT,'--output=files'])
            return (queries or {}).get('cquery',str(self.script)+'\n')
        def owner(command,**kwargs):
            calls.append(command)
            self.assertEqual(command,[sys.executable,'-B',str(self.script),'--bazel',str(self.private/'probe-bazel'),
                *['--bazel-arg='+x for x in self.flags],'--output',str(self.private/'rebuild')])
            self.assertEqual(kwargs['cwd'],self.work/'consumer')
            result=self.owner_result()
            if change:change(result)
            (self.private/'rebuild/rebuild.json').write_text(json.dumps(result))
            return NS(returncode=0)
        with patch.object(subprocess,'check_output',side_effect=query),patch.object(subprocess,'run',side_effect=owner):
            worker.libteam_incremental(self.profile,self.spec,self.spec_path,self.wrapper)
        return calls

    def test_selected_exported_probe_uses_real_adapter_and_bounded_public_receipt(self):
        calls=self.exercise();self.assertEqual(len(calls),3)
        receipt=json.loads((self.artifacts/'libteam-incremental.json').read_text())
        self.assertEqual(receipt['status'],'passed');self.assertEqual(len(receipt['builds']),4)
        self.assertTrue(receipt['original_and_restored_output_hashes_match_uploaded'])
        self.assertNotIn('commands',receipt);self.assertNotIn('file://',json.dumps(receipt))
        worker.safe_json(receipt)
        subprocess.run([str(self.private/'probe-bazel'),'info',*self.flags,'output_base'],check=True)
        self.assertEqual(json.loads((self.root/'adapter-command.json').read_text()),
                         [*self.startup,'info',*self.flags,'output_base'])

    def test_failed_owner_retains_restoration_status_without_success_claim(self):
        def fail(result):
            result['status']='failed'
            raise subprocess.CalledProcessError(1,['owner'])
        with self.assertRaises(subprocess.CalledProcessError):self.exercise(change=fail)
        result=json.loads((self.artifacts/'libteam-incremental.json').read_text())
        self.assertEqual(result['status'],'failed');self.assertTrue(result['source_restored'])

    def test_unrestored_owner_input_is_rejected_and_recorded(self):
        def changed(result):self.config.write_text('not restored')
        with self.assertRaisesRegex(ValueError,'restore'):self.exercise(change=changed)
        result=json.loads((self.artifacts/'libteam-incremental.json').read_text())
        self.assertEqual(result['status'],'failed');self.assertFalse(result['source_restored'])

    def test_missing_phase_evidence_fails_closed(self):
        def missing(result):(self.private/'rebuild/no-op-events.json').unlink()
        with self.assertRaisesRegex(ValueError,'evidence'):self.exercise(change=missing)

    def test_restored_output_must_match_the_ordinary_uploaded_bytes(self):
        def changed(result):
            index_path=self.artifacts/'outputs.json';index=json.loads(index_path.read_text())
            label=self.targets[0];payload=self.artifacts/index[label][0]['path'];payload.write_text('different uploaded payload')
            index[label][0].update(size=payload.stat().st_size,sha256=worker.digest(payload))
            index_path.write_text(json.dumps(index))
        with self.assertRaisesRegex(ValueError,'uploaded outputs'):self.exercise(change=changed)

    def test_wrong_or_ambiguous_selected_source_is_not_executed(self):
        with self.assertRaisesRegex(ValueError,'one selected'):
            self.exercise(queries={'cquery':str(self.script)+'\n'+str(self.script)+'\n'})

    def test_wrong_output_base_is_not_used(self):
        with self.assertRaisesRegex(ValueError,'escaped'):
            self.exercise(queries={'info':'/tmp/unrelated\n'})

    def test_corrupt_selected_probe_is_not_executed(self):
        self.script.write_text('# changed owner source\n')
        with self.assertRaisesRegex(ValueError,'reviewed source'):
            self.exercise()

    def test_same_bytes_outside_selected_owner_are_not_executed(self):
        outside=self.root/'verify_rebuild.py';outside.write_bytes(self.script.read_bytes())
        with self.assertRaisesRegex(ValueError,'reviewed source'):
            self.exercise(queries={'cquery':str(outside)+'\n'})


if __name__=='__main__': unittest.main()
