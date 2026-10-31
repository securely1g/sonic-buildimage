#!/usr/bin/env python3
"""Verify the pinned native P4 source inputs without changing any files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


MANIFEST = Path(__file__).with_name("source-cache.json")


def verify(source_dir, manifest, package=None, version=None, upstream_version=None):
    if manifest.get("schema") != 1:
        raise ValueError("unsupported source manifest")
    packages = manifest["packages"]
    if package is not None:
        if package not in packages:
            raise ValueError("unsupported source package")
        selected = packages[package]
        if (version, upstream_version) != (selected["version"], selected["upstream_version"]):
            raise ValueError("unsupported pinned local P4 source version")
        packages = {package: selected}
    elif version is not None or upstream_version is not None:
        raise ValueError("version selection requires a package")
    verified = []
    for name, metadata in packages.items():
        expected_names = {
            f"{name}_{metadata['version']}.dsc",
            f"{name}_{metadata['upstream_version']}.orig.tar.gz",
            f"{name}_{metadata['version']}.debian.tar.xz",
        }
        if set(metadata["files"]) != expected_names:
            raise ValueError("source manifest must name the exact source triplet")
        for filename, expected in metadata["files"].items():
            if Path(filename).name != filename:
                raise ValueError("invalid source filename")
            try:
                descriptor = os.open(source_dir / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size != expected["bytes"]:
                        raise ValueError("source type or size mismatch: " + filename)
                    digest = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as error:
                raise ValueError("cannot read required regular source: " + filename) from error
            if digest.hexdigest() != expected["sha256"]:
                raise ValueError("source SHA256 mismatch: " + filename)
            verified.append({"package": name, "filename": filename, **expected})
    return {"status": "verified", "files": verified}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("verify", help="verify all nine inputs, or one exact package triplet")
    check.add_argument("--source-dir", required=True, type=Path)
    check.add_argument("--package")
    check.add_argument("--version")
    check.add_argument("--upstream-version")
    args = parser.parse_args(argv)
    try:
        manifest_bytes = MANIFEST.read_bytes()
        receipt = verify(args.source_dir, json.loads(manifest_bytes), args.package,
                         args.version, args.upstream_version)
        receipt["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("P4 source verification failed: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
