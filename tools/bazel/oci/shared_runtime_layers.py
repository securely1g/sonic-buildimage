"""Factor identical regular files into a shared layer without merging image receipts."""

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile


def path_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe layer path: " + name)
    return path.as_posix()


def member_metadata(member):
    return (member.type, member.size, member.mode, member.uid, member.gid,
            member.uname, member.gname, member.mtime, member.linkname,
            member.devmajor, member.devminor,
            tuple(sorted((key, value) for key, value in member.pax_headers.items()
                         if key not in ("path", "linkpath"))))


def inventory(path, directories=None):
    entries, blocked = {}, set()
    fallback = None
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            name = path_name(member.name)
            if name in entries:
                fallback = "duplicate paths"
            if PurePosixPath(name).name.startswith(".wh."):
                fallback = "whiteouts"
            if member.issparse():
                fallback = "sparse files"
            if member.islnk():
                try:
                    blocked.add(path_name(member.linkname.lstrip("/")))
                except ValueError:
                    fallback = "complex hardlinks"
            digest = None
            if member.isfile() and not member.issparse():
                with archive.extractfile(member) as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
            entries[name] = (member_metadata(member), digest)
            if member.isdir() and directories is not None:
                directories[name] = member
    return entries, blocked, fallback


@contextmanager
def gzip_output(path, pigz):
    with path.open("wb") as output, subprocess.Popen(
            [pigz, "-n", "-6", "-p", "2", "-c"],
            stdin=subprocess.PIPE, stdout=output) as process:
        with process.stdin:
            yield process.stdin
        if process.wait():
            raise ValueError("failed to compress shared runtime layer")


@contextmanager
def compressed_tar(path, pigz):
    with gzip_output(path, pigz) as output, tarfile.open(
            fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        yield archive


def original_layer(source, destination, pigz):
    with source.open("rb") as stream:
        compressed = stream.read(2) == b"\x1f\x8b"
    if compressed:
        shutil.copyfile(source, destination)
    else:
        with source.open("rb") as stream, gzip_output(destination, pigz) as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)


def split(first, second, common_out, first_out, second_out, receipt, pigz):
    outputs = [path.resolve() for path in (common_out, first_out, second_out, receipt)]
    if len(set(outputs)) != len(outputs) or set(outputs) & {first.resolve(), second.resolve()}:
        raise ValueError("layer outputs must be distinct from each other and the inputs")
    directories = {}
    first_entries, first_blocked, first_fallback = inventory(first, directories)
    second_entries, second_blocked, second_fallback = inventory(second)
    blocked = first_blocked | second_blocked
    common = set()
    fallback = first_fallback or second_fallback
    if not fallback:
        for name in first_entries.keys() & second_entries.keys():
            _, digest = first_entries[name]
            if digest is None or name in blocked or first_entries[name] != second_entries[name]:
                continue
            # Parent directories must precede shared files even when the base
            # has a symlink there. Keep links and their targets in the delta.
            parents = [str(parent) for parent in PurePosixPath(name).parents if str(parent) != "."]
            if any(parent not in directories or
                   first_entries[parent] != second_entries.get(parent) for parent in parents):
                continue
            common.add(name)
    if not common:
        fallback = fallback or "no identical movable files"
        with compressed_tar(common_out, pigz):
            pass
        original_layer(first, first_out, pigz)
        original_layer(second, second_out, pigz)
    else:
        with compressed_tar(common_out, pigz) as shared, compressed_tar(first_out, pigz) as delta:
            parents = {str(parent) for name in common for parent in PurePosixPath(name).parents
                       if str(parent) != "."}
            for name in sorted(parents, key=lambda name: (len(PurePosixPath(name).parts), name)):
                shared.addfile(directories[name])
            with tarfile.open(first, "r|*") as archive:
                for member in archive:
                    target = shared if path_name(member.name) in common else delta
                    if member.isfile():
                        with archive.extractfile(member) as source:
                            target.addfile(member, source)
                    else:
                        target.addfile(member)
        with compressed_tar(second_out, pigz) as delta, tarfile.open(second, "r|*") as archive:
            for member in archive:
                if path_name(member.name) in common:
                    continue
                if member.isfile():
                    with archive.extractfile(member) as source:
                        delta.addfile(member, source)
                else:
                    delta.addfile(member)
    receipt.write_text(json.dumps({
        "schema": 1,
        "common_files": len(common),
        "common_file_bytes": sum(first_entries[name][0][1] for name in common),
        "fallback": fallback,
        "output_bytes": {
            "common": common_out.stat().st_size,
            "first_delta": first_out.stat().st_size,
            "second_delta": second_out.stat().st_size,
        },
    }, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("first", "second", "common-out", "first-out", "second-out", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--pigz", required=True)
    args = parser.parse_args()
    split(args.first, args.second, args.common_out, args.first_out, args.second_out,
          args.receipt, args.pigz)


if __name__ == "__main__":
    main()
