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


def build(archive, destination, bazel="bazel", startup=(), options=(), workspace=ROOT):
    if archive not in ARCHIVES:
        raise ValueError("unsupported SWSS archive: " + archive)
    for name in ("target/docker-config-engine-trixie.gz",
                 "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl"):
        if not (workspace / name).is_file():
            raise ValueError("Make prerequisite is missing: " + name)
    target = "//dockers/docker-orchagent:" + archive
    command = [bazel, *startup]
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
    parser.add_argument("--bazel-startup-arg", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    args = parser.parse_args()
    build(args.archive, args.output.resolve(), args.bazel, args.bazel_startup_arg,
          [*shlex.split(os.environ.get("BAZEL_SWSS_ARGS", "")), *args.bazel_arg])


if __name__ == "__main__":
    main()
