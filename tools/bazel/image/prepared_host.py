#!/usr/bin/env python3
"""Export and verify finalized host files across the CI privilege boundary.

The native preparation job owns host finalization. Final assembly only verifies
its receipt against current declared inputs and copies the three output files.
No daemon, mount, namespace, chroot or privileged subprocess is used here.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host


OUTPUT_NAMES = {'fs': 'fs.squashfs', 'boot': 'boot.tar', 'platform': 'platform.tar.gz'}


def load_receipt(path):
    value = json.loads(Path(path).read_text())
    if (not isinstance(value, dict) or value.get('schema') != 1
            or value.get('status') != 'passed' or value.get('image_store_backend') != 'containerd'
            or not isinstance(value.get('inputs'), dict) or value['inputs'].get('schema') != 1
            or value['inputs'].get('image_store_backend') != 'containerd'
            or set(value.get('outputs', {})) != set(OUTPUT_NAMES)):
        raise ValueError('prepared host requires a successful containerd host receipt with input identities')
    for key in OUTPUT_NAMES:
        record = value['outputs'][key]
        if (not isinstance(record, dict) or set(record) != {'size', 'sha256'}
                or type(record['size']) is not int or record['size'] <= 0
                or not isinstance(record['sha256'], str)
                or len(record['sha256']) != 64
                or any(char not in '0123456789abcdef' for char in record['sha256'])):
            raise ValueError('invalid prepared host output identity: ' + key)
    return value


def verify_outputs(paths, receipt):
    for name in OUTPUT_NAMES:
        if host.file_identity(paths[name]) != receipt['outputs'][name]:
            raise ValueError('prepared host output failed size/SHA256 verification: ' + name)


def copy_verified(source, destination, expected):
    """Stage a complete checked copy, leaving an existing output on failure."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError('prepared host destination must not be a symlink')
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            with Path(source).open('rb') as incoming:
                shutil.copyfileobj(incoming, stream, 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
            if host.file_identity(pending) != expected:
                raise ValueError('prepared host bytes changed while copying')
            pending.chmod(0o644)
            pending.replace(destination)
        finally:
            pending.unlink(missing_ok=True)


def export(paths, receipt_path, output):
    """Publish a fresh portable handoff directory from the preparation job."""
    receipt = load_receipt(receipt_path)
    verify_outputs(paths, receipt)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('prepared host export directory must be fresh')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.' + output.name + '.', dir=output.parent))
    try:
        for name, filename in OUTPUT_NAMES.items():
            copy_verified(paths[name], temporary / filename, receipt['outputs'][name])
        (temporary / 'receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
        temporary.chmod(0o755)
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify(request, prepared, receipt_path, outputs, output_receipt):
    """Reject stale inputs and corrupt output bytes before final packaging."""
    receipt = load_receipt(receipt_path)
    if request['image_store_backend'] != 'containerd':
        raise ValueError('prepared host assembly requires the containerd backend')
    config = host.load_config(request['config'])
    if host.image_names(config['environment']) != (set(request['metadata']), set(request['local_images'])):
        raise ValueError('declared image inputs do not match the native service configuration')
    identity = host.input_identity(request)
    if receipt['inputs'] != identity or receipt.get('snapshot_identity') != config['identity']:
        raise ValueError('prepared host inputs changed; rerun native host preparation')
    verify_outputs(prepared, receipt)
    for name in OUTPUT_NAMES:
        copy_verified(prepared[name], outputs[name], receipt['outputs'][name])
    if host.input_identity(request) != identity:
        raise ValueError('host inputs changed during prepared host verification')
    result = dict(receipt, prepared_host_verified=True,
                  scope='Verified prepared host inputs and output bytes without privileged operations')
    destination = Path(output_receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    exporter = commands.add_parser('export')
    for name in ('fs', 'boot', 'platform', 'receipt', 'output'):
        exporter.add_argument('--' + name, required=True)
    verifier = commands.add_parser('verify')
    for name in ('source', 'snapshot', 'config', 'build-script', 'extension-template',
                 'execution-environment', 'runtime-overlay', 'offline-host', 'fs',
                 'boot', 'platform', 'receipt', 'prepared-fs', 'prepared-boot',
                 'prepared-platform', 'prepared-receipt'):
        verifier.add_argument('--' + name, required=True)
    verifier.add_argument('--image-store-backend', choices=('containerd',), required=True)
    verifier.add_argument('--metadata', action='append', default=[])
    verifier.add_argument('--local-image', action='append', default=[])
    args = parser.parse_args()
    if args.command == 'export':
        export({key: getattr(args, key) for key in OUTPUT_NAMES}, args.receipt, args.output)
        return
    keys = ('source', 'snapshot', 'config', 'execution_environment', 'build_script',
            'extension_template', 'runtime_overlay', 'offline_host', 'image_store_backend')
    request = {key: getattr(args, key) for key in keys}
    request.update(metadata=host.mappings(args.metadata), local_images=host.mappings(args.local_image))
    verify(request, {key: getattr(args, 'prepared_' + key) for key in OUTPUT_NAMES},
           args.prepared_receipt, {key: getattr(args, key) for key in OUTPUT_NAMES}, args.receipt)


if __name__ == '__main__':
    main()
