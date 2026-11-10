"""Original pure request fixtures shared by controller admission tests.

Function bodies come from p4rt_worker_receipt_test.py at Git blob
df0b935198ef06aa3f0b26cba66eec9097652d6f; no legacy controller is imported.
"""
from tools.bazel.ci import image_worker_receipt as receipt

def request():
    return {'schema': 1, 'repository': receipt.REPOSITORY, 'workflow': receipt.WORKFLOW,
            'normal_run': 123, 'request_attempt': 1, 'pr': 159, 'head': 'a' * 40,
            'base': 'b' * 40, 'merge': 'c' * 40, 'tree': 'd' * 40,
            'catalog': 'e' * 40, 'owner': 'f' * 40, 'controller': '1' * 40,
            'contract_sha256': '2' * 64}


def pr_event(value):
    return {'number': value['pr'], 'repository': {'full_name': receipt.REPOSITORY},
        'pull_request': {'number': value['pr'],
            'head': {'sha': value['head'], 'ref': 'feature', 'repo': {'full_name': receipt.REPOSITORY}},
            'base': {'sha': value['base'], 'ref': 'main', 'repo': {'full_name': receipt.REPOSITORY}}}}


def pr_environment(value):
    return {'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_REPOSITORY': receipt.REPOSITORY,
        'GITHUB_REF': 'refs/pull/' + str(value['pr']) + '/merge', 'GITHUB_SHA': value['merge'],
        'GITHUB_BASE_REF': 'main', 'GITHUB_HEAD_REF': 'feature',
        'CI_HEAD_SHA': value['head'], 'CI_BASE_SHA': value['base']}


def metadata():
    req = request()
    artifact = {'id': 300, 'name': receipt.acceptance_name(req), 'expired': False,
                'size_in_bytes': 4096, 'workflow_run': {'id': 200, 'head_sha': req['controller'],
                    'repository_id': 42, 'head_repository_id': 42}}
    run = {'id': 200, 'head_sha': req['controller'], 'run_attempt': 1,
           'repository': {'full_name': receipt.REPOSITORY, 'id': 42},
           'head_repository': {'full_name': receipt.REPOSITORY}, 'path': receipt.WORKFLOW,
           'event': 'workflow_dispatch', 'actor': {'login': 'securely1g'},
           'status': 'completed', 'conclusion': 'success'}
    job = {'id': 201, 'name': 'Source image (p4rt)', 'status': 'completed', 'conclusion': 'success',
           'run_id': 200, 'run_attempt': 1, 'head_sha': req['controller'],
           'runner_id': 12, 'runner_name': 'sonic-slave-lgh114-1',
           'labels': ['sonic-slave-container-dpool', 'sonic-slave-lgh114-1'],
           'steps': [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in receipt.STEPS]}
    return req, artifact, run, [job]
