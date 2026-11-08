#!/usr/bin/env python3
"""Guard fixed registry workers without running Bazel or package installers."""
import ast
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
    def test_pi_keeps_the_complete_reviewed_build_and_runtime_test(self):
        profile=worker.PROFILES['pi']
        self.assertEqual(profile,{
            'revision':'261bdc36d08b90b5ad568b199c377903277644ae',
            'module':'p4lang-pi','version':'0.1.3.sonic.3',
            'presubmit_sha256':'74b9da56345cc46fa0f0bba6f0ede910c23dab46f9a2f85eb2e4b7a9e0c8558d',
            'builds':4,'tests':1})
        self.assertEqual(self.selection(consumer='pi',revision=profile['revision']),profile)
        builds=['@p4lang-pi//:runtime','@p4lang-pi//:runtime.debug_symbols',
                '@p4lang-pi//:sdk','@p4lang-pi//:runtime_packages.json']
        tests=['@p4lang-pi//:runtime_test']
        self.spec['config']={'build_targets':builds,'test_targets':tests}
        for verb,targets in [('build',builds),('test',tests)]:
            auditor=Mock()
            _,actual,flags=worker.audit_before_build(self.spec,[*self.startup,verb,*targets],auditor)
            self.assertEqual(actual,verb)
            self.assertEqual([flag for flag in flags if not flag.startswith('--')],targets)
            self.assertEqual(auditor.call_args.args[2],builds+tests)
            with self.assertRaisesRegex(ValueError,'complete original target list'):
                worker.audit_before_build(self.spec,[*self.startup,verb,*targets[:-1]],auditor)
            self.assertEqual(auditor.call_count,1)
    def test_dash_keeps_the_complete_reviewed_build_and_runtime_tests(self):
        profile=worker.PROFILES['dash']
        self.assertEqual(profile,{
            'revision':'39726e4b38d77e462a9481adef898c35e5cf4daa',
            'module':'sonic-dash','version':'0.0.3-53e524a348589e2b2e0585a8c148bd9b2f8bc686',
            'presubmit_sha256':'663edbc36d3d30e55bb048eba34e3a0aaa47903f86cbd9b66ef76a67fef85310',
            'builds':4,'tests':2})
        self.assertEqual(self.selection(consumer='dash',revision=profile['revision']),profile)
        builds=['@sonic-dash//:runtime','@sonic-dash//:runtime.debug_symbols',
                '@sonic-dash//:sdk','@sonic-dash//:runtime_packages.json']
        tests=['@sonic-dash//bazel:runtime_test','@sonic-dash//bazel:make_flags_test']
        self.spec['config']={'build_targets':builds,'test_targets':tests}
        for verb,targets in [('build',builds),('test',tests)]:
            auditor=Mock()
            _,actual,flags=worker.audit_before_build(self.spec,[*self.startup,verb,*targets],auditor)
            self.assertEqual(actual,verb)
            self.assertEqual([flag for flag in flags if not flag.startswith('--')],targets)
            self.assertEqual(auditor.call_args.args[2],builds+tests)
            with self.assertRaisesRegex(ValueError,'complete original target list'):
                worker.audit_before_build(self.spec,[*self.startup,verb,*targets[:-1]],auditor)
            self.assertEqual(auditor.call_count,1)
    def test_child_environment_excludes_credentials(self):
        result=worker.child_environment({**self.environment,'GH_TOKEN':'private','ACTIONS_RUNTIME_TOKEN':'private',
            'GIT_CONFIG_GLOBAL':'private','HTTP_PROXY':'http://private.invalid','OTHER':'private'},Path('/actions/_work/registry'),Path('/bazel_cache/registry-native/w'))
        self.assertFalse({'GH_TOKEN','ACTIONS_RUNTIME_TOKEN','GIT_CONFIG_GLOBAL','OTHER'} & result.keys())
        self.assertEqual(result['HTTP_PROXY'],'http://private.invalid')
        self.assertEqual(result['GIT_CONFIG_VALUE_0'],'/actions/_work/registry')
        self.assertEqual(result['GIT_CONFIG_VALUE_1'],str(ROOT))
        self.assertEqual(result['USE_BAZEL_VERSION'],'8.5.1')
    def test_pi_reader_token_is_not_in_native_environment(self):
        result=worker.child_environment({**self.environment,'GITHUB_TOKEN':'fixture-token'},
            self.root/'registry',self.root/'cache')
        self.assertNotIn('GITHUB_TOKEN',result)
    def pi_rule_fixture(self):
        directory=self.root/'registry-native-rule-inputs';directory.mkdir()
        manifest=self.root/'control/bazel-public-inputs/ci/github_sources/sonic-buildimage-rule-inputs.json'
        manifest.parent.mkdir(parents=True);manifest.write_text('{"schema_version":1,"sources":[]}')
        receipt=self.root/'registry-native-rule-sources.json'
        value={'schema_version':1,'manifest_sha256':worker.digest(manifest),'sources':[],'files':[]}
        receipt.write_text(json.dumps(value))
        return directory,receipt,value
    def test_pi_rule_receipt_binds_manifest_and_preserves_cache_flags(self):
        directory,receipt,value=self.pi_rule_fixture()
        with patch.dict(os.environ,{'RUNNER_TEMP':str(self.root)}),patch.object(worker,'ROOT',self.root/'control'):
            for consumer in ('pi','dash'):
                with self.subTest(consumer=consumer):
                    self.assertEqual(worker.verified_pi_rule_inputs(consumer),(str(directory),value))
            self.assertIsNone(worker.verified_pi_rule_inputs('fips'))
        self.spec['distdirs']=[str(directory)]
        auditor=Mock()
        _,_,flags=worker.audit_before_build(self.spec,[*self.startup,'build',*self.spec['config']['build_targets']],auditor)
        self.assertIn('--distdir='+str(directory),flags)
        self.assertIn('--repository_cache='+self.spec['repository_cache'],flags)
        self.assertIn('--disk_cache='+self.spec['disk_cache'],flags)
        self.assertIn('--distdir='+str(directory),auditor.call_args.args[1])
    def test_pi_rule_receipt_rejects_missing_malformed_private_and_wrong_manifest(self):
        directory,receipt,value=self.pi_rule_fixture()
        with patch.dict(os.environ,{'RUNNER_TEMP':str(self.root)}),patch.object(worker,'ROOT',self.root/'control'):
            for text in ['{',json.dumps({**value,'manifest_sha256':'0'*64}),
                         json.dumps({**value,'env':{'TOKEN':'secret'}}),' '*65537]:
                receipt.write_text(text)
                for consumer in ('pi','dash'):
                    with self.subTest(consumer=consumer,text=text[:32]),self.assertRaises((ValueError,TypeError)):
                        worker.verified_pi_rule_inputs(consumer)
            receipt.unlink()
            for consumer in ('pi','dash'):
                with self.subTest(consumer=consumer),self.assertRaises(ValueError):worker.verified_pi_rule_inputs(consumer)
            receipt.symlink_to(self.root/'other')
            for consumer in ('pi','dash'):
                with self.subTest(consumer=consumer),self.assertRaises(ValueError):worker.verified_pi_rule_inputs(consumer)
            receipt.unlink();receipt.write_text(json.dumps(value))
            directory.rmdir();directory.symlink_to(self.root,target_is_directory=True)
            for consumer in ('pi','dash'):
                with self.subTest(consumer=consumer),self.assertRaises(ValueError):worker.verified_pi_rule_inputs(consumer)
    def test_registry_mirror_phase_uses_shared_public_config_and_receipt_contract(self):
        from tools.bazel.ci import source_image_worker as shared
        # Execute the unchanged mirror-phase dispatch from main, excluding native phases.
        main=next(n for n in ast.parse((ROOT/'tools/bazel/ci/registry_native_worker.py').read_text()).body
                  if isinstance(n,ast.FunctionDef) and n.name=='main')
        branch=next(n for n in main.body if isinstance(n,ast.If) and
                    ast.unparse(n.test)=="args.phase in ('prepare-mirrors', 'scan-mirrors', 'cleanup-mirrors')")
        module=ast.Module(body=[ast.FunctionDef(name='delegate',args=ast.arguments(
            posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),
            body=[copy.deepcopy(branch)],decorator_list=[])],type_ignores=[])
        code=compile(ast.fix_missing_locations(module),'<actual-registry-mirror-delegation>','exec')
        original=(ROOT/'tools/bazel/tests/fixtures/source-mirrors-99779640.cfg').read_bytes()
        for corrupt,private_root in [(False,''),(False,'https://fixture.example.invalid/debian/'),(True,'')]:
            with self.subTest(corrupt=corrupt,private=bool(private_root)),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);artifacts=root/'artifacts';artifacts.mkdir()
                public=root/'control/bazel-public-inputs/ci/source_mirrors.cfg'
                public.parent.mkdir(parents=True);public.write_bytes(original+(b'corrupt' if corrupt else b''))
                private=Mock();private.MIRROR_ROOTS=(('TEST_MIRROR_ROOT','unused'),)
                selected=[private_root] if private_root else []
                private.selected_mirrors.return_value=selected
                expected=shared.mirror_configuration(original).decode()
                private.configuration.return_value=expected+'# composed by private fixture\n'
                def write_config(path,data):
                    path.write_text(data);path.chmod(0o600)
                private.write_config.side_effect=write_config
                env={**self.environment,'RUNNER_TEMP':str(root),'TEST_MIRROR_ROOT':private_root}
                namespace={**worker.__dict__,'args':NS(phase='prepare-mirrors',artifacts=artifacts)}
                with patch.object(shared,'ROOT',root/'control'),patch.object(sys,'path',list(sys.path)), \
                        patch.dict(sys.modules,{'private_mirrors':private}),patch.dict(os.environ,env,clear=True), \
                        patch.object(shared.platform,'machine',return_value='x86_64'), \
                        patch.object(shared.platform,'freedesktop_os_release',return_value={'VERSION_CODENAME':'trixie'}):
                    exec(code,namespace)
                    if corrupt:
                        with self.assertRaisesRegex(ValueError,'checksum mismatch'):namespace['delegate']()
                        private.write_config.assert_not_called()
                        self.assertFalse(worker.registry_mirror_path().exists())
                        self.assertFalse((artifacts/'worker-source-mirrors.json').exists())
                    else:
                        namespace['delegate']()
                        result=worker.verified_mirror_config(artifacts)
                        self.assertEqual(result,str(worker.registry_mirror_path()))
                        receipt=json.loads((artifacts/'worker-source-mirrors.json').read_text())
                        self.assertEqual(receipt,{'original_sha256':shared.MIRROR_SHA,'archive_integrity_unchanged':True})
                        self.assertEqual(Path(result).read_text(),private.configuration.return_value if private_root else expected)
                        if private_root:private.configuration.assert_called_once_with(expected,selected)
                        else:private.configuration.assert_not_called()
                    self.assertIn(str(root/'control/.source-mirror-registry/ci'),sys.path)
                    self.assertNotIn('TEST_MIRROR_ROOT',os.environ)

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
    def test_worker_uses_the_prepared_registry_downloader(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        with patch.dict(os.environ, {'RUNNER_TEMP': str(self.root), 'GITHUB_RUN_ID': '11', 'GITHUB_RUN_ATTEMPT': '1'}):
            with self.assertRaisesRegex(ValueError, 'prepared registry downloader'):
                worker.verified_mirror_config(artifacts)
            path = worker.registry_mirror_path()
            path.write_text('prepared registry downloader')
            path.chmod(0o600)
            self.assertEqual(worker.verified_mirror_config(artifacts), str(path))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'prepared registry downloader'):
                worker.verified_mirror_config(artifacts)

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
    def test_fixed_checkout_permissions_and_no_hosted_cache_dependency(self):
        job=self.flow['jobs']['registry_native'];self.assertEqual(job['permissions'],{'contents':'read','actions':'read'})
        checkouts=[s for s in job['steps'] if s.get('uses','').startswith('actions/checkout@')]
        self.assertEqual(len(checkouts),4);self.assertTrue(all(s['with']['persist-credentials'] is False for s in checkouts))
        self.assertEqual(checkouts[1]['with']['repository'],'securely1g/sonic-bazel-registry')
        self.assertEqual(checkouts[1]['with']['ref'],'c41c7baf15307ad1c8f94d5dca511ef952d28729')
        self.assertEqual(checkouts[2]['with']['repository'],'securely1g/sonic-bazel-registry')
        self.assertEqual(checkouts[2]['with']['ref'],'997796400cc3eefc1f0590b227cd9b74c2141424')
        self.assertEqual(checkouts[2]['with']['path'],'control/bazel-public-inputs')
        self.assertIs(checkouts[2]['with']['sparse-checkout-cone-mode'],False)
        self.assertEqual(checkouts[2]['with']['sparse-checkout'].split(),[
            'ci/github_sources.py','ci/github_sources/sonic-buildimage-rule-inputs.json','ci/source_mirrors.cfg'])
        self.assertEqual(checkouts[3]['with']['ref'],'$'+'{{ inputs.source_revision }}')
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
    def test_pi_and_dash_preparation_is_token_scoped_and_fail_closed_before_native(self):
        job=self.flow['jobs']['registry_native'];steps=job['steps']
        step=next(s for s in steps if s.get('name')=='Prepare verified PI and DASH rule inputs')
        native=next(s for s in steps if s.get('id')=='native')
        self.assertLess(steps.index(step),steps.index(native))
        self.assertEqual(step['if'],"inputs.registry_consumer == 'pi' || inputs.registry_consumer == 'dash'")
        for consumer in [*worker.PROFILES,'none','unknown']:
            self.assertEqual(condition(step['if'],consumer=consumer),consumer in ('pi','dash'))
        self.assertEqual(step['env'],{'GITHUB_TOKEN':'$'+'{{ github.token }}'})
        self.assertNotIn('GITHUB_TOKEN',job['env']);self.assertNotIn('env',native)
        self.assertNotIn('continue-on-error',step);self.assertNotIn('if',native)
        command=step['run']
        self.assertIn('--repository-cache "/bazel_cache/registry-native/$EXPECTED_WORKER/repository"',command)
        self.assertIn('--distdir "$RUNNER_TEMP/registry-native-rule-inputs"',command)
        self.assertIn('--github-token-env GITHUB_TOKEN',command)
        upload=next(s for s in steps if 'upload-artifact@' in s.get('uses',''))
        self.assertEqual(upload['with']['path'],'$'+'{{ runner.temp }}/registry-native-upload/')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);binary=root/'bin';binary.mkdir();trace=root/'trace.json'
            fake=binary/'python3'
            fake.write_text('#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n'
                'Path(os.environ["TRACE"]).write_text(json.dumps({"argv":sys.argv[1:],"token_present":bool(os.environ.get("GITHUB_TOKEN"))}))\n'
                'sys.exit(int(os.environ["FAKE_EXIT"]))\n')
            fake.chmod(0o755)
            for code in (0,27):
                env={'PATH':str(binary),'RUNNER_TEMP':str(root),'EXPECTED_WORKER':'sonic-slave-lgh114-1',
                     'GITHUB_TOKEN':'fixture-token','TRACE':str(trace),'FAKE_EXIT':str(code)}
                result=subprocess.run(['/bin/bash','-e','-c',command],env=env,cwd=root,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,code)
                observed=json.loads(trace.read_text());self.assertTrue(observed['token_present'])
                self.assertIn('/bazel_cache/registry-native/sonic-slave-lgh114-1/repository',observed['argv'])
                self.assertEqual(observed['argv'][-2:],['--github-token-env','GITHUB_TOKEN'])


if __name__=='__main__': unittest.main()
