#!/usr/bin/env python3
"""Retain original native API objects under this observer's actual identity.

This is a fixed metadata caller of the maintained receipt owner. It does not
impersonate a normal PR job or produce an acceptance result.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.parse


def require(value, message):
    if not value:
        raise ValueError(message)


def checked_bytes(path, expected):
    require(path.is_file() and not path.is_symlink(), "Missing fixed regular input")
    require(path.stat().st_size == expected['bytes'] <= 2 * 1024**2,
            "Fixed input size differs")
    value = path.read_bytes()
    require(len(value) == expected['bytes'] and
            hashlib.sha256(value).hexdigest() == expected['sha256'],
            "Fixed input identity differs")
    return value


def load_owner(source, plan):
    for name, identity in plan['owner_sources'].items():
        require(Path(name).parts[:3] == ('tools', 'bazel', 'ci') and
                len(Path(name).parts) == 4 and '..' not in Path(name).parts,
                "Unexpected owner input path")
        checked_bytes(source / name, identity)
    sys.path.insert(0, str(source.resolve()))
    return importlib.import_module('tools.bazel.ci.image_worker_receipt')


def actual_operator(owner, plan):
    keys = ('GITHUB_REPOSITORY', 'GITHUB_EVENT_NAME', 'GITHUB_ACTOR',
            'GITHUB_TRIGGERING_ACTOR', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
            'GITHUB_JOB', 'GITHUB_SHA', 'GITHUB_REF', 'RUNNER_NAME')
    context = {key: os.environ[key] for key in keys}
    require(context['GITHUB_REPOSITORY'] == plan['repository'] and
            context['GITHUB_EVENT_NAME'] == 'workflow_dispatch' and
            context['GITHUB_ACTOR'] == context['GITHUB_TRIGGERING_ACTOR'] == 'securely1g' and
            context['GITHUB_JOB'] == 'vs_native_api_provenance',
            "Unexpected actual observer context")
    run_id = owner.number(int(context['GITHUB_RUN_ID']))
    attempt = owner.number(int(context['GITHUB_RUN_ATTEMPT']))
    revision = owner.revision(context['GITHUB_SHA'])
    run = owner.api('/actions/runs/' + str(run_id))
    require(run['id'] == run_id and run['run_attempt'] == attempt and
            run['head_sha'] == revision and run['event'] == 'workflow_dispatch' and
            run['path'] == owner.WORKFLOW and
            run['repository']['full_name'] == run['head_repository']['full_name'] == plan['repository'] and
            run['actor']['login'] == run['triggering_actor']['login'] == 'securely1g',
            "Actual operator API context differs")
    return {'context': context, 'run': run}


def fixed_artifact(actual, expected):
    require(all(actual[key] == value for key, value in expected.items()) and
            actual['expired'] is False, "Fixed original artifact differs")


def authenticate_origin(owner, plan, family, output):
    actual_operator(owner, plan)
    selected = plan['families'][family]
    output.mkdir(mode=0o700)
    owner.worker_origin(SimpleNamespace(artifact=selected['request_artifact']['id'],
                                       image=family, directory=output))
    origin = owner.read(output / 'origin.json', public=False)
    fixed_artifact(origin['artifact'], selected['request_artifact'])
    require(origin['run']['id'] == selected['request']['normal_run'] and
            origin['run']['head_sha'] == selected['request']['head'],
            "Original normal request source differs")


def capture(owner, plan, family, source, request_path, output):
    selected = plan['families'][family]
    request_bytes = checked_bytes(request_path, {'bytes': selected['request_bytes'],
                                                'sha256': selected['request_sha256']})
    request = owner.read(request_path)
    require(request == selected['request'], "Original request fields differ")
    owner.validate_request(request)
    contract = owner.read(source / 'tools/bazel/ci' / (family + '_worker_contract.json'))
    owner.reviewed_worker_pool(request, contract)
    origin = owner.read(output / 'origin.json', public=False)
    fixed_artifact(origin['artifact'], selected['request_artifact'])
    require(origin['run']['id'] == request['normal_run'] and
            origin['run']['head_sha'] == request['head'], "Original request API source differs")
    operator = actual_operator(owner, plan)
    require(operator['run']['id'] not in (request['normal_run'], selected['producer_run']),
            "Observer and original runs must remain distinct")
    name = owner.acceptance_name(request)
    rows = owner.inventory(owner.api('/actions/artifacts?per_page=100&name=' +
                                     urllib.parse.quote(name)), 'artifacts')
    artifact = owner.unique_artifact(rows, name)
    fixed_artifact(artifact, selected['acceptance_artifact'])
    run = owner.api('/actions/runs/' + str(artifact['workflow_run']['id']))
    require(run['id'] == selected['producer_run'] and
            run['run_attempt'] == selected['producer_attempt'], "Original producer run differs")
    jobs = owner.inventory(owner.api('/actions/runs/' + str(run['id']) +
                                     '/jobs?filter=latest&per_page=100'), 'jobs')
    job = owner.worker_metadata(request, artifact, run, jobs, contract)
    require(job['id'] == selected['producer_job'], "Original producer job differs")
    provenance = {'artifact': artifact, 'run': run, 'job': job, 'jobs': jobs,
                  'origin': origin['artifact']}
    require(len(json.dumps(provenance).encode()) <= owner.MEMBER_LIMIT,
            "Original API provenance exceeds the existing reader bound")
    owner.write(output / 'provenance.json', provenance, public=False)
    with (output / 'request.json').open('xb') as stream:
        stream.write(request_bytes)
    observation = {'schema': 1, 'scope': 'original_native_api_metadata_observation',
        'family': family, 'operator': operator, 'source_revision': plan['source_revision'],
        'source_head': plan['source_head'], 'source_tree': plan['source_tree'],
        'request_sha256': selected['request_sha256'],
        'provenance_sha256': hashlib.sha256((output / 'provenance.json').read_bytes()).hexdigest(),
        'normal_consumer_job_passed': False, 'native_acceptance_executed': False,
        'original_sources_reattributed': False}
    owner.write(output / 'observation.json', observation, public=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('origin', 'capture'))
    parser.add_argument('--family', choices=('syncd', 'gearbox'), required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--inputs-sha256', required=True)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(args.inputs.is_file() and not args.inputs.is_symlink() and
            args.inputs.stat().st_size <= 65536, "Missing bounded fixed plan")
    data = args.inputs.read_bytes()
    require(hashlib.sha256(data).hexdigest() == args.inputs_sha256, "Fixed plan differs")
    plan = json.loads(data)
    owner = load_owner(args.source, plan)
    if args.mode == 'origin':
        authenticate_origin(owner, plan, args.family, args.output)
    else:
        require(args.request is not None, "Missing original request input")
        capture(owner, plan, args.family, args.source, args.request, args.output)


if __name__ == '__main__':
    main()
