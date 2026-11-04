#!/usr/bin/env python3
"""One exact failed-publication recovery; never invokes APT, Bazel or tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import registry_native_worker as worker

ORIGINAL_RUN = 41805992669
ORIGINAL_JOB = 126493030326
ORIGINAL_CONTROL = '87080a1abb62df8c19052def730497835b8d722a'
REVISION = '28ea8a8b17f6ecf85a32aadf8ef5eb4f65ca9623'
WORKER = 'sonic-slave-lgh114-2'
ORIGINAL_ARTIFACTS = Path('/actions/_work/_temp/registry-native-artifacts')
PRIVATE = Path('/bazel_cache/registry-native')/WORKER/'dash'/f'{ORIGINAL_RUN}-1'
RECEIPT_HASHES = {
    'worker-registry.json': 'bfbdb847f00d6daf7bc0a6d103677e344126e8f745d2063a55b73a7205a1e775',
    'worker-run.json': 'fe632e06cf6e3ee09286bd0b670c5ad9648017a923f19827ad5ba2bd22abc483',
    'worker-execute.json': '3f9f1e9c43b130f4fde9b0636bb09a66a8d6fbdc4b71de430568772884546570',
    'worker-publish.json': '5d96e3b8385945c936bd75d557d9a3c7b842f2ad3d64385b3d3d031102cf07a2',
}
require = worker.require


def check_job(run, job):
    require(run['id'] == ORIGINAL_RUN and run['head_sha'] == ORIGINAL_CONTROL and
            run['event'] == 'workflow_dispatch' and run['run_attempt'] == 1 and
            run['repository']['full_name'] == 'securely1g/sonic-buildimage' and
            run['path'] == '.github/workflows/bazel-oci.yml' and
            run['status'] == 'completed' and run['conclusion'] == 'failure',
            'Unexpected original workflow identity')
    require(job['id'] == ORIGINAL_JOB and job['run_id'] == ORIGINAL_RUN and
            job['status'] == 'completed' and job['conclusion'] == 'failure' and
            job['runner_name'] == WORKER, 'Unexpected original worker job')
    required = {'Run the original native registry build and tests': 'success',
                'Stage declared native outputs and public receipts': 'failure',
                'Upload ordinary registry consumer evidence': 'success'}
    for name, conclusion in required.items():
        steps = [step for step in job['steps'] if step['name'] == name]
        require(len(steps) == 1 and steps[0]['status'] == 'completed' and
                steps[0]['conclusion'] == conclusion, 'Original phase outcome differs')
    return {'status': 'passed', 'original_run': ORIGINAL_RUN, 'original_job': ORIGINAL_JOB,
            'original_control': ORIGINAL_CONTROL, 'registry_revision': REVISION,
            'worker': WORKER, 'native_phase': 'success', 'original_publication': 'failure'}


def verify_remote(output):
    token = os.environ.get('GH_TOKEN')
    require(token and os.getuid() == os.getgid() == 1000, 'Outer read-only Actions identity required')
    def api(suffix):
        request = urllib.request.Request(
            'https://api.github.com/repos/securely1g/sonic-buildimage/' + suffix,
            headers={'Authorization': 'Bearer '+token, 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    proof = check_job(api('actions/runs/'+str(ORIGINAL_RUN)), api('actions/jobs/'+str(ORIGINAL_JOB)))
    worker.write(output/'worker-export-origin.json', proof)


def available_state():
    work = PRIVATE/'work'
    return {'original_artifacts_present': ORIGINAL_ARTIFACTS.is_dir(),
            'original_output_index_present': (ORIGINAL_ARTIFACTS/'outputs.json').is_file(),
            'persistent_work_present': work.is_dir(),
            'persistent_spec_present': (PRIVATE/'spec.json').is_file(),
            'persistent_consumer_present': (work/'consumer/MODULE.bazel').is_file(),
            'persistent_output_bases': len(list((work/'bazel').glob('*/server'))) if (work/'bazel').is_dir() else 0}


def validate_native(artifacts, private, workspace):
    require(artifacts.resolve() == artifacts and private.resolve() == private, 'Recovery paths traverse symlinks')
    for name, checksum in RECEIPT_HASHES.items():
        require(worker.digest(artifacts/name) == checksum, 'Original public receipt differs')
    state = json.loads((artifacts/'worker-registry.json').read_text())
    require(state['status'] == 'passed' and state['dispatcher_revision'] == ORIGINAL_CONTROL and
            state['registry_revision'] == REVISION and state['worker'] == WORKER and
            state['server_shutdown']['status'] == 'stopped' and state['no_oom_events'] is True,
            'Original native acceptance is incomplete')
    require(json.loads((artifacts/'worker-publish.json').read_text())['error_class'] == 'PermissionError',
            'Original failure is not export permission')
    runner, config = worker.registry_config(workspace, worker.PROFILES['dash'])
    spec = json.loads((private/'spec.json').read_text())
    require(spec['work'] == str(private/'work') and spec['artifacts'] == str(artifacts) and
            spec['config'] == config and state['build_targets'] == config['build_targets'] and
            state['test_targets'] == config['test_targets'], 'Original configured inventory differs')
    for pidfile in (private/'work/bazel').glob('*/server/server.pid.txt'):
        pid = int(pidfile.read_text().strip())
        process = worker.server_process(pid)
        require(process is None or (process['state'] == 'Z' and process['rss_pages'] == 0),
                'An original consumer server is still live')
    index = json.loads((artifacts/'outputs.json').read_text())
    require(set(index) == set(config['build_targets']), 'Original output target inventory differs')
    summaries = {}
    for phase in ('build', 'test'):
        path = artifacts/(phase+'-events.jsonl')
        summary = worker.bep_summary(path.read_text().splitlines())
        require(summary['command'] == phase and summary['bazel_version'] == '8.5.1' and
                summary['finished'] == {'success': True, 'exit_code': 0} and not summary['truncated'],
                'Original BEP is not a complete successful invocation')
        if phase == 'build':
            targets = {runner.normalized_label(label): value for label, value in summary['targets'].items()}
            require(all(targets.get(label) is True for label in config['build_targets']),
                    'Original build BEP omits required targets')
        else:
            runner.verify_tests(path, config['test_targets'])
        summaries[phase] = {'sha256': worker.digest(path), 'summary': summary}
    require(worker.verify_outputs(artifacts), 'Original output index is unavailable')
    unique = {record['path']: record['size'] for rows in index.values() for record in rows}
    require(shutil.disk_usage(artifacts).free >= 2*sum(unique.values())+1024**3,
            'Insufficient export staging capacity')
    return {'outputs_sha256': worker.digest(artifacts/'outputs.json'),
            'declared_files': len(unique), 'declared_bytes': sum(unique.values()),
            'original_bep': summaries}


def repair(args, output):
    require(os.geteuid() == 0 and Path('/.dockerenv').is_file(), 'Recovery requires the real disposable container root')
    worker.marker_identity(Path('/etc/sonic-slave-github-runner.json'))
    worker.clean_source(args.workspace, REVISION)
    admission = json.loads((output/'worker-export-preflight.json').read_text())
    require(admission['status'] == 'passed' and admission['facts']['worker'] == WORKER,
            'Recovery preflight failed')
    native = validate_native(ORIGINAL_ARTIFACTS, PRIVATE, args.workspace)
    permissions = worker.readable_tree_archives(ORIGINAL_ARTIFACTS)
    worker.write(output/'worker-export-permissions.json', {'status': 'passed', **native,
                                                         'permissions': permissions})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('verify', 'recover', 'repair'))
    parser.add_argument('--consumer', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--workspace', required=True, type=Path)
    args = parser.parse_args()
    worker.selection(args.consumer, args.revision, args.worker, os.environ)
    require(args.consumer == 'dash' and args.revision == REVISION and args.worker == WORKER,
            'Only the fixed original DASH export can be recovered')
    require(args.workspace.is_absolute() and args.workspace.resolve() == args.workspace and
            args.workspace.is_relative_to('/actions/_work'), 'Invalid exact registry checkout')
    output = ROOT/'artifacts/registry-export'
    output.mkdir(parents=True, exist_ok=True)
    receipt = {'status': 'running', 'kind': 'registry-native-export', 'original_run': ORIGINAL_RUN,
               'original_job': ORIGINAL_JOB, 'original_control': ORIGINAL_CONTROL, 'registry_revision': REVISION,
               'export_run': int(os.environ['GITHUB_RUN_ID']), 'worker': WORKER,
               'native_execution': False, 'available': available_state(), 'failed_stage': None}
    stage = {'repair': 'root_repair', 'recover': 'preflight', 'verify': 'original_workflow'}[args.phase]
    try:
        if args.phase == 'verify':
            verify_remote(output)
        elif args.phase == 'repair':
            repair(args, output)
        else:
            require(os.getuid() == os.getgid() == 1000, 'Outer exporter must remain UID/GID1000')
            origin = json.loads((output/'worker-export-origin.json').read_text())
            require(origin == {'status': 'passed', 'original_run': ORIGINAL_RUN, 'original_job': ORIGINAL_JOB,
                               'original_control': ORIGINAL_CONTROL, 'registry_revision': REVISION,
                               'worker': WORKER, 'native_phase': 'success', 'original_publication': 'failure'},
                    'Verified original phase receipt is missing')
            preflight = worker.measure(WORKER)
            worker.write(output/'worker-export-preflight.json', preflight)
            require(preflight['status'] == 'passed', 'Recovery resource/isolation preflight failed')
            stage = 'retained_evidence'
            require(receipt['available']['original_output_index_present'], 'Original evidence is unavailable')
            stage = 'root_repair'
            cache = Path('/bazel_cache/registry-native')/WORKER
            environment = worker.child_environment(os.environ, args.workspace, cache)
            subprocess.run(['sudo', '-n', '/usr/bin/env', '-i', *[k+'='+v for k,v in environment.items()],
                            sys.executable, '-B', str(Path(__file__).resolve()), 'repair', *sys.argv[2:]], check=True)
            stage = 'publication'
            worker.clean_source(args.workspace, REVISION)
            worker.publish(SimpleNamespace(artifacts=ORIGINAL_ARTIFACTS, workspace=args.workspace, revision=REVISION), recovery=True)
            receipt['published_tar_sha256'] = worker.digest(output/'registry-native-evidence.tar')
            receipt['published_tar_bytes'] = (output/'registry-native-evidence.tar').stat().st_size
        receipt['status'] = 'passed'
    except BaseException as error:
        receipt.update(status='failed', failed_stage=stage, error_class=type(error).__name__)
        raise
    finally:
        worker.write(output/('worker-export-'+args.phase+'.json'), receipt)


if __name__ == '__main__':
    main()
