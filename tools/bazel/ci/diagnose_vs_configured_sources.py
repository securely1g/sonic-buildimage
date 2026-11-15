#!/usr/bin/env python3
"""Diagnose configured source discovery only; never build or admit images."""
import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

OLD_REVISION = 'ecf71beb27adbb14962070bed85ee41b3626d40e'
OLD_SHA = 'c20ef5bc44334c9d369467f4ada216a6135ebc5d63be20a7ea3d0f2844539dc4'
NEW_SHA = 'eff09671afe46e50812551c21e9aa71fd965409d4ac9f2f625d5c55e1d895523'
HELPER_SHA = '90742f5704f634585290602893d58e704f5fad3b73ee46165758e4f984bc3de8'
PUBLIC_SHA = '69354ce956cb2d21bad917911c562092eb0dc5fd9945e10344b2dda723f562d4'
MAX_METADATA = 128 * 1024**2


def identity(path):
    raw = path.read_bytes()
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def load_module(path, expected, name):
    if path.is_symlink() or not path.is_file() or identity(path)['sha256'] != expected:
        raise ValueError('Different sealed diagnostic owner: ' + name)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify_rows(raw, public):
    """Describe actual old-parser failures without guessing their source."""
    counts = {'accepted': 0, 'display_shape': 0, 'label_guard': 0}
    examples = []
    for number, line in enumerate(raw.decode('utf-8').splitlines(keepends=True), 1):
        match = re.fullmatch(r'([^\s()]+) \((?:[0-9a-f]+|null)\)\n', line)
        category = 'display_shape'
        if match is not None:
            label = match[1]
            if label.startswith('@@//'):
                label = label[2:]
            try:
                public.label(label)
                category = 'accepted'
            except ValueError:
                category = 'label_guard'
        counts[category] += 1
        if category != 'accepted' and len(examples) < 32:
            data = line.encode('utf-8')
            example = {'row': number, 'category': category, 'bytes': len(data),
                       'sha256': hashlib.sha256(data).hexdigest(), 'text': line[:4096],
                       'truncated': len(line) > 4096}
            public.safe_json(example)
            examples.append(example)
    return {'counts': counts, 'examples': examples,
            'examples_complete': counts['display_shape'] + counts['label_guard'] <= len(examples)}


def retain_file(path, destination, public, *, json_content=False, binary=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_METADATA:
        raise ValueError('Unexpected diagnostic file type or size')
    raw = path.read_bytes()
    if not binary:
        public.safe_text(raw.decode('utf-8'))
        if json_content:
            public.safe_json(public.load_json(raw))
    with destination.open('xb') as stream:
        stream.write(raw)
    destination.chmod(0o600)
    if identity(destination) != identity(path):
        raise ValueError('Diagnostic original copy differs')
    return identity(destination)


def run(root):
    control, source, legacy, scratch = [root / name for name in ('control', 'source', 'legacy', 'scratch')]
    helper = load_module(control / 'tools/bazel/ci/build_vs_coherent_installed_policies.py',
                         HELPER_SHA, '_diagnostic_cohort')
    plan = helper.load_plan(control / 'tools/bazel/ci/vs_coherent_installed_policy_inputs.json')
    helper.environment_check(plan, scratch, source)
    helper.setup.source_check(source, plan['source_revision'])
    helper.setup.source_check(legacy, OLD_REVISION)
    orch = helper.setup.import_from(source, 'tools.bazel.ci.orchagent_image')
    # Preload every eager dependency from the product checkout, before loading
    # either controller owner. Their direct APIs receive the workspace explicitly.
    for name in ('build', 'cache_reuse', 'command_log', 'public_artifacts', 'resolution',
                 'artifact_validation', 'bazel_commands', 'syncd_image'):
        importlib.import_module('tools.bazel.ci.' + name)
    importlib.import_module('tools.bazel.gzip.source_archive')
    logs = importlib.import_module('tools.bazel.ci.command_log')
    public = importlib.import_module('tools.bazel.ci.public_artifacts')
    syncd = importlib.import_module('tools.bazel.ci.syncd_image')
    helper.require(identity(Path(public.__file__))['sha256'] == PUBLIC_SHA, 'Different public owner')
    old_path, new_path = [base / 'tools/bazel/ci/container.py' for base in (legacy, control)]
    old = load_module(old_path, OLD_SHA, '_diagnostic_original_container')
    new = load_module(new_path, NEW_SHA, '_diagnostic_structured_container')
    for key, value in tuple(sys.modules.items()):
        if key.startswith('tools.') and getattr(value, '__file__', None):
            helper.require(Path(value.__file__).resolve().is_relative_to(source), 'Mixed product imports')
    helper.require(old.public_artifacts is public and new.public_artifacts is public and
                   old.command_log is logs and new.command_log is logs and
                   old.OPTIONS == new.OPTIONS and old.SOURCE_FILES == new.SOURCE_FILES,
                   'Owner source-discovery interface changed')
    private, output = scratch / 'source-discovery-diagnostic', root / 'diagnostic-output'
    private.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    receipt = {'status': 'preparing', 'commands': []}
    result = {'schema': 1, 'status': 'running', 'scope': 'Source-discovery diagnostic only',
              'image_acceptance': False, 'source_revision': plan['source_revision'],
              'native_revision': plan['native_revision'], 'catalog_revision': plan['catalog_revision'],
              'original_controller_sha256': OLD_SHA, 'candidate_controller_sha256': NEW_SHA,
              'files': {}, 'original_parser': {'status': 'not-run'},
              'structured_discovery': {'status': 'not-run'}}
    stage = 'source-preparation'
    capture_paths = []
    command_receipts = [receipt]
    try:
        helper.require(orch.identity(private) == plan['source_revision'], 'Different Orchagent source')
        syncd.clean_source(private)
        helper.require(syncd.identity()['revision'] == plan['source_revision'], 'Different Syncd source')
        receipt['gnoi_source'] = helper.prepare_component_gnoi(source, private, receipt, logs)
        result['effective_heap'] = helper.observe_component_heap(plan, source, scratch, private, receipt, logs)
        logs.execute(helper.setup.MAKE_ARGUMENTS, private, receipt, 'swss.manifests', cwd=source)
        manifests = helper.manifest_snapshot(source)
        logs.execute(syncd.manifest_command(), private, receipt, 'syncd.manifests', cwd=source)
        helper.require(helper.manifest_snapshot(source) == manifests, 'Different shared manifests')
        for family, spec in helper.COMPONENTS.items():
            owner = source / 'tools/bazel/ci' / (spec['module'] + '.py')
            helper.require(identity(owner)['sha256'] == spec['owner_sha256'], 'Different component owner')
            logs.execute(spec['manifest_command'], private, receipt, family + '.manifests', cwd=source)
            helper.require(helper.manifest_snapshot(source) == manifests, 'Different component manifests')
        result['shared_manifests'] = manifests
        stage = 'launcher-evidence'
        selected = shutil.which('bazel', path='/usr/local/bin:/usr/bin:/bin')
        if selected is None:
            selected = shutil.which('bazelisk', path='/usr/local/bin:/usr/bin:/bin')
        helper.require(selected is not None, 'Missing installed launcher')
        binary = Path(selected).resolve(strict=True)
        mode = binary.stat()
        helper.require(stat.S_ISREG(mode.st_mode) and mode.st_uid == 0 and not mode.st_mode & 0o022
                       and mode.st_mode & 0o111 and mode.st_size <= MAX_METADATA and
                       identity(binary)['sha256'] == plan['bazel_launcher_sha256'], 'Different installed launcher')
        native = scratch / 'native-bazel'
        assigned = [ast.literal_eval(node.value) for node in ast.parse(native.read_text()).body
                    if isinstance(node, ast.Assign) and any(isinstance(v, ast.Name) and v.id == 'binary'
                                                           for v in node.targets)]
        helper.require(assigned == [str(binary)], 'Generated launcher selects a different installed binary')
        for name, path, is_binary in [('installed-launcher.original', binary, True),
                                     ('native-launcher.original.py', native, False),
                                     ('review-proxy.original.sh', scratch / 'review-bazel', False),
                                     ('review-owner.original.sh', source / 'tools/bazel/ci/review_bazel.sh', False),
                                     ('source-user-configuration.original.txt', source / '.bazelrc.user', False)]:
            result['files'][name] = retain_file(path, output / name, public, binary=is_binary)
        options = [*old.OPTIONS, *helper.OPTIONS,
                   '--platforms=@sonic_build_infra//platforms:x86_64_trixie',
                   '--host_platform=@sonic_build_infra//platforms:x86_64_trixie']
        bazel = str(scratch / 'review-bazel')
        old_directory, new_directory = private / 'original', private / 'structured'
        old_directory.mkdir(mode=0o700)
        new_directory.mkdir(mode=0o700)
        old_receipt, new_receipt = {'commands': []}, {'commands': []}
        command_receipts.extend([old_receipt, new_receipt])
        def execute(command, directory, target_receipt, name, *, output_path=None):
            value = logs.execute(command, directory, target_receipt, name, cwd=source, output_path=output_path)
            if output_path is None:
                raw = directory / (name + '.stdout.txt')
                with raw.open('x') as stream:
                    stream.write(value)
                raw.chmod(0o600)
            return value
        stage = 'test-suite-query'
        targets = [*old.query_tests('//dockers/docker-orchagent:ci_tests', bazel, old_directory,
                                   old_receipt, execute), '//dockers/docker-orchagent:ci_artifacts']
        result['targets'] = targets
        stage = 'original-configured-query'
        raw_old = old_directory / 'source-configured-targets.raw.txt'
        capture_paths.append(('original-labels.raw.txt', raw_old, False))
        capture_paths.append(('original-query.stderr.log', old_directory / 'source-configured-targets.log', False))
        try:
            old.configured_source_labels(targets, bazel, options, old_directory, old_receipt, execute)
            result['original_parser'] = {'status': 'passed'}
        except ValueError as error:
            message = str(error)
            public.safe_text(message)
            result['original_parser'] = {'status': 'rejected', 'reason': message[:512]}
        helper.require(raw_old.stat().st_size <= MAX_METADATA, 'Original metadata exceeds bounded retention')
        result['original_rows'] = classify_rows(raw_old.read_bytes(), public)
        stage = 'structured-source-query'
        capture_paths.extend([
            ('structured-targets.raw.json', new_directory / 'source-configured-targets.raw.json', True),
            ('structured-query.stderr.log', new_directory / 'source-configured-targets.log', False),
            ('source-buildfiles.query', new_directory / 'source-buildfiles.query', False),
            ('source-inputs.stdout.txt', new_directory / 'source-inputs.stdout.txt', False)])
        names = new.declared_sources(targets, bazel, source, new_directory, new_receipt, execute, options=options)
        source_hashes = {name: identity(source / name) for name in names}
        result['structured_discovery'] = {'status': 'passed', 'source_files': source_hashes,
                                          'discovery': new_receipt['source_discovery']}
        helper.setup.source_check(source, plan['source_revision'])
        helper.require(identity(old_path)['sha256'] == OLD_SHA and identity(new_path)['sha256'] == NEW_SHA,
                       'Controller owner changed during diagnostic')
        result['status'] = 'completed'
    except Exception as error:
        result['status'] = 'failed'
        result['failure_stage'] = stage
        result['failure_type'] = type(error).__name__
        message = str(error)[:512]
        try:
            public.safe_text(message)
            result['failure_reason'] = message
        except ValueError:
            result['failure_reason'] = 'Rejected by unchanged public text guard'
        raise
    finally:
        with (private / 'receipts.json').open('x') as stream:
            json.dump(command_receipts, stream, sort_keys=True, indent=2)
            stream.write('\n')
        result['command_results'] = [
            {key: entry[key] for key in ('returncode', 'elapsed_seconds', 'log')}
            for record in command_receipts for entry in record['commands']]
        for name, path, json_content in capture_paths:
            if path.is_file():
                try:
                    result['files'][name] = retain_file(path, output / name, public, json_content=json_content)
                except (OSError, ValueError, UnicodeError):
                    result['files'][name] = {**identity(path), 'retained': False,
                                             'reason': 'Original did not pass bounded public-file checks'}
        # Full argv-bearing command records remain private. Public evidence names
        # only actual raw outputs, hashes, parser results and fixed source identities.
        public.safe_json(result)
        with (output / 'diagnostic.json').open('x') as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write('\n')
        (output / 'diagnostic.json').chmod(0o600)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    arguments = parser.parse_args()
    root = arguments.root
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root:
        parser.error('Root must be the existing absolute private job directory')
    raise SystemExit(run(root))
