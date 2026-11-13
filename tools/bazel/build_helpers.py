"""Shared cache settings and atomic archive publication for Make/Bazel builds."""

import argparse
import filecmp
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.bazel.cache import credentials


def cache_options(cache_directory, remote_only=False):
    """Share content-addressed caches, keeping each Bazel output base private."""
    if not cache_directory:
        return []
    root = Path(cache_directory).expanduser().resolve()
    options = []
    names = ("repository_cache",) if remote_only else ("repository_cache", "disk_cache")
    for name in names:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        options.append(f"--{name}={path}")
    return options


def remote_cache_options(required=False):
    """Read the shared cache without exposing authentication in Bazel options."""
    if not os.environ.get("BAZEL_REMOTE_AUTH"):
        if required:
            raise ValueError(credentials.ERROR)
        return []
    credentials.authorization()
    helper = Path(__file__).resolve().parent / "cache/credentials.py"
    if required:
        try:
            valid_helper = (helper.resolve() == helper and helper.is_file() and
                            os.access(helper, os.X_OK) and
                            Path(credentials.__file__).resolve() == helper)
        except (OSError, RuntimeError):
            valid_helper = False
        if not valid_helper:
            raise ValueError(credentials.ERROR)
    return [
        "--remote_cache=" + credentials.ENDPOINT + "/",
        "--credential_helper=" + credentials.HOST + "=" + str(helper),
        "--remote_executor=",
        "--remote_accept_cached=true",
        "--remote_upload_local_results=false",
    ] + (["--disk_cache="] if required else [])


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
        policy = os.environ.get("SONIC_BAZEL_CACHE_POLICY", "")
        if policy not in ("", "remote-only"):
            parser.error("SONIC_BAZEL_CACHE_POLICY must be empty or remote-only")
        remote_only = policy == "remote-only"
        required_options = []
        if remote_only:
            try:
                # Refuse missing credentials before creating working directories.
                required_options = remote_cache_options(required=True)
            except ValueError:
                parser.exit(2, credentials.ERROR + "\n")
        # In K3s the caller supplies a transient repository/output location.
        # Do not create, remove or reuse a local action-cache directory here.
        arguments = cache_options(args.cache_directory, remote_only=remote_only) + shlex.split(args.arguments)
        try:
            # Apply the shared cache policy after caller options so Make image
            # builds cannot accidentally upload results or enable remote execution.
            arguments += required_options if remote_only else remote_cache_options()
        except ValueError:
            parser.exit(2, credentials.ERROR + "\n")
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
