#!/usr/bin/env python3
"""Fail closed unless final assembly runs without root, capabilities or sockets."""
import argparse
import json
import os
from pathlib import Path


def check(status=None, uid=None, socket_paths=None):
    uid = os.geteuid() if uid is None else uid
    status = Path('/proc/self/status').read_text() if status is None else status
    fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    if uid == 0:
        raise ValueError('final assembly must run as a nonroot user')
    capabilities = {name: int(fields.get(name, '-1').strip(), 16)
                    for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')}
    if any(capabilities.values()):
        raise ValueError('final assembly requires all Linux capabilities dropped')
    if fields.get('NoNewPrivs', '').strip() != '1' or fields.get('Seccomp', '').strip() != '2':
        raise ValueError('final assembly requires no-new-privileges and a seccomp filter')
    paths = socket_paths if socket_paths is not None else (
        '/var/run/docker.sock', '/run/docker.sock', '/run/containerd/containerd.sock', '/dev/kvm')
    if any(Path(path).exists() or Path(path).is_symlink() for path in paths):
        raise ValueError('final assembly must not receive a runtime socket or KVM device')
    return {'schema': 1, 'uid': uid, 'capabilities': capabilities,
            'no_new_privileges': True, 'seccomp_filter': True,
            'runtime_sockets_and_kvm': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    receipt = check()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
