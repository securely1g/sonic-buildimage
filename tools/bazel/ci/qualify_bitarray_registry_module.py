#!/usr/bin/env python3
"""Delegate fixed AMD64 bitarray qualification to the existing registry runner."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys

MODULE = 'bitarray'
VERSION = '2.8.1.sonic.1'
BUILD = ['@bitarray//:whl', '@bitarray//:pkg', '//:installed_layer']
TEST = ['@bitarray//:upstream_test', '@bitarray//:wheel_contract_test', '//:installed_wheel_test']
COMMENT = b'\n/* Disposable declared-header invalidation check. */\n'
HEADER = 'bitarray/bitarray.h'
CPP = {'bitarray/_bitarray.cpython-313-x86_64-linux-gnu.so': 'bitarray/_bitarray.c',
       'bitarray/_util.cpython-313-x86_64-linux-gnu.so': 'bitarray/_util.c'}
LOG_LIMIT = 64 * 1024**2


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
            value['GITHUB_JOB'] == 'vs_bitarray_registry_qualification' and
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
             for number in (1, 2)}
    bases.update({phase: scratch / 'pass-2/work/bazel' / (phase + '-output-base')
                  for phase in ('copy-baseline', 'header-edited', 'restored')})
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


def graph_labels(text):
    # The original command logger retains stderr beside query output.
    labels = [line.strip().split(' ', 1)[0] for line in text.splitlines()
              if line.strip().startswith(('@', '//')) and '//' in line]
    require(labels and len(labels) <= 20000, 'Dependency query count differs')
    repositories = sorted({label.split('//', 1)[0] for label in labels if label.startswith('@')})
    require(not any('pip' in repo.lower() and 'bitarray' in repo.lower() for repo in repositories),
            'Repository-time pip bitarray is reachable')
    return {'target_count': len(labels), 'repositories': repositories,
            'repository_time_pip_bitarray_reachable': False, 'scope': BUILD + TEST}


def output_pair(artifacts):
    outputs = json.loads((artifacts / 'outputs.json').read_bytes())
    result = {}
    for target in ('@bitarray//:whl', '//:installed_layer'):
        rows = outputs[target]
        require(len(rows) == 1 and re.fullmatch('[0-9a-f]{64}', rows[0]['sha256']), 'Declared output identity differs')
        result[target] = {'sha256': rows[0]['sha256'], 'bytes': rows[0]['size']}
    return result


def source_copy(original, destination, identities):
    require(not destination.exists(), 'Preserve prior disposable source copy')
    rows = list(original.rglob('*'))
    require(len(rows) <= 1000 and all(not path.is_symlink() and (path.is_dir() or path.is_file()) for path in rows)
            and sum(path.stat().st_size for path in rows if path.is_file()) <= 16 * 1024**2,
            'Fetched source copy exceeds the reviewed shape')
    for relative, identity in identities.items():
        exact(original / relative, identity)
    shutil.copytree(original, destination)
    for relative, identity in identities.items():
        exact(destination / relative, identity)


@contextmanager
def changed_header(path):
    original = path.read_bytes()
    try:
        path.write_bytes(original + COMMENT)
        yield
    finally:
        path.write_bytes(original)


def retain_phase(scratch, phase, artifacts, outputs):
    destination = scratch / 'reports' / phase
    destination.mkdir(parents=True)
    for name in ('validation.json', 'source-validation.json', 'outputs.json', 'test-results.json',
                 'build-action-audit.json', 'test-action-audit.json', 'action-audit.json', 'supported-graph.json'):
        path = artifacts / name
        if path.exists():
            require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024**2,
                    'Original small result shape differs')
            with (destination / name).open('xb') as output:
                output.write(path.read_bytes())
    write(destination / 'phase.json', {'phase': phase, 'outputs': outputs,
          'full_qualification_passed': False, 'original_operator': operator()})


def run(plan, source, registry, scratch, support):
    support.source_check(source, plan['source_revision'])
    support.source_check(registry, plan['registry_revision'])
    for relative, identity in plan['registry_files'].items():
        exact(registry / relative, identity)
    runner = load_owner('fixed_registry_ci', registry / 'ci/registry_ci.py', plan['registry_runner'])
    audit = load_owner('fixed_action_audit', source / 'tools/bazel/ci/bazel_commands.py', plan['action_audit'])
    setup = json.loads((scratch / 'setup.json').read_bytes())
    require(setup['operator'] == operator() and setup['registry_revision'] == plan['registry_revision'] and
            setup['home_sha256'] == sha(os.environ['HOME'].encode()), 'Prepared actual context changed')
    arguments = ['--repository_cache=', '--disk_cache=', '--incompatible_strict_action_env',
                 '--experimental_downloader_config=' + str(scratch / 'source-mirrors.cfg'),
                 *['--distdir=' + path for path in setup['distdirs']]]
    original_run = runner.run_logged
    phases, outputs = {}, {}
    for number in (1, 2):
        root = scratch / ('pass-' + str(number))
        work, artifacts = root / 'work', root / 'private'
        launcher = scratch / ('pass-' + str(number) + '-bazel')
        exact(launcher, setup['launchers']['pass-' + str(number)])
        captured = {}

        def logged(command, cwd, log):
            prefix, kind, options, targets = command_parts(command, launcher, work)
            require(kind not in captured, 'Unexpected repeated original runner phase')
            captured[kind] = list(command)
            audit.audit_targets(str(launcher), safe_options(options), targets,
                                workspace=cwd, output=artifacts / (kind + '-action-audit.json'),
                                startup_options=prefix[1:])
            if kind == 'build':
                dependency_log = artifacts / 'dependencies.private.log'
                original_run([*prefix, 'cquery', *safe_options(options), '--output=label',
                              'deps(set(' + ' '.join(BUILD + TEST) + '))'], cwd, dependency_log)
                require(dependency_log.stat().st_size <= 8 * 1024**2, 'Dependency query output bound')
                write(artifacts / 'supported-graph.json', graph_labels(dependency_log.read_text()))
            execution = artifacts / (kind + '-execution.private.json')
            original_run([*command, '--execution_log_json_file=' + str(execution)], cwd, log)
            phases['pass' + str(number) + '-' + kind] = str(execution.relative_to(scratch))

        runner.run_logged = logged
        try:
            runner.run(registry, MODULE, VERSION, 'amd64', work, artifacts, bazel=str(launcher),
                       bazel_startup_args=['--batch'], bazel_build_args=arguments)
        finally:
            runner.run_logged = original_run
        require(set(captured) == {'build', 'test'}, 'Original functional phases incomplete')
        outputs['pass' + str(number)] = output_pair(artifacts)
        retain_phase(scratch, 'pass' + str(number), artifacts, outputs['pass' + str(number)])
        if number == 2:
            prefix, _, options, _ = command_parts(captured['build'], launcher, work)
            fetched = runner.fetched_modules(work / 'bazel/output-base', MODULE)
            require(len(fetched) == 1 and fetched[0].resolve().is_relative_to(work), 'Fetched module escapes fresh work')
            copied = work / 'qualification-source'
            source_copy(fetched[0].parent, copied, plan['upstream_inputs'])
            header = copied / HEADER
            original_header = header.read_bytes()

            def rebuild(label):
                destination = root / label
                destination.mkdir()
                phase_launcher = scratch / (label + '-bazel')
                exact(phase_launcher, setup['launchers'][label])
                phase_prefix = [str(phase_launcher), *prefix[1:]]
                flags = [*safe_options(options), '--override_module=bitarray=' + str(copied)]
                audit.audit_targets(str(phase_launcher), flags, BUILD, workspace=work / 'consumer',
                                    output=destination / 'action-audit.json', startup_options=phase_prefix[1:])
                execution = destination / 'execution.private.json'
                events = destination / 'build-events.jsonl'
                original_run([*phase_prefix, 'build', *flags, '--build_event_json_file=' + str(events),
                              '--execution_log_json_file=' + str(execution), *BUILD], work / 'consumer',
                             destination / 'build.private.log')
                trees = runner.query_output_trees(phase_prefix, flags, BUILD, work / 'consumer', destination)
                execution_root = (work / 'consumer/bazel-out').resolve(strict=True).parent
                require(execution_root.is_relative_to(work), 'Bazel execution root escapes fresh work')
                runner.collect_outputs(events, BUILD, work, destination, trees, execution_root)
                phases[label] = str(execution.relative_to(scratch))
                outputs[label] = output_pair(destination)
                retain_phase(scratch, label, destination, outputs[label])

            rebuild('copy-baseline')
            with changed_header(header):
                rebuild('header-edited')
            require(header.read_bytes() == original_header, 'Disposable source was not restored')
            rebuild('restored')
    require(outputs['pass1'] == outputs['pass2'] == outputs['copy-baseline'] == outputs['restored'],
            'Unchanged-source declared outputs differ')
    require(outputs['header-edited']['@bitarray//:whl'] != outputs['copy-baseline']['@bitarray//:whl'],
            'Packaged header change did not change the wheel')
    support.source_check(registry, plan['registry_revision'])
    require(setup['home_sha256'] == sha(os.environ['HOME'].encode()), 'Actual HOME changed')
    write(scratch / 'run-result.json', {'status': 'REAL_RUNNER_TESTS_PASSED_LOG_PROOF_PENDING',
          'phases': phases, 'outputs': outputs, 'operator': operator(),
          'original_registry_inputs_unchanged': True, 'disposable_header_restored': True})


def compile_evidence(raw, parser, expected_header, inputs, require_hit=False):
    require(0 < len(raw) <= LOG_LIMIT, 'Execution log bound differs')
    decoder, text, offset, count, selected = parser.json_decoder(), raw.decode('utf-8'), 0, 0, {}
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        record, offset = decoder.raw_decode(text, offset)
        count += 1
        require(type(record) is dict and count <= 20000, 'Execution log record shape differs')
        label = parser.proto_field(record, 'target_label', '')
        if record.get('mnemonic') != 'CppCompile' or not isinstance(label, str):
            continue
        match = re.fullmatch(r'@@?bitarray(?:\+[^/]*)?//:(.+)', label)
        if match is None or match[1] not in CPP:
            continue
        target = match[1]
        require(target not in selected and parser.proto_field(record, 'exit_code', 0) == 0 and
                record.get('status', '') == '', 'Repeated or failed C compile action')
        hit = parser.proto_field(record, 'cache_hit', False)
        require(type(hit) is bool and (not require_hit or hit) and
                (record.get('runner') == 'remote cache hit' if hit else
                 record.get('runner') in ('linux-sandbox', 'processwrapper-sandbox', 'local')),
                'Selected C action did not use the required cache/runner')
        digest = record.get('digest', {}).get('hash', '')
        require(re.fullmatch('[0-9a-f]{64}', digest), 'Missing C action digest')
        actual_inputs = record.get('inputs', [])
        for relative, expected in ((HEADER, expected_header), (CPP[target], inputs[CPP[target]]['sha256'])):
            rows = [row for row in actual_inputs if row.get('path', '').endswith('/' + relative)]
            require(len(rows) == 1 and rows[0].get('digest', {}).get('hash') == expected,
                    'Declared original C/header input differs')
        paths = [row.get('path', '') for row in parser.proto_field(record, 'actual_outputs', [])]
        require(paths and any(path.endswith('.o') for path in paths) and
                all(path.startswith('bazel-out/') and '..' not in Path(path).parts for path in paths),
                'Selected C output paths differ')
        selected[target] = {'action_sha256': digest, 'cache_hit': hit, 'runner': record['runner'],
                            'outputs': sorted(paths), 'header_sha256': expected_header}
    require(set(selected) == set(CPP), 'Missing two real C compile actions')
    return {'actions': selected, 'execution_log_sha256': sha(raw), 'execution_log_bytes': len(raw)}


def verify(plan, native, scratch, support):
    support.source_check(native, plan['native_revision'])
    for relative, identity in plan['native_files'].items():
        exact(native / relative, identity)
    exact(native / 'tools/bazel/ci/remote_cache_smoke.py', plan['execution_reader'])
    parser = support.import_from(native, 'tools.bazel.ci.remote_cache_smoke')
    result = json.loads((scratch / 'run-result.json').read_bytes())
    setup = json.loads((scratch / 'setup.json').read_bytes())
    require(result['operator'] == setup['operator'] == operator(), 'Actual operator changed')
    original = plan['upstream_inputs'][HEADER]['sha256']
    proofs = {}
    for phase in ('pass1-build', 'pass2-build', 'copy-baseline', 'header-edited', 'restored'):
        expected = plan['edited_header_sha256'] if phase == 'header-edited' else original
        raw = parser.read_under(scratch, result['phases'][phase], LOG_LIMIT)
        proofs[phase] = compile_evidence(raw, parser, expected, plan['upstream_inputs'],
                                        require_hit=phase in ('pass2-build', 'restored'))
    for target in CPP:
        a = proofs['pass1-build']['actions'][target]['action_sha256']
        b = proofs['pass2-build']['actions'][target]['action_sha256']
        c = proofs['copy-baseline']['actions'][target]['action_sha256']
        d = proofs['header-edited']['actions'][target]['action_sha256']
        e = proofs['restored']['actions'][target]['action_sha256']
        require(a == b and c == e and c != d, 'Declared-source action digest invalidation/restoration differs')
    report = {'schema': 1, 'status': 'FIXED_BITARRAY_AMD64_QUALIFICATION_PASSED', 'module': MODULE,
              'version': VERSION, 'architecture': 'amd64', 'operator': operator(),
              'registry_revision': plan['registry_revision'], 'setup_source_revision': plan['source_revision'],
              'native_setup_owner': plan['native_revision'], 'source_archive_sha256': plan['source_archive_sha256'],
              'full_original_suites': 2, 'outputs': result['outputs'], 'compile_evidence': proofs,
              'declared_header_edit_restored': True, 'native_ELF_change_required': False,
              'disk_cache': False, 'repository_cache': False, 'HOME_unchanged': True,
              'buildimage_consumer_graph_and_full_VS_admission': False}
    write(scratch / 'reports/qualification.json', report)


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
