"""Shared cache settings and atomic archive publication for Make/Bazel builds."""

import argparse
import filecmp
from pathlib import Path
import shlex
import shutil
import tempfile


def cache_options(cache_directory):
    """Share content-addressed caches, keeping each Bazel output base private."""
    if not cache_directory:
        return []
    root = Path(cache_directory).expanduser().resolve()
    options = []
    for name, flag in (("repository_cache", "repository_cache"), ("disk_cache", "disk_cache")):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    options = commands.add_parser("options", help="Print one Bazel option per line")
    options.add_argument("--cache-directory")
    options.add_argument("--arguments", default="", help="Additional shell-quoted Bazel options")
    export = commands.add_parser("export", help="Publish the single archive reported by cquery")
    export.add_argument("--query-output", required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "options":
        arguments = cache_options(args.cache_directory) + shlex.split(args.arguments)
        if any(not value or any(char in value for char in "\r\n\0") for value in arguments):
            parser.error("Bazel options must be nonempty single-line arguments")
        for value in arguments:
            print(value)
    else:
        files = args.query_output.splitlines()
        if len(files) != 1:
            parser.error("expected exactly one Bazel archive output")
        source = Path(files[0])
        if not source.is_file() or not source.stat().st_size:
            parser.error("Bazel did not produce a nonempty archive")
        export_archive(source, args.output)


if __name__ == "__main__":
    main()
