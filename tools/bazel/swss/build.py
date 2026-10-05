#!/usr/bin/env python3
"""Build an SWSS OCI image and export the archive consumed by Make."""

import argparse
import filecmp
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[3]
ARCHIVES = ("docker-orchagent.gz", "docker-orchagent-dbg.gz")


def cache_options(cache_directory):
    """Share content-addressed caches, keeping each Bazel output base private."""
    if not cache_directory:
        return []
    root = Path(cache_directory).expanduser().resolve()
    options = []
    for name, flag in (("repository", "repository_cache"), ("disk", "disk_cache")):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        options.append(f"--{flag}={path}")
    return options


def export_archive(source, destination):
    """Leave the previous image intact if copying a new archive fails."""
    if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".",
                                     delete=False) as output:
        temporary = Path(output.name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def build(archive, destination, bazel="bazel", startup=(), options=(), workspace=ROOT,
          cache_directory=None):
    if archive not in ARCHIVES:
        raise ValueError("unsupported SWSS archive: " + archive)
    for name in ("target/docker-config-engine-trixie.oci/index.json",
                 "target/docker-config-engine-trixie.oci/oci-layout",
                 "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl"):
        if not (workspace / name).is_file():
            raise ValueError("Make prerequisite is missing: " + name)
    target = "//dockers/docker-orchagent:" + archive
    command = [bazel, *startup]
    options = [*cache_options(cache_directory), *options]
    subprocess.run([*command, "build", *options, target], cwd=workspace, check=True)
    result = subprocess.run([*command, "cquery", *options, "--output=files", target],
                            cwd=workspace, check=True, text=True, stdout=subprocess.PIPE)
    files = result.stdout.splitlines()
    if len(files) != 1:
        raise ValueError("expected exactly one SWSS archive output")
    source = workspace / files[0]
    if not source.is_file() or not source.stat().st_size:
        raise ValueError("Bazel did not produce a nonempty SWSS archive")
    export_archive(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", choices=ARCHIVES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bazel", default=os.environ.get("BAZEL", "bazel"))
    parser.add_argument("--cache-directory", default=os.environ.get("BAZEL_SWSS_CACHE_DIR"))
    parser.add_argument("--bazel-startup-arg", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    args = parser.parse_args()
    build(args.archive, args.output.resolve(), args.bazel, args.bazel_startup_arg,
          [*shlex.split(os.environ.get("BAZEL_SWSS_ARGS", "")), *args.bazel_arg],
          cache_directory=args.cache_directory)


if __name__ == "__main__":
    main()
