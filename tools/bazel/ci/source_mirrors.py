#!/usr/bin/env python3
"""Reuse the registry's verified source mirrors with Bazel's normal downloader."""

import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
URL = "https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/9697f3db62fc58a7a70b0d876abe0a849133a00d/ci/source_mirrors.cfg"
SHA256 = "622830b8adf42d229a53b60ea0956de4a4a3c119d0639479be78817994d67327"


def verify(configuration):
    """Accept only the selected shared policy; source hashes remain Bazel inputs."""
    if hashlib.sha256(configuration).hexdigest() != SHA256:
        raise ValueError("Source mirror configuration checksum mismatch")
    return configuration


def download():
    with urlopen(URL, timeout=60) as response:
        return verify(response.read())


def prepare(output, local=ROOT / "tools/bazel/debian_mirrors.cfg"):
    """Keep this repository's Debian mirrors first, then add the shared policy."""
    shared = download()
    configuration = local.read_bytes() + b"\n" + shared
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(configuration)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output)


if __name__ == "__main__":
    main()
