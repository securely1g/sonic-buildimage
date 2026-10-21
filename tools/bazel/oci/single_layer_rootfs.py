"""Expose the pinned Debian image's single existing filesystem archive.

This copies a validated OCI blob verbatim. It never merges layers or implements
whiteouts. A future base with multiple layers must use an OCI materializer.
"""

import argparse
from pathlib import Path
import shutil

from tools.bazel.oci.oci_layout import validate_layout


def copy_rootfs(image, architecture, output):
    layout = validate_layout(image, "linux/" + architecture)
    if len(layout.layers) != 1:
        raise ValueError("Expected one complete Debian filesystem layer; materialize multi-layer images first")
    media_type = layout.manifest["layers"][0].get("mediaType")
    if media_type != "application/vnd.oci.image.layer.v1.tar+gzip":
        raise ValueError("Pinned Debian filesystem must be a gzip TAR layer")
    shutil.copyfile(layout.layers[0], output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--architecture", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    copy_rootfs(args.image, args.architecture, args.output)


if __name__ == "__main__":
    main()
