#!/usr/bin/env python3
"""Qualify the fixed Scapy successor with the maintained native registry runner."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys

MODULE = 'scapy'
VERSION = '2.6.1.sonic.3'
BUILD = ['@scapy//:wheel']
TEST = ['@scapy//:wheel_test', '@scapy//:upstream_test']


def require(value, message):
    if not value:
        raise ValueError(message)



def sha(raw):
    return hashlib.sha256(raw).hexdigest()



def exact(path, identity):
    require(path.is_file() and not path.is_symlink() and
            path.stat().st_size == identity['bytes'] <= 2 * 1024**2, 'Owner input type or size differs')
    raw = path.read_bytes()
    require(len(raw) == identity['bytes'] and sha(raw) == identity['sha256'], 'Owner input hash differs')
    return raw



def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write('\n')



def load_owner(name, path, identity):
    exact(path, identity)
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, 'Owner module unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def plan_data(path, digest):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536, 'Plan bound differs')
    raw = path.read_bytes()
    require(sha(raw) == digest, 'Fixed qualification plan differs')
    plan = json.loads(raw)
    require(plan['module'] == MODULE and plan['version'] == VERSION and plan['architecture'] == 'amd64' and
            plan['build_targets'] == BUILD and plan['test_targets'] == TEST, 'Qualification scope differs')
    require(re.fullmatch('[0-9a-f]{40}', plan['registry_revision']) is not None,
            'Reviewed registry candidate revision is still pending')
    return plan



def operator():
    keys = ('GITHUB_REPOSITORY', 'GITHUB_EVENT_NAME', 'GITHUB_ACTOR', 'GITHUB_TRIGGERING_ACTOR',
            'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'RUNNER_NAME')
    value = {key: os.environ[key] for key in keys}
    require(value['GITHUB_REPOSITORY'] == 'securely1g/sonic-buildimage' and
            value['GITHUB_EVENT_NAME'] == 'workflow_dispatch' and
            value['GITHUB_ACTOR'] == value['GITHUB_TRIGGERING_ACTOR'] == 'securely1g' and
            value['GITHUB_JOB'] == 'vs_scapy_registry_qualification' and
            re.fullmatch(r'ks-[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?', value['RUNNER_NAME']) and
            re.fullmatch('[0-9a-f]{40}', value['GITHUB_SHA']) and
            all(re.fullmatch('[1-9][0-9]*', value[key]) for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')),
            'Actual operator identity differs')
    return value



def prepare(plan, source, native, registry, scratch, archives, mirrors, support):
    support.source_check(registry, plan['registry_revision'])
    for relative, identity in plan['registry_files'].items():
        exact(registry / relative, identity)
    support.prepare_cache(plan, source, native, scratch, archives, mirrors)
    worker = sys.modules['tools.bazel.ci.source_image_worker']
    resources = json.loads((scratch / 'resources.json').read_bytes())
    profile = resources['selected_profile']
    binary = shutil.which('bazel', path='/usr/local/bin:/usr/bin:/bin') or shutil.which(
        'bazelisk', path='/usr/local/bin:/usr/bin:/bin')
    require(binary is not None, 'Qualified installed launcher disappeared')
    binary = str(Path(binary).resolve(strict=True))
    configuration = worker.remote_cache_configuration(source)
    require((scratch / 'native-bazel').read_text() == worker.launcher_text(
        binary, scratch / 'output-base', profile, configuration), 'Prepared launcher configuration changed')
    launchers = {}
    bases = {'pass-' + str(number): scratch / ('pass-' + str(number)) / 'work/bazel/output-base'
             for number in (1,)}
    for name, output_base in bases.items():
        path = scratch / (name + '-bazel')
        text = worker.launcher_text(binary, output_base, profile, configuration)
        with path.open('x') as output:
            output.write(text)
        path.chmod(0o700)
        launchers[name] = {'bytes': len(text.encode()), 'sha256': sha(text.encode())}
    distdirs = sorted((scratch / 'cache-setup/distdir').iterdir())
    require(len(distdirs) == 5 and all(path.is_dir() and not path.is_symlink() for path in distdirs),
            'Verified source distdirs differ')
    write(scratch / 'setup.json', {'operator': operator(), 'registry_revision': plan['registry_revision'],
          'launchers': launchers, 'profile': profile, 'distdirs': [str(path) for path in distdirs],
          'home_sha256': sha(os.environ['HOME'].encode()), 'disk_cache': False, 'repository_cache': False,
          'source_revision': plan['source_revision'], 'native_revision': plan['native_revision']})



def command_parts(command, launcher, work):
    require(command[0] == str(launcher), 'Different fixed Bazel launcher')
    matches = [(i, word) for i, word in enumerate(command) if word in ('build', 'test')]
    require(len(matches) == 1, 'Unexpected original runner command')
    index, kind = matches[0]
    require(command[1:index] == ['--ignore_all_rc_files', '--output_user_root=' + str(work / 'bazel'), '--batch'],
            'Different fixed startup arguments')
    targets = BUILD if kind == 'build' else TEST
    require(command[-len(targets):] == targets, 'Different declared targets')
    options = command[index + 1:-len(targets)]
    require(not any(item.startswith('--execution_log_') for item in options), 'Execution log override')
    return command[:index], kind, options, targets



def safe_options(options):
    return [item for item in options if not item.startswith(('--build_event_json_file=', '--execution_log_'))]

def retain_originals(scratch, artifacts, completed):
    """Retain genuine records and test outputs, including original raw campaign XML."""
    destination = scratch / 'reports'
    destination.mkdir()
    records = {}
    for name in ('validation.json', 'source-validation.json', 'outputs.json',
                 'test-results.json', 'test-results-error.json',
                 'build-action-audit.json', 'test-action-audit.json'):
        path = artifacts / name
        if path.exists():
            require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024**2,
                    'Original result type or bound differs')
            raw = path.read_bytes()
            with (destination / name).open('xb') as output:
                output.write(raw)
            records[name] = {'bytes': len(raw), 'sha256': sha(raw)}
    for name in ('test-results', 'testlogs'):
        directory = artifacts / name
        if not directory.exists():
            continue
        require(directory.is_dir() and not directory.is_symlink(), 'Original test directory type differs')
        paths = list(directory.rglob('*'))
        require(len(paths) <= 256 and all(not path.is_symlink() and
                (path.is_file() or path.is_dir()) for path in paths), 'Original test output shape differs')
        files = [path for path in paths if path.is_file()]
        require(sum(path.stat().st_size for path in files) <= 32 * 1024**2 and
                all(path.stat().st_size <= 16 * 1024**2 for path in files), 'Original test output size differs')
        for path in files:
            relative = path.relative_to(artifacts)
            raw = path.read_bytes()
            copied = destination / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            with copied.open('xb') as output:
                output.write(raw)
            records[relative.as_posix()] = {'bytes': len(raw), 'sha256': sha(raw)}
    write(destination / 'originals.json', {'schema': 1, 'operator': operator(),
          'original_runner_completed': completed, 'files': records,
          'scope': TEST, 'retention_is_not_test_success': True})


def run(plan, source, registry, scratch, support):
    support.source_check(source, plan['source_revision'])
    support.source_check(registry, plan['registry_revision'])
    for relative, identity in plan['registry_files'].items():
        exact(registry / relative, identity)
    runner = load_owner('fixed_registry_ci', registry / 'ci/registry_ci.py', plan['registry_runner'])
    runner.sync_module(registry, MODULE, VERSION, check=True)
    config = runner.validate_entry(registry, MODULE, VERSION)
    require(config['architectures'] == ['amd64'] and config['build_targets'] == BUILD and
            config['test_targets'] == TEST and config['consumer_deps'] == [] and
            config.get('generated_module_overlay') is True and
            set(config) == {'architectures', 'build_targets', 'test_targets', 'consumer_deps',
                            'platforms', 'host_platforms', 'generated_module_overlay'},
            'Declared Scapy qualification changed')
    audit = load_owner('fixed_action_audit', source / 'tools/bazel/ci/bazel_commands.py', plan['action_audit'])
    setup = json.loads((scratch / 'setup.json').read_bytes())
    require(setup['operator'] == operator() and setup['registry_revision'] == plan['registry_revision'] and
            setup['home_sha256'] == sha(os.environ['HOME'].encode()), 'Prepared actual context changed')
    arguments = ['--repository_cache=', '--disk_cache=', '--incompatible_strict_action_env',
                 '--experimental_downloader_config=' + str(scratch / 'source-mirrors.cfg'),
                 *['--distdir=' + path for path in setup['distdirs']]]
    work, artifacts = scratch / 'pass-1/work', scratch / 'pass-1/private'
    launcher = scratch / 'pass-1-bazel'
    exact(launcher, setup['launchers']['pass-1'])
    original_run = runner.run_logged
    captured = {}
    completed = False

    def logged(command, cwd, log):
        prefix, kind, options, targets = command_parts(command, launcher, work)
        require(kind not in captured, 'Unexpected repeated original runner phase')
        captured[kind] = list(command)
        audit.audit_targets(str(launcher), safe_options(options), targets, workspace=cwd,
                            output=artifacts / (kind + '-action-audit.json'), startup_options=prefix[1:])
        original_run(command, cwd, log)

    runner.run_logged = logged
    try:
        runner.run(registry, MODULE, VERSION, 'amd64', work, artifacts, bazel=str(launcher),
                   bazel_startup_args=['--batch'], bazel_build_args=arguments)
        require(set(captured) == {'build', 'test'}, 'Original functional phases incomplete')
        completed = True
    finally:
        runner.run_logged = original_run
        retain_originals(scratch, artifacts, completed)
        support.source_check(registry, plan['registry_revision'])
        require(setup['home_sha256'] == sha(os.environ['HOME'].encode()), 'Actual HOME changed')
    write(scratch / 'run-result.json', {'status': 'ORIGINAL_SCAPY_RUNNER_PASSED',
          'operator': operator(), 'registry_revision': plan['registry_revision'],
          'module': MODULE, 'version': VERSION, 'architecture': 'amd64',
          'build_targets': BUILD, 'passed_tests': TEST, 'original_registry_inputs_unchanged': True})


def verify(plan, native, scratch, support):
    support.source_check(native, plan['native_revision'])
    for relative, identity in plan['native_files'].items():
        exact(native / relative, identity)
    setup = json.loads((scratch / 'setup.json').read_bytes())
    result = json.loads((scratch / 'run-result.json').read_bytes())
    require(result['operator'] == setup['operator'] == operator() and
            result['registry_revision'] == setup['registry_revision'] == plan['registry_revision'] and
            result['status'] == 'ORIGINAL_SCAPY_RUNNER_PASSED' and result['passed_tests'] == TEST,
            'Original runner did not pass this qualification')
    report = json.loads((scratch / 'reports/validation.json').read_bytes())
    require(report['module'] == MODULE and report['version'] == VERSION and
            report['architecture'] == report['execution_architecture'] == 'amd64' and
            report['cross_compile'] is False and report['passed_tests'] == TEST and
            report['build_targets'] == BUILD and report['fetched_module_matches_registry'] is True,
            'Original native result scope differs')
    retained = json.loads((scratch / 'reports/originals.json').read_bytes())
    require(retained['original_runner_completed'] is True and retained['operator'] == operator(),
            'Original report retention incomplete')
    for relative, identity in retained['files'].items():
        path = scratch / 'reports' / relative
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == identity['bytes'] and
                sha(path.read_bytes()) == identity['sha256'], 'Retained original changed')
    require(any('/upstream_test/' in name and
                (name.endswith('/upstream-xunit.xml') or name.endswith('/outputs.zip'))
                for name in retained['files']), 'Original upstream case output missing')
    require(setup['home_sha256'] == sha(os.environ['HOME'].encode()), 'Actual HOME changed')
    write(scratch / 'reports/qualification.json', {'schema': 1, 'status': 'SCAPY_AMD64_QUALIFICATION_PASSED',
          'module': MODULE, 'version': VERSION, 'architecture': 'amd64', 'operator': operator(),
          'registry_revision': plan['registry_revision'], 'source_revision': plan['source_revision'],
          'native_revision': plan['native_revision'], 'build_targets': BUILD, 'passed_tests': TEST,
          'disk_cache': False, 'repository_cache': False, 'HOME_unchanged': True,
          'full_VS_admission': False})




def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('prepare', 'run', 'verify'))
    for name in ('plan', 'source', 'native', 'registry', 'scratch'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--archives', type=Path)
    parser.add_argument('--mirrors', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    plan = plan_data(args.plan, args.plan_sha256)
    require(args.scratch.is_dir() and args.scratch.resolve() == args.scratch and os.getuid() == 1000,
            'Expected fresh private K3s scratch')
    require(os.environ.get('USE_BAZEL_VERSION') == '8.5.1' and
            Path(os.environ['BAZELISK_HOME']).resolve().is_relative_to(args.scratch),
            'Expected the fixed Bazel version and disposable launcher directory')
    support = load_owner('qualified_cache_setup', Path(__file__).with_name('build_vs_swss_installed_policy.py'),
                         plan['setup_owner'])
    if args.phase == 'prepare':
        require(args.archives is not None and args.mirrors is not None, 'Missing original setup inputs')
        prepare(plan, args.source, args.native, args.registry, args.scratch, args.archives, args.mirrors, support)
    elif args.phase == 'run':
        run(plan, args.source, args.registry, args.scratch, support)
    else:
        verify(plan, args.native, args.scratch, support)



if __name__ == '__main__':
    main()
