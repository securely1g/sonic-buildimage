"""Run PINS source tests and its original installed-package probe against the VS-produced TARs."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from tools.bazel.ci import artifact_validation as check
from tools.bazel.ci import p4rt_common_sdk, p4rt_handoff
from tools.bazel.ci.python_packages import test_result

INSTALLED = '//p4rt_app:runtime_package_test'
PACKAGE_TARGETS = {'//p4rt_app:' + name + suffix for name in ('p4rt_runtime', 'p4rt_runtime_debug')
                   for suffix in ('', '_contract_template')}


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def source_tests(targets, expected):
    """Every discovered label must run, with the installed probe executed against transferred files."""
    check.require(sorted(targets) == expected and targets.count(INSTALLED) == 1 and
                  len(set(targets)) == len(targets) and
                  all(target.startswith(('//p4rt_app:', '//p4rt_app/')) for target in targets),
                  'P4RT test targets differ from the VS producer')
    native = sorted(set(targets) - {INSTALLED})
    check.require(native, 'Missing P4RT source tests')
    return native


def native_tests(bazel, options, tests, output, private):
    # Audit the exact source tests requested below before Bazel executes them.
    graph = private / 'actions.json'
    with graph.open('w') as stream:
        bazel(['aquery', *options, '--output=jsonproto', '--include_artifacts', '--include_commandline=false',
               'deps(set(' + ' '.join(tests) + '))'], stdout=stream)
    with graph.open() as stream:
        actions = json.load(stream)
    audit = p4rt_common_sdk.audit_graph(actions)
    check.require(not any(target['label'] in PACKAGE_TARGETS for target in actions['targets']) and
                  not audit['action_counts'].get('PackageTar'), 'P4RT source tests would rebuild production packages')
    del actions
    write(output / 'test-action-audit.json', audit)
    events = private / 'events.jsonl'
    bazel(['test', *options, '--nocache_test_results', '--test_output=errors', '--nozip_undeclared_test_outputs',
           '--build_event_json_file=' + str(events), *tests])
    statuses = {}
    with events.open() as stream:
        for line in stream:
            event = json.loads(line)
            if 'testSummary' in event:
                statuses[event['id']['testSummary']['label']] = event['testSummary']['overallStatus']
    check.require(set(statuses) == set(tests) and all(s == 'PASSED' for s in statuses.values()),
                  'Every P4RT source test must execute and pass')
    logs = Path(bazel(['info', *options, 'bazel-testlogs'], stdout=subprocess.PIPE).stdout.strip())
    for target in tests:
        relative = Path(target.removeprefix('//').replace(':', '/')) / 'test.xml'
        destination = output / 'test-results' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        xml = logs / relative
        check.require(test_result(xml)['skipped'] == 0 and not ET.parse(xml).findall('.//skipped'),
                      'P4RT source test contains a skipped case: ' + target)
        shutil.copyfile(xml, destination)
    write(output / 'test-results.json', statuses)


def run(args, sdk, output, runtime):
    source = args.owner_source.resolve()
    owner = p4rt_handoff.owner_api(source, args.owner_revision)
    built = owner.verify_build(output, sdk, args.owner_revision)
    runtime_env, proof = owner.runtime_environment(runtime, sdk)
    clean = {key: value for key, value in os.environ.items() if key not in ('LD_PRELOAD', 'LD_AUDIT', 'LD_LIBRARY_PATH')}
    command = [args.owner_bazel, '--output_base=' + str(args.owner_output_base.resolve())]

    def bazel(arguments, **kwargs):
        return subprocess.run([*command, *arguments], cwd=source, env=clean | {'USE_BAZEL_VERSION': '6.4.0'},
                              check=True, text=True, **kwargs)

    override = '--override_repository=sonic_swss_common=' + str(sdk)
    runner = [sys.executable, '-B', str(source / 'ci/runtime_run_under.py'), '--sdk', str(sdk),
              '--receipt', str(runtime), '--receipt-sha256', proof['receipt_sha256'], '--']
    options = ['--config=p4rt-runtime', override, '--jobs=' + str(args.jobs), '--run_under=' + shlex.join(runner),
               *['--test_env=' + key + '=' + value for key, value in runtime_env.items()]]
    try:
        version = bazel(['version', '--gnu_format'], stdout=subprocess.PIPE).stdout
        check.require([line for line in version.splitlines() if line.startswith('bazel ')] == ['bazel 6.4.0'],
                      'P4RT source tests require the producer Bazel version')
        targets = sorted(bazel(['query', override, 'tests(//p4rt_app/...)', '--output=label'],
                               stdout=subprocess.PIPE).stdout.splitlines())
        tests = source_tests(targets, built['test_targets'])
        (output / 'test-targets.txt').write_text('\n'.join(targets) + '\n')
        with tempfile.TemporaryDirectory(prefix='p4rt-tests-', dir=output.parent) as temporary:
            private = Path(temporary)
            native_tests(bazel, options, tests, output, private)
            # PINS' run_under distinguishes its installed Python probe by executable
            # name. Keep its loader and environment handling, using the original script.
            launcher = private / 'runtime_package_test'
            probe = source / 'p4rt_app/runtime_package_test.py'
            launcher.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-B', str(probe)]) + ' "$@"\n')
            launcher.chmod(0o755)
            subprocess.run([*runner, str(launcher), '--runtime', output / 'p4rt_runtime.tar',
                            '--debug', output / 'p4rt_runtime_debug.tar', '--common-library', sdk / 'lib/libswsscommon.so.0',
                            '--runtime-contract', output / 'p4rt_runtime_contract.json',
                            '--debug-contract', output / 'p4rt_runtime_debug_contract.json'],
                           cwd=source, env=clean | runtime_env | {'TEST_UNDECLARED_OUTPUTS_DIR': temporary}, check=True)
            report = private / 'installed-packages.json'
            owner.verify_installed(json.loads(report.read_text()), built, sdk)
            owner.verify_runtime_unchanged(runtime, sdk, proof)
            shutil.copyfile(report, output / 'installed-packages.json')
        p4rt_handoff.source_identity(source, args.owner_revision)
        owner.verify_build(output, sdk, args.owner_revision)
    finally:
        bazel_env = clean | {'USE_BAZEL_VERSION': '6.4.0'}
        subprocess.run([*command, 'shutdown'], cwd=source, env=bazel_env, check=False)
