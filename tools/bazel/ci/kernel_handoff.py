#!/usr/bin/env python3
"""Transfer verified kernel packages between CI jobs without building a kernel."""

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import public_artifacts
from tools.bazel.kernel import prepare as kernel


def directory(path):
    path = Path(path)
    kernel.require(not path.is_symlink() and path.is_dir(),
                   "kernel handoff directory must be an ordinary directory")
    return path.resolve(strict=True)


def names_for(workspace, architecture):
    # These CI jobs use the same VS/Trixie/no_sign defaults as native Make.
    return set(kernel.contract(workspace, architecture)[1]) | {kernel.MANIFEST, kernel.PROVENANCE}


def verify(directory, workspace, source, names, *, exact):
    if exact:
        kernel.require({path.name for path in directory.iterdir()} == names,
                       "kernel handoff must contain exactly five DEBs and two metadata files")
    for name in names:
        kernel.regular(directory / name)
    # Reject ambiguous JSON and recorded credentials/diagnostics before these
    # metadata files cross the CI artifact boundary. Preserve their exact bytes.
    for name in (kernel.MANIFEST, kernel.PROVENANCE):
        value = public_artifacts.load_json((directory / name).read_bytes())
        kernel.require(isinstance(value, dict), "kernel handoff metadata must be a JSON object")
        public_artifacts.safe_json(value)
    result = kernel.verify(directory, workspace, source=source,
                           architecture=source["launcher"]["architecture"])
    kernel.require(result["provenance"]["source_commit"] == source["source_commit"],
                   "kernel CI handoff must come from the same source commit")
    return result


def copy_files(source, destination, names):
    for name in sorted(names):
        # Do not follow a symlink or block on a special file if an input changes
        # between validation and copying. Verify the complete staged set again.
        descriptor = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as incoming:
            kernel.require(stat.S_ISREG(os.fstat(incoming.fileno()).st_mode),
                           "kernel handoff inputs must be regular files")
            with (destination / name).open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            (destination / name).chmod(0o644)


def unchanged_source(workspace, source, architecture):
    kernel.require(kernel.source_state(workspace, architecture) == source,
                   "kernel handoff source changed during transfer")


def export_bundle(workspace, debs_directory, output, architecture="amd64"):
    workspace = directory(workspace)
    packages = directory(workspace / debs_directory)
    output = Path(output).absolute()
    kernel.require(not output.exists() and not output.is_symlink(),
                   "kernel handoff output already exists")
    with kernel.package_lock(workspace):
        source = kernel.source_state(workspace, architecture)
        names = names_for(workspace, architecture)
        verify(packages, workspace, source, names, exact=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".kernel-handoff-", dir=output.parent) as temporary:
            staged = Path(temporary) / "packages"
            staged.mkdir()
            copy_files(packages, staged, names)
            result = verify(staged, workspace, source, names, exact=True)
            unchanged_source(workspace, source, architecture)
            staged.rename(output)
        return result


def import_bundle(workspace, incoming, debs_directory, architecture="amd64"):
    workspace = directory(workspace)
    incoming = directory(incoming)
    with kernel.package_lock(workspace):
        source = kernel.source_state(workspace, architecture)
        names = names_for(workspace, architecture)
        verify(incoming, workspace, source, names, exact=True)
        destination = kernel.debs_path(workspace, debs_directory)
        kernel.require(destination != incoming, "kernel handoff input and package destination must differ")
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".kernel-handoff-", dir=destination) as temporary:
            staged = Path(temporary)
            copy_files(incoming, staged, names)
            verify(staged, workspace, source, names, exact=True)
            unchanged_source(workspace, source, architecture)
            changed = []
            for name in sorted(names):
                existing = destination / name
                kernel.require(not existing.is_symlink()
                               and (not existing.exists() or existing.is_file()),
                               "kernel package destination must contain regular files")
                if not existing.exists() or kernel.sha256(existing) != kernel.sha256(staged / name):
                    changed.append(name)
            if changed:
                # Match Make's publication protocol: provenance is the last
                # completion marker, and a partial copy cannot retain an old one.
                (destination / kernel.PROVENANCE).unlink(missing_ok=True)
                for name in changed:
                    if name != kernel.PROVENANCE:
                        (staged / name).replace(destination / name)
                (staged / kernel.PROVENANCE).replace(destination / kernel.PROVENANCE)
            try:
                result = verify(destination, workspace, source, names, exact=False)
                unchanged_source(workspace, source, architecture)
            except Exception:
                (destination / kernel.PROVENANCE).unlink(missing_ok=True)
                raise
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("export", "import"):
        child = commands.add_parser(command)
        child.add_argument("--workspace", type=Path, default=Path.cwd())
        child.add_argument("--debs-directory", type=Path, default=kernel.DEBS_DIRECTORY)
        child.add_argument("--architecture", choices=("amd64", "arm64"), default="amd64")
        child.add_argument("--output" if command == "export" else "--input", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "export":
        result = export_bundle(args.workspace, args.debs_directory, args.output, args.architecture)
    else:
        result = import_bundle(args.workspace, args.input, args.debs_directory, args.architecture)
    print(json.dumps({"command": args.command, "architecture": args.architecture,
                      "source_commit": result["provenance"]["source_commit"],
                      "verified_packages": len(result["manifest"]["packages"])}, sort_keys=True))


if __name__ == "__main__":
    main()
