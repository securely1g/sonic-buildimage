"""Use upstream umoci to preserve base config and encode filesystem replacements.

APT supplies the complete configured filesystem. Umoci compares it with the
original base, creates the required whiteouts and updates OCI digests. The
following rules_oci image applies only the owner's ordinary config overrides.
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
    for name in ('base', 'rootfs', 'umoci', 'tar', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    environment = 'debian:trixie-20260918@sha256:9cc080028c43b27d2074d63a5f9caf7166d731494965616c1a6d2827a004585c'
    if os.geteuid() != 0 or os.environ.get('APT_INSTALL_BUILD_CONTAINER') != environment:
        parser.error('Use the declared native root build container to preserve file ownership')
    checked = validate_layout(args.base, args.platform)
    with tempfile.TemporaryDirectory(prefix='upstream-oci-repack-') as temporary:
        # Work on private copies. Upstream umoci owns all generated OCI blobs
        # and manifests; the added local tag names the verified input image.
        layout = Path(temporary) / 'layout'
        shutil.copytree(args.base, layout)
        descriptor = dict(checked.descriptor)
        descriptor['annotations'] = dict(descriptor.get('annotations', {}), **{'org.opencontainers.image.ref.name': 'rootfs'})
        (layout / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [descriptor]}))
        bundle = Path(temporary) / 'bundle'
        subprocess.run([str(args.umoci), 'unpack', '--image', str(layout) + ':rootfs', str(bundle)], check=True)
        shutil.rmtree(bundle / 'rootfs')
        (bundle / 'rootfs').mkdir()
        subprocess.run([str(args.tar), '-xf', str(args.rootfs), '-C', str(bundle / 'rootfs')], check=True)
        subprocess.run([str(args.umoci), 'repack', '--image', str(layout) + ':rootfs',
                        '--no-mask-volumes', '--history.created=1970-01-01T00:00:00Z',
                        '--history.created_by=Install original Debian packages with APT', str(bundle)], check=True)
        validate_layout(layout, args.platform)
        shutil.copytree(layout, args.output, dirs_exist_ok=True)


if __name__ == '__main__':
    main()
