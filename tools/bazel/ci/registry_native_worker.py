#!/usr/bin/env python3
"""Explicit native registry consumers on the existing disposable dpool workers."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.source_worker_preflight import WORKERS, measure, marker_identity, select_build_profile, cpu_limit
from tools.bazel.ci.bazel_commands import audit_targets
from tools.bazel.ci.public_artifacts import safe_json, bep_summary, dependency_json

PROFILES = {
    'common': {'revision': '03f870c62fa2bab567133c6a9303f71392e4986e',
               'module': 'sonic-swss-common', 'version': '0.0.8-43598143ef4a46431eae0859915cd7d951dad1ed',
               'presubmit_sha256': '4c121e977fb3a316aca8ab1127cc66c42ca082e771744be0393995662dfa8694', 'builds': 16, 'tests': 24},
    'fips': {'revision': '8374b07ca138b98546122186d832544f1b46ac07',
             'module': 'sonic-fips', 'version': '0.0.5-15fcb926a302059ec379f0c2fd15eb3d88bda4c9',
             'presubmit_sha256': 'f4d7148c6dd8e8e5f1deda78bb0c878fa8fc4604181fce654a23d3d7e578a870', 'builds': 18, 'tests': 6},
    'sairedis': {'revision': 'dd35a932bfebdcb8f9c31c1127ed7496cf334df2',
         'module': 'sonic-sairedis',
         'version': '0.0.4-706cc3d1f6182dea18deed36a2ddb0fbcf1a59fc',
         'presubmit_sha256': '8d72692557b6d74bdc6a6c29ebc2af7eeb1ae3858006e8b7f38449ca04a723cc',
         'builds': 19,
         'tests': 7},
    'dash': {'revision': '28ea8a8b17f6ecf85a32aadf8ef5eb4f65ca9623',
         'module': 'sonic-dash',
         'version': '0.0.3-53e524a348589e2b2e0585a8c148bd9b2f8bc686',
         'presubmit_sha256': '41137940b8899634d4a36077fa40745d6a22b16145998963883162be5b56d58f',
         'builds': 4,
         'tests': 2},
    'linkmgrd': {'revision': 'b6b0b1bfcd784c7b2fa4bc7ada101c276d0258aa',
         'module': 'sonic-linkmgrd',
         'version': '1.0.0-803c4b170de1ebdf38d638517fd1115368ec9a5c',
         'presubmit_sha256': '6859c3c860387afeaf85752364bdf2992f933d80df31e0e39ea88243b6dd6efa',
         'builds': 4,
         'tests': 3},
}
# Populate only from verified provisioner provenance. A local image ID alone is
# not a registry image reference. Common remains fail-closed until one is known.
SLAVE_IMAGE_REFERENCES = {}
PROXY_NAMES = ('http_proxy','https_proxy','all_proxy','no_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')
CONTEXT_NAMES = ('GITHUB_EVENT_NAME','GITHUB_REPOSITORY','GITHUB_ACTOR','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','RUNNER_NAME','RUNNER_TEMP')


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    safe_json(value)
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def selection(consumer, revision, worker, environment):
    require(environment.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            environment.get('GITHUB_REPOSITORY') == 'securely1g/sonic-buildimage' and
            environment.get('GITHUB_ACTOR') == 'securely1g', 'Registry native tasks require an explicit owned dispatch')
    require(consumer in PROFILES and revision == PROFILES[consumer]['revision'], 'Unreviewed registry selection')
    require(worker in WORKERS and environment.get('RUNNER_NAME') == worker, 'Wrong dpool worker affinity')
    require(re.fullmatch(r'[1-9][0-9]*', environment.get('GITHUB_RUN_ID','')) and
            re.fullmatch(r'[1-9][0-9]*', environment.get('GITHUB_RUN_ATTEMPT','')), 'Missing dispatch identity')
    return PROFILES[consumer]


def clean_source(workspace, revision):
    require(subprocess.check_output(['git','-C',str(workspace),'rev-parse','HEAD'],text=True).strip() == revision,
            'Registry checkout differs from the fixed revision')
    require(not subprocess.check_output(['git','-C',str(workspace),'status','--porcelain'],text=True).strip(),
            'Registry checkout is not clean')


def registry_config(workspace, profile):
    path = workspace / 'modules' / profile['module'] / profile['version'] / 'presubmit.json'
    require(digest(path) == profile['presubmit_sha256'], 'Reviewed presubmit changed')
    spec = importlib.util.spec_from_file_location('selected_registry_ci', workspace / 'ci/registry_ci.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    config = runner.validate_entry(workspace, profile['module'], profile['version'])
    require(len(config['build_targets']) == profile['builds'] and len(config['test_targets']) == profile['tests'] and
            'amd64' in config['architectures'], 'Registry target inventory changed')
    return runner, config


def container_reference(config, marker):
    if not config.get('test_runtime_preparation'):
        return None
    reference = SLAVE_IMAGE_REFERENCES.get(marker['base_image'])
    require(isinstance(reference,str) and re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}',reference),
            'Common needs the actual provisioner-verified slave image reference; a local image ID is insufficient')
    return reference


def memory():
    root = Path('/sys/fs/cgroup')
    return {name: (root / name).read_text().strip() for name in
            ('memory.max','memory.current','memory.peak','memory.swap.max','memory.swap.current','memory.events','cpu.max')}


def child_environment(environment, workspace, cache):
    # Never pass Actions tokens, credential helpers, or the caller's complete environment.
    result = {k:environment[k] for k in (*PROXY_NAMES,*CONTEXT_NAMES) if k in environment}
    result.update(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                  HOME=str(cache/'home'), TMPDIR=str(cache/'tmp'), PYTHONDONTWRITEBYTECODE='1',
                  USE_BAZEL_VERSION='8.5.1', BAZELISK_HOME=str(cache/'bazelisk'),
                  GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(workspace),
                  GIT_CONFIG_KEY_1='safe.directory', GIT_CONFIG_VALUE_1=str(ROOT))
    return result


def command_parts(arguments):
    commands = {'build','test','aquery','cquery','info','shutdown','version'}
    positions = [i for i,x in enumerate(arguments) if x in commands]
    require(len(positions)==1, 'Expected one fixed Bazel subcommand')
    i=positions[0]
    return arguments[:i],arguments[i],arguments[i+1:]


def adjusted_bazel(spec, arguments):
    startup,command,options=command_parts(arguments)
    expected=['--ignore_all_rc_files','--output_user_root='+str(Path(spec['work'])/'bazel')]
    require(startup==expected, 'Registry runner startup/output confinement changed')
    launch=[spec['binary'],*startup,'--host_jvm_args=-Xmx3g',
            '--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts']
    if command in {'build','test','aquery','cquery'}:
        p=spec['profile']
        options += ['--repository_cache='+spec['repository_cache'],'--disk_cache='+spec['disk_cache'],
                    '--remote_cache=','--jobs='+str(p['jobs']),'--local_resources=cpu='+str(p['cpu']),
                    '--local_resources=memory='+str(p['scheduler_memory_mb'])]
    return launch,command,options


def audit_before_build(spec, arguments, auditor=None):
    auditor = auditor or audit_targets
    startup,command,options=adjusted_bazel(spec,arguments)
    if command not in {'build','test'}:
        return startup,command,options
    labels=[x for x in options if not x.startswith('--')]
    expected=spec['config'][command+'_targets']
    require(labels==expected, 'Bazel invocation differs from the complete original target list')
    flags=[x for x in options if x.startswith('--') and not x.startswith('--build_event_json_file=')]
    # These presentation/test flags do not select configured dependencies.
    flags=[x for x in flags if x!='--nocache_test_results' and not x.startswith('--test_output=')]
    auditor(startup[0],flags,spec['config']['build_targets']+spec['config']['test_targets'],
            workspace=Path.cwd(),output=Path(spec['artifacts'])/('no-deb-'+command+'.json'),startup_options=startup[1:])
    return startup,command,options


def bazel_main():
    spec=json.loads(Path(os.environ['REGISTRY_WORKER_SPEC']).read_text())
    startup,command,options=audit_before_build(spec,sys.argv[2:])
    os.execv(startup[0],[*startup,command,*options])


def prepare_tools():
    packages=('binutils','build-essential','ca-certificates','curl','gdb','git','libxml2','libzstd-dev',
              'patch','pkg-config','python3','tar','unzip','xz-utils','zlib1g-dev','zstd')
    missing=[]
    for package in packages:
        check=subprocess.run(['dpkg-query','-W','-f=${db:Status-Status}',package],capture_output=True,text=True)
        if check.returncode or check.stdout!='installed': missing.append(package)
    if missing:
        subprocess.run(['apt-get','update'],check=True)
        subprocess.run(['apt-get','install','-y','--no-install-recommends',*missing],check=True,
                       env={**os.environ,'DEBIAN_FRONTEND':'noninteractive'})
    for tool in ('gcc','g++','gdb','git','readelf','objcopy','pkg-config','patch','tar','unzip','xz','zstd'):
        require(shutil.which(tool), 'Missing native execution tool: '+tool)
    return missing


def cache_directory(cache, name):
    destination=cache/name
    require(destination.resolve()==destination and not destination.is_symlink(),
            'Registry cache descendant must not traverse symlinks')
    destination.mkdir(parents=True,exist_ok=True)
    require(destination.is_dir(), 'Registry cache descendant is not a directory')
    return destination


def server_process(pid):
    try:
        fields=Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()
    except FileNotFoundError:
        return None
    return {'state':fields[0], 'started':int(fields[19]), 'rss_pages':int(fields[21])}


def shutdown_consumer(wrapper,work):
    consumer=work/'consumer'
    records=list((work/'bazel').glob('*/server/server.pid.txt'))
    if not records:
        return {'status':'not_started','caches_and_outputs_retained':True}
    require(consumer.is_dir() and len(records)==1, 'Ambiguous registry consumer server')
    require(records[0].resolve().is_relative_to(work.resolve()), 'Server PID file escapes private work')
    pid=int(records[0].read_text().strip())
    before=server_process(pid)
    if before is None or (before['state']=='Z' and before['rss_pages']==0):
        return {'status':'already_stopped','pid':pid,'caches_and_outputs_retained':True}
    startup=['--ignore_all_rc_files','--output_user_root='+str(work/'bazel')]
    actual=int(subprocess.check_output([str(wrapper),*startup,'info','server_pid'],cwd=consumer,text=True).strip())
    require(actual==pid, 'Consumer command resolved a different Bazel server')
    result=subprocess.run([str(wrapper),*startup,'shutdown'],cwd=consumer)
    require(result.returncode in (0,36), 'Consumer shutdown command failed')
    after=server_process(pid)
    require(after is None or (after['started']==before['started'] and after['state']=='Z' and after['rss_pages']==0),
            'Registry consumer Bazel server remains alive')
    return {'status':'stopped','pid':pid,'before':before,'after':after,'exit_code':result.returncode,
            'caches_and_outputs_retained':True}


def execute(args, profile, cache):
    require(os.geteuid()==0, 'Registry consumer must use real container root')
    require(Path('/.dockerenv').is_file(), 'Registry consumer must stay in the disposable slave')
    marker_path=Path('/etc/sonic-slave-github-runner.json')
    marker_identity(marker_path)
    marker=json.loads(marker_path.read_text())
    runner,config=registry_config(args.workspace,profile)
    runner.verify_native('amd64')
    require('VERSION_CODENAME=trixie' in Path('/etc/os-release').read_text().splitlines(), 'Expected native Trixie')
    reference=container_reference(config,marker)
    if reference: runner.require_test_container(reference)
    for name in ('home','tmp','bazelisk','repository','disk'):
        cache_directory(cache,name)
    before=memory(); selected=select_build_profile(int(before['memory.max']),cpu_limit(before['cpu.max']))
    admitted=json.loads((args.artifacts/'worker-preflight.json').read_text())
    require(admitted['status']=='passed' and selected==admitted['build_profile'],
            'Worker resources changed after preflight')
    require(admitted['facts']['worker']==args.worker and admitted['facts']['slave']['base_image']==marker['base_image'],
            'Root consumer differs from measured slave identity')
    run_key=os.environ['GITHUB_RUN_ID']+'-'+os.environ['GITHUB_RUN_ATTEMPT']
    parent=cache_directory(cache,args.consumer)
    private=parent/run_key
    require(private.resolve()==private and not private.is_symlink(), 'Private run path must not traverse symlinks')
    private.mkdir(exist_ok=False)
    work=private/'work'
    spec={'binary':shutil.which('bazel'),'work':str(work),'artifacts':str(args.artifacts),
          'repository_cache':str(cache/'repository'),'disk_cache':str(cache/'disk'),'profile':selected,'config':config}
    require(spec['binary'], 'Worker must provide Bazel/Bazelisk')
    require(subprocess.check_output([spec['binary'],'--version'],text=True).strip()=='bazel 8.5.1','Expected Bazel8.5.1')
    spec_path=private/'spec.json';spec_path.write_text(json.dumps(spec))
    wrapper=private/'bazel-wrapper'
    wrapper.write_text('#!/usr/bin/python3\nimport os,sys\nos.execv(sys.executable,[sys.executable,"-B",'+repr(str(Path(__file__).resolve()))+',"bazel",*sys.argv[1:]])\n')
    wrapper.chmod(0o755)
    os.environ['REGISTRY_WORKER_SPEC']=str(spec_path)
    state={'status':'running','kind':'registry-native','registry_revision':profile['revision'],
           'module':profile['module'],'version':profile['version'],'architecture':'amd64',
           'worker':args.worker,'slave_image_id':marker['base_image'],'container_image_reference':reference,
           'dispatcher_revision':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
           'build_profile':selected,'memory_before':before,'cache_mode':'persistent_worker_local',
           'build_targets':config['build_targets'],'test_targets':config['test_targets']}
    started=time.monotonic()
    try:
        state['tools_installed']=prepare_tools()
        command=[sys.executable,'-B',str(args.workspace/'ci/registry_ci.py'),'run','--module',profile['module'],
                 '--version',profile['version'],'--architecture','amd64','--work-dir',str(work),
                 '--artifacts',str(args.artifacts),'--bazel',str(wrapper)]
        if reference: command += ['--test-container-image',reference]
        child=subprocess.Popen(command,cwd=args.workspace)
        while True:
            try:
                code=child.wait(timeout=30)
                require(code==0,'Ordinary registry runner failed')
                break
            except subprocess.TimeoutExpired:
                print(json.dumps({'kind':'registry-native','elapsed_seconds':round(time.monotonic()-started),'memory':memory()}),flush=True)
        clean_source(args.workspace,profile['revision'])
        after=memory()
        counts=lambda s:dict(line.split() for line in s.splitlines())
        require(all(counts(after['memory.events'])[key]==counts(before['memory.events'])[key] for key in ('oom','oom_kill')),
                'Registry validation had OOM events')
        state.update(status='passed',no_oom_events=True)
    except BaseException as error:
        state.update(status='failed',error_class=type(error).__name__)
        raise
    finally:
        try:
            state['server_shutdown']=shutdown_consumer(wrapper,work)
        except BaseException as error:
            state.update(status='failed',shutdown_error_class=type(error).__name__)
            raise
        finally:
            state.update(memory_after=memory(),elapsed_seconds=time.monotonic()-started)
            write(args.artifacts/'worker-registry.json',state)


def verify_outputs(artifacts):
    path=artifacts/'outputs.json'
    if not path.exists():
        return False  # Partial failed collection is private until it has an index.
    index=json.loads(path.read_text())
    require(isinstance(index,dict), 'Invalid native output index')
    declared=set()
    for records in index.values():
        require(isinstance(records,list), 'Invalid native output records')
        for item in records:
            relative=Path(item['path'])
            require(bool(relative.parts) and not relative.is_absolute() and '..' not in relative.parts and relative.parts[0]=='outputs',
                    'Output path escapes declared payloads')
            source=artifacts/relative
            require(not source.is_symlink() and source.resolve().is_relative_to((artifacts/'outputs').resolve()) and source.is_file(),
                    'Output is not a confined regular file')
            require(type(item['size']) is int and source.stat().st_size==item['size'] and digest(source)==item['sha256'],
                    'Native output size or hash differs')
            declared.add(relative.as_posix())
    actual={p.relative_to(artifacts).as_posix() for p in (artifacts/'outputs').rglob('*') if not p.is_dir()}
    require(actual==declared, 'Undeclared or missing native outputs')
    return True


def validate_registry_lock(value, workspace, revision):
    """Validate exact local registry URLs without changing retained lock bytes."""
    prefix=workspace.resolve().as_uri()+'/'
    public='https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/'+revision+'/'
    def checked(item):
        if isinstance(item,str) and item.startswith(prefix):
            url=urlsplit(item)
            target=Path(unquote(url.path))
            require(not url.query and not url.fragment and target.resolve().is_relative_to(workspace.resolve()),
                    'Local registry URL escapes selected checkout')
            relative=target.resolve().relative_to(workspace.resolve())
            require(relative.parts and relative.parts[0] in ('modules','bazel_registry.json'),
                    'Local registry URL is not registry metadata')
            return public+relative.as_posix()
        if isinstance(item,dict): return {checked(k):checked(v) for k,v in item.items()}
        if isinstance(item,list): return [checked(v) for v in item]
        return item
    dependency_json(checked(value),'lock')


def publish(args):
    """Use the ordinary registry transport, with private control/env outside it."""
    indexed_outputs=verify_outputs(args.artifacts)
    destination=args.artifacts/'public';destination.mkdir(exist_ok=True)
    for source in args.artifacts.iterdir():
        if source.name=='outputs' and not indexed_outputs: continue
        if source==destination or source.name in ('build-events.jsonl','test-events.jsonl','failed-actions','worker-publish.json') or source.suffix=='.log': continue
        require(not source.is_symlink(), 'Evidence root must not contain symlinks')
        if source.is_dir():
            # Registry output collector already confines/materializes these paths.
            require(source.name in ('outputs','test-results','testlogs'), 'Unexpected evidence directory')
            require(not any(item.is_symlink() for item in source.rglob('*')), 'Evidence subtree contains a symlink')
            shutil.copytree(source,destination/source.name,copy_function=shutil.copy2,symlinks=False)
        else:
            require(source.suffix in ('.json','.jsonl','.bazel','.lock') or source.name=='test-runtime-prepare.py', 'Unexpected evidence file')
            if source.suffix=='.json':
                safe_json(json.loads(source.read_text()))
            elif source.suffix=='.jsonl':
                require(source.name=='declared-output-trees.jsonl', 'Unexpected JSONL evidence')
                for line in source.read_text().splitlines(): safe_json(json.loads(line))
            elif source.suffix=='.lock':
                validate_registry_lock(json.loads(source.read_text()),args.workspace,args.revision)
            shutil.copy2(source,destination/source.name)
    summaries={name:bep_summary((args.artifacts/name).read_text().splitlines(),partial=True)
               for name in ('build-events.jsonl','test-events.jsonl') if (args.artifacts/name).exists()}
    write(destination/'build-summary.json',summaries)
    subprocess.run(['bash',str(args.workspace/'ci/archive_artifacts.sh'),str(destination),str(args.artifacts.parent/'registry-native-evidence.tar')],check=True)


def main():
    if len(sys.argv)>1 and sys.argv[1]=='bazel':
        return bazel_main()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('selection','run','execute','publish'))
    parser.add_argument('--consumer',required=True,choices=PROFILES)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--worker',required=True,choices=WORKERS)
    parser.add_argument('--workspace',required=True,type=Path)
    parser.add_argument('--artifacts',required=True,type=Path)
    args=parser.parse_args()
    profile=selection(args.consumer,args.revision,args.worker,os.environ)
    require(args.workspace.is_absolute() and args.workspace.resolve().is_relative_to('/actions/_work'), 'Invalid worker checkout path')
    require(args.artifacts.is_absolute() and args.artifacts.resolve().is_relative_to('/actions/_work') and
            not args.artifacts.resolve().is_relative_to(args.workspace.resolve()), 'Invalid artifact path')
    if args.phase=='selection': return
    args.artifacts.mkdir(parents=True,exist_ok=True)
    phase_receipt={'status':'running','kind':'registry-native','phase':args.phase,
                   'consumer':args.consumer,'registry_revision':profile['revision'],'worker':args.worker}
    phase_path=args.artifacts/('worker-'+args.phase+'.json')
    write(phase_path,phase_receipt)
    try:
        clean_source(args.workspace,profile['revision'])
        cache=Path('/bazel_cache/registry-native')/args.worker
        require(cache.resolve()==cache,'Registry cache must not traverse symlinks')
        if args.phase=='publish': return publish(args)
        if args.phase=='execute': return execute(args,profile,cache)
        preflight=measure(args.worker)
        args.artifacts.mkdir(parents=True,exist_ok=True)
        write(args.artifacts/'worker-preflight.json',preflight)
        require(preflight['status']=='passed','Worker resource/isolation preflight failed')
        require(os.getuid()==os.getgid()==1000,'Outer worker control must remain UID/GID1000')
        _,config=registry_config(args.workspace,profile)
        marker=json.loads(Path('/etc/sonic-slave-github-runner.json').read_text())
        container_reference(config,marker)  # Fail before sudo/APT if Common identity is unavailable.
        environment=child_environment(os.environ,args.workspace,cache)
        command=['sudo','-n','/usr/bin/env','-i',*[k+'='+v for k,v in environment.items()],
                 sys.executable,'-B',str(Path(__file__).resolve()),'execute',*sys.argv[2:]]
        subprocess.run(command,check=True)

    except BaseException as error:
        phase_receipt.update(status='failed',error_class=type(error).__name__)
        raise
    finally:
        if phase_receipt['status']=='running': phase_receipt['status']='passed'
        write(phase_path,phase_receipt)



if __name__=='__main__':
    main()
