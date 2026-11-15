#!/usr/bin/env python3
"""Retain four genuine owner-test outcomes and a separate auxiliary installation."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse

HELPER_SHA = '90742f5704f634585290602893d58e704f5fad3b73ee46165758e4f984bc3de8'
CONTROLLER_SHA = '1ede9fe34763907665b13fd3b03abcfb1965661f22eed266d073b6f7de657920'
AUXILIARY_SHA = 'c9346693da1be654cf7f19e3781d1cdda7b59f4618e03425fb69f91e9a25af34'
FIXTURE_SHA = 'd85273a6a17d467a1f7012221489cf357799cd34b16910cd5313b95b2fcbf2eb'
PUBLIC_SHA = '69354ce956cb2d21bad917911c562092eb0dc5fd9945e10344b2dda723f562d4'
TARGETS = (
    '//tools/bazel/tests:sonic_python_wheels_test',
    '//tools/bazel/tests:sonic_yang_mgmt_wheel_test',
    '//tools/bazel/tests:supervisor_python_wheel_test',
    '@scapy//:upstream_test',
)
WHEELS = {
    'common': '//src/sonic-py-common:sonic_py_common_wheel',
    'config': '//src/sonic-config-engine:sonic_config_engine_wheel',
    'yang': '//src/sonic-yang-mgmt:sonic_yang_mgmt_wheel',
    'supervisor': '//src/sonic-supervisord-utilities:sonic_supervisord_utilities_wheel',
}
MAX_FILE = 128 * 1024**2
RUNTIME_EXPRESSION = (
    'json.encode({"label":str(target.label),'
    '"executable":providers(target)["DefaultInfo"].files_to_run.executable.path,'
    '"manifest":providers(target)["DefaultInfo"].files_to_run.runfiles_manifest.path})'
)


def require(value, message):
    if not value:
        raise ValueError(message)


def identity(path):
    raw = path.read_bytes()
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def load_module(path, digest, name):
    require(path.is_file() and not path.is_symlink() and identity(path)['sha256'] == digest,
            'Different fixed diagnostic owner: ' + name)
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def local_output(value, execution_root, owned_root):
    require(isinstance(value, str) and value and '\\' not in value and
            '\n' not in value and '\r' not in value and '\0' not in value,
            'Invalid configured output path')
    relative = PurePosixPath(value)
    require(not relative.is_absolute() and str(relative) == value and
            all(part not in ('', '.', '..') for part in relative.parts),
            'Configured output must be relative')
    path = (execution_root / value).resolve(strict=True)
    require(path.is_relative_to(owned_root) and path.is_file(), 'Configured output escapes job')
    return path


def runtime_rows(raw, public, execution_root, owned_root):
    result = {}
    for line in raw.decode('utf-8').splitlines():
        row = public.load_json(line)
        require(isinstance(row, dict) and set(row) == {'label', 'executable', 'manifest'},
                'Different runtime provider metadata')
        label = row['label']
        require(isinstance(label, str), 'Invalid runtime label')
        if label.startswith('@@//'):
            label = label[2:]
        require(label in TARGETS[:3] and label not in result, 'Different or duplicate runtime target')
        result[label] = {
            key: local_output(row[key], execution_root, owned_root)
            for key in ('executable', 'manifest')}
    require(set(result) == set(TARGETS[:3]), 'Incomplete runtime provider metadata')
    return result


def selected_runfiles(raw, owned_root):
    """Read only two ordinary manifest entries; escaped/ambiguous selections fail."""
    selected = {'python': [], 'installer': []}
    for line in raw.decode('utf-8').splitlines():
        # Selected logical names contain no spaces or escapes. Other manifest
        # records are retained verbatim and are not interpreted by this reader.
        if line.startswith(' '):
            require('/site-packages/installer/__init__.py' not in line and
                    '/bin/python3 ' not in line, 'Escaped selected runfile is unsupported')
            continue
        logical, separator, physical = line.partition(' ')
        kind = None
        if re.fullmatch(r'rules_python[+A-Za-z0-9_.~-]*/bin/python3', logical):
            kind = 'python'
        elif re.fullmatch(r'[A-Za-z0-9_.+~-]+/site-packages/installer/__init__\.py', logical):
            kind = 'installer'
        if kind is None:
            continue
        require(separator and physical and '\0' not in physical, 'Missing selected runfile')
        path = Path(physical)
        require(path.is_absolute(), 'Selected runfile is not absolute')
        resolved = path.resolve(strict=True)
        require(resolved.is_relative_to(owned_root) and resolved.is_file(),
                'Selected runfile escapes job')
        selected[kind].append((logical, path, resolved))
    require(all(len(rows) == 1 for rows in selected.values()), 'Ambiguous selected runtime')
    return {name: rows[0] for name, rows in selected.items()}


def test_evidence(events, labels, owned_root, public):
    """Select genuine local BEP outputs; never infer a sandbox output filename."""
    result, outputs, finished = {}, {}, []
    messages = events.read_text().splitlines()
    require(messages, 'Missing test BEP')
    for number, line in enumerate(messages):
        event = public.load_json(line)
        if event.get('lastMessage'):
            require(number == len(messages) - 1, 'BEP lastMessage is not final')
        if 'finished' in event:
            finished.append(event['finished'].get('exitCode', {}).get('code', 0))
        test = event.get('id', {}).get('testResult')
        if not test:
            continue
        label = labels.get(test.get('label'))
        require(label in TARGETS, 'Unexpected test result')
        require(label not in result, 'Repeated configured test result')
        value = event.get('testResult', {})
        status = value.get('status')
        require(status in ('PASSED', 'FAILED', 'TIMEOUT', 'INCOMPLETE', 'REMOTE_FAILURE',
                           'FAILED_TO_BUILD', 'TOOL_HALTED_BEFORE_TESTING'),
                'Unknown test result status')
        result[label] = status
        for item in value.get('testActionOutput', []):
            name = item.get('name')
            if name not in ('test.log', 'test.xml', 'test.outputs/validation.json',
                            'test.outputs/wheels.json', 'test.outputs/upstream-xunit.xml'):
                continue
            uri = urlparse(item.get('uri', ''))
            require(uri.scheme == 'file' and not uri.netloc and not uri.query and not uri.fragment,
                    'Test evidence is not a local URI')
            path = Path(unquote(uri.path)).resolve(strict=True)
            require(path.is_relative_to(owned_root) and path.is_file(),
                    'Test evidence escapes job')
            key = (label, name)
            require(key not in outputs, 'Repeated test output')
            outputs[key] = path
    require(public.load_json(messages[-1]).get('lastMessage') is True and
            len(finished) == 1 and type(finished[0]) is int, 'Incomplete test BEP')
    require(set(result) == set(TARGETS), 'Not all four tests produced results')
    return result, outputs, finished[0]


def installer_manifest(root):
    records = []
    for package in ('installer', 'installer-0.7.0.dist-info'):
        directory = root / package
        require(directory.is_dir() and not directory.is_symlink(), 'Missing installer package')
        for path in sorted(directory.rglob('*')):
            if path.is_dir():
                require(not path.is_symlink(), 'Installer directory is a symlink')
                continue
            require(path.resolve(strict=True).is_relative_to(root) and path.is_file(),
                    'Installer leaf escapes selected package')
            records.append({'relative_name': path.relative_to(root).as_posix(), **identity(path)})
            require(len(records) <= 128 and sum(row['bytes'] for row in records) <= 16 * 1024**2,
                    'Installer inventory exceeds bound')
    return {'root': str(root), 'version': '0.7.0', 'files': records}


def capture_registered(captures, output, retain, result):
    complete = True
    for name, path in captures:
        if (output / name).exists():
            continue
        try:
            require(path.is_file(), 'Registered original is missing')
            retain(name, path)
        except (OSError, ValueError) as error:
            complete = False
            result['status'] = 'failed'
            result['files'][name] = {'retained': False, 'error_type': type(error).__name__}
    return complete


def run(root):
    control, source, scratch = [root / name for name in ('control', 'source', 'scratch')]
    helper = load_module(control / 'tools/bazel/ci/build_vs_coherent_installed_policies.py',
                         HELPER_SHA, '_owner_diagnostic_cohort')
    auxiliary_path = control / 'tools/bazel/ci/auxiliary_installed_wheel_diagnostic.py'
    auxiliary = load_module(auxiliary_path, AUXILIARY_SHA, '_owner_diagnostic_auxiliary')
    plan = helper.load_plan(control / 'tools/bazel/ci/vs_coherent_installed_policy_inputs.json')
    helper.environment_check(plan, scratch, source)
    helper.setup.source_check(source, plan['source_revision'])
    orch = helper.setup.import_from(source, 'tools.bazel.ci.orchagent_image')
    for name in ('build', 'cache_reuse', 'command_log', 'public_artifacts', 'resolution',
                 'artifact_validation', 'bazel_commands', 'syncd_image'):
        importlib.import_module('tools.bazel.ci.' + name)
    importlib.import_module('tools.bazel.gzip.source_archive')
    logs = importlib.import_module('tools.bazel.ci.command_log')
    public = importlib.import_module('tools.bazel.ci.public_artifacts')
    syncd = importlib.import_module('tools.bazel.ci.syncd_image')
    require(identity(Path(public.__file__))['sha256'] == PUBLIC_SHA, 'Different public owner')
    owner_path = control / 'tools/bazel/ci/container.py'
    owner = load_module(owner_path, CONTROLLER_SHA, '_owner_diagnostic_container')
    for key, value in tuple(sys.modules.items()):
        if key.startswith('tools.') and getattr(value, '__file__', None):
            require(Path(value.__file__).resolve().is_relative_to(source), 'Mixed product imports')
    require(owner.public_artifacts is public and owner.command_log is logs, 'Mixed owner imports')
    private, output = scratch / 'owner-test-diagnostic', root / 'owner-diagnostic-output'
    private.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    receipt = {'status': 'preparing', 'commands': []}
    result = {'schema': 1, 'status': 'running', 'scope': 'Four owner-test diagnostic only',
              'image_acceptance': False, 'source_revision': plan['source_revision'],
              'native_revision': plan['native_revision'], 'controller_sha256': CONTROLLER_SHA,
              'targets': list(TARGETS), 'files': {}, 'test_stage': 'not-run',
              'auxiliary_stage': 'not-run', 'original_failed_execution_scripts': False}
    stage, captures = 'source-preparation', []
    retention_failed = False
    def retain(name, path):
        nonlocal retention_failed
        try:
            destination = output / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            raw, _ = auxiliary.snapshot(path.resolve(strict=True), MAX_FILE)
            auxiliary.write_original(destination, raw)
            result['files'][name] = identity(destination)
        except (OSError, ValueError):
            retention_failed = True
            raise
    def execute(command, directory, target_receipt, name, *, output_path=None):
        return logs.execute(command, directory, target_receipt, name, cwd=source, output_path=output_path)
    try:
        require(orch.identity(private) == plan['source_revision'], 'Different Orchagent source')
        syncd.clean_source(private)
        require(syncd.identity()['revision'] == plan['source_revision'], 'Different Syncd source')
        receipt['gnoi_source'] = helper.prepare_component_gnoi(source, private, receipt, logs)
        result['effective_heap'] = helper.observe_component_heap(plan, source, scratch, private, receipt, logs)
        logs.execute(helper.setup.MAKE_ARGUMENTS, private, receipt, 'swss.manifests', cwd=source)
        manifests = helper.manifest_snapshot(source)
        logs.execute(syncd.manifest_command(), private, receipt, 'syncd.manifests', cwd=source)
        require(helper.manifest_snapshot(source) == manifests, 'Different shared manifests')
        for family, spec in helper.COMPONENTS.items():
            require(identity(source / 'tools/bazel/ci' / (spec['module'] + '.py'))['sha256'] ==
                    spec['owner_sha256'], 'Different component owner')
            logs.execute(spec['manifest_command'], private, receipt, family + '.manifests', cwd=source)
            require(helper.manifest_snapshot(source) == manifests, 'Different component manifests')
        result['shared_manifests'] = manifests
        for name, path in (
                ('native-launcher.original.py', scratch / 'native-bazel'),
                ('review-proxy.original.sh', scratch / 'review-bazel'),
                ('review-owner.original.sh', source / 'tools/bazel/ci/review_bazel.sh'),
                ('source-user-configuration.original.txt', source / '.bazelrc.user')):
            retain('private/' + name, path)
        options = [*owner.OPTIONS, *helper.OPTIONS,
                   '--platforms=@sonic_build_infra//platforms:x86_64_trixie',
                   '--host_platform=@sonic_build_infra//platforms:x86_64_trixie']
        bazel = str(scratch / 'review-bazel')
        stage = 'action-audit'
        actions = private / 'actions.json'
        actions.touch(mode=0o600)
        execute([bazel, 'aquery', *options, '--output=jsonproto',
                 'deps(set(' + ' '.join(TARGETS) + '))'], private, receipt, 'actions', output_path=actions)
        audit = owner.inspect_actions(actions)
        require(not audit['deb_outputs'], 'Owner diagnostic produces DEB outputs')
        require(not audit['packaging_wrappers'], 'Owner diagnostic uses packaging wrappers')
        result['action_audit'] = audit
        stage = 'configured-test-labels'
        labels = owner.configured_test_labels(TARGETS, bazel, options, private, receipt, execute)
        events = private / 'tests.events.json'
        events.touch(mode=0o600, exist_ok=False)
        captures.extend([('private/tests.events.json', events), ('private/tests.command.log', private / 'tests.log')])
        stage, result['test_stage'] = 'four-tests', 'running'
        returncode = 0
        try:
            execute([bazel, 'test', *options, '--nocache_test_results', '--test_output=errors',
                     '--nozip_undeclared_test_outputs', '--build_event_json_file=' + str(events),
                     *TARGETS], private, receipt, 'tests')
        except subprocess.CalledProcessError as error:
            returncode = error.returncode
        result['test_returncode'] = returncode
        stage = 'original-test-output-retention'
        outcomes, originals, bep_returncode = test_evidence(events, labels, scratch, public)
        require(returncode == bep_returncode, 'Test command and BEP exits differ')
        result['test_results'] = outcomes
        result['tests_passed'] = returncode == 0 and all(value == 'PASSED' for value in outcomes.values())
        result['test_stage'] = 'captured'
        for (label, name), path in sorted(originals.items()):
            retain('private/tests/' + label.rsplit(':', 1)[1] + '/' + name, path)
        # Reuse the owner's exact required standard-output checks after either exit.
        owner.collect_test_outputs(events, private, TARGETS, labels)
        require(('@scapy//:upstream_test', 'test.outputs/upstream-xunit.xml') in originals,
                'Scapy original xUnit is missing')
        stage, result['auxiliary_stage'] = 'auxiliary-inputs', 'running'
        execution_root = owner.build.execution_root(events)
        owned_root = scratch.resolve(strict=True)
        require(execution_root.resolve(strict=True).is_relative_to(owned_root), 'Execution root escapes job')
        raw_runtime = private / 'runtime-provider.jsonl'
        raw_runtime.touch(mode=0o600)
        captures.append(('private/runtime-provider.jsonl', raw_runtime))
        execute([bazel, 'cquery', *options, '--consistent_labels', '--output=starlark',
                 '--starlark:expr=' + RUNTIME_EXPRESSION, 'set(' + ' '.join(TARGETS[:3]) + ')'],
                private, receipt, 'runtime-provider', output_path=raw_runtime)
        retain('private/runtime-provider.jsonl', raw_runtime)
        runtimes = runtime_rows(raw_runtime.read_bytes(), public, execution_root, owned_root)
        selected = []
        for label, paths in runtimes.items():
            retain('private/runfiles/' + label.rsplit(':', 1)[1] + '.manifest', paths['manifest'])
            selected.append(selected_runfiles(paths['manifest'].read_bytes(), owned_root))
        require(all(value == selected[0] for value in selected), 'Tests select different auxiliary runtimes')
        interpreter = selected[0]['python'][1]
        installer_root = selected[0]['installer'][2].parent.parent
        wheel_rows = {}
        for role, label in WHEELS.items():
            value = execute([bazel, 'cquery', *options, '--output=files', label],
                            private, receipt, 'wheel-' + role)
            paths = value.splitlines()
            require(len(paths) == 1, 'Wheel must have one configured output')
            path = local_output(paths[0], execution_root, owned_root)
            require(path.suffix == '.whl' and path.stat().st_size > 0, 'Missing exact built wheel')
            wheel_rows[role] = {'path': str(path), **identity(path)}
            retain('private/wheels/' + path.name, path)
        fixture = source / 'tools/bazel/tests/integration/installed_wheel_fixture.py'
        require(identity(fixture)['sha256'] == FIXTURE_SHA, 'Different installed wheel fixture')
        temporary = private / 'auxiliary-test-tmp'
        temporary.mkdir(mode=0o700)
        inputs = {'schema': 1, 'source_revision': plan['source_revision'],
                  'fixture': {'path': str(fixture), **identity(fixture)},
                  'interpreter': {'path': str(interpreter), **identity(interpreter)},
                  'installer': installer_manifest(installer_root),
                  'test_tmpdir': str(temporary), 'exclude_source_roots': [str(source)],
                  'wheels': wheel_rows}
        inputs_path = private / 'auxiliary-inputs.json'
        auxiliary.write_json(inputs_path, inputs)
        retain('private/auxiliary-inputs.json', inputs_path)
        stage = 'auxiliary-installation'
        prior = os.environ.get('TEST_TMPDIR')
        os.environ['TEST_TMPDIR'] = str(temporary)
        try:
            execute([str(interpreter), '-I', '-B', str(auxiliary_path), '--inputs', str(inputs_path),
                     '--inputs-sha256', identity(inputs_path)['sha256'],
                     '--output', str(output / 'auxiliary')], private, receipt, 'auxiliary-install')
        finally:
            if prior is None:
                os.environ.pop('TEST_TMPDIR', None)
            else:
                os.environ['TEST_TMPDIR'] = prior
        summary = public.load_json((output / 'auxiliary/summary.json').read_bytes())
        public.safe_json(summary)
        result['auxiliary'] = summary
        result['auxiliary_stage'] = 'captured'
        helper.setup.source_check(source, plan['source_revision'])
        require(identity(owner_path)['sha256'] == CONTROLLER_SHA and
                identity(auxiliary_path)['sha256'] == AUXILIARY_SHA, 'Diagnostic owner changed')
        result['status'] = 'completed'
    except Exception as error:
        result['status'] = 'failed'
        result['failure_stage'] = stage
        result['failure_type'] = type(error).__name__
        if result['test_stage'] == 'running':
            result['test_stage'] = 'failed'
        if result['auxiliary_stage'] == 'running':
            result['auxiliary_stage'] = 'failed'
        raise
    finally:
        auxiliary.write_json(private / 'receipt.json', receipt)
        captures.append(('private/receipt.json', private / 'receipt.json'))
        for entry in receipt['commands']:
            name = entry['log']
            require(isinstance(name, str) and Path(name).name == name, 'Invalid maintained log name')
            captures.append(('private/commands/' + name, private / name))
        captures_complete = capture_registered(captures, output, retain, result)
        retention_failed = retention_failed or not captures_complete
        for path in sorted((output / 'auxiliary').rglob('*')) if (output / 'auxiliary').is_dir() else []:
            if path.is_file():
                result['files'][path.relative_to(output).as_posix()] = identity(path)
        result['command_results'] = [
            {key: entry[key] for key in ('returncode', 'elapsed_seconds', 'log')}
            for entry in receipt['commands']]
        result['retention_complete'] = not retention_failed
        public.safe_json(result)
        auxiliary.write_json(output / 'diagnostic.json', result)
    require(not retention_failed, 'Original evidence retention incomplete')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    require(args.root.is_absolute() and not args.root.is_symlink() and
            args.root.resolve(strict=True) == args.root, 'Root must be the existing private job directory')
    raise SystemExit(run(args.root))

