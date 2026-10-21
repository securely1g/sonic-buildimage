"""Connect a checked OCI input to upstream umoci and BSD tar.

Umoci handles all OCI extraction and whiteout semantics. The temporary index
adds a local tag to the already-verified descriptor; image blobs remain intact.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from tools.bazel.oci.oci_layout import validate_layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('image', 'umoci', 'tar', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    environment = 'debian:trixie-20260918@sha256:9cc080028c43b27d2074d63a5f9caf7166d731494965616c1a6d2827a004585c'
    if os.geteuid() != 0 or os.environ.get('APT_INSTALL_BUILD_CONTAINER') != environment:
        parser.error('Use the declared native root build container to preserve file ownership')
    checked = validate_layout(args.image, args.platform)
    with tempfile.TemporaryDirectory(prefix='upstream-oci-unpack-') as temporary:
        root = Path(temporary)
        layout = root / 'layout'
        (layout / 'blobs').mkdir(parents=True)
        for algorithm in (args.image / 'blobs').iterdir():
            if algorithm.is_dir():
                (layout / 'blobs' / algorithm.name).mkdir()
                for blob in algorithm.iterdir():
                    (layout / 'blobs' / algorithm.name / blob.name).symlink_to(blob.resolve())
        shutil.copyfile(args.image / 'oci-layout', layout / 'oci-layout')
        descriptor = dict(checked.descriptor)
        descriptor['annotations'] = dict(descriptor.get('annotations', {}), **{'org.opencontainers.image.ref.name': 'rootfs'})
        (layout / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [descriptor]}))
        bundle = root / 'bundle'
        subprocess.run([str(args.umoci), 'unpack', '--image', str(layout) + ':rootfs', str(bundle)], check=True)
        subprocess.run([str(args.tar), '--format=pax', '-cf', str(args.output), '-C', str(bundle / 'rootfs'), '.'], check=True)


if __name__ == '__main__':
    main()
