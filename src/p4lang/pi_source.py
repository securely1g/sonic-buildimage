#!/usr/bin/env python3
"""Stage pinned official PI Git sources and packaging, without OBS equivalence claims."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


VERSION = "0.1.3-2"
COMPONENTS = {
    ".": "p4lang/PI",
    "proto/openconfig/gnmi": "openconfig/gnmi",
    "proto/openconfig/public": "openconfig/public",
    "proto/p4runtime": "p4lang/p4runtime",
    "third_party/googletest": "google/googletest",
    "third_party/uthash": "troydhanson/uthash",
    "packaging": "p4lang/packages",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_manifest(manifest):
    require(set(manifest) == {"version", "archives"} and manifest["version"] == VERSION,
            "unsupported PI source manifest")
    archives = manifest["archives"]
    require(len(archives) == len(COMPONENTS), "PI manifest must contain all seven archives")
    require([item["destination"] for item in archives] == list(COMPONENTS),
            "unexpected or missing PI source component")
    for item in archives:
        require(set(item) == {"destination", "commit", "url", "sha256", "bytes", "prefix"},
                "unexpected archive metadata")
        repository = COMPONENTS[item["destination"]]
        require(re.fullmatch(r"[0-9a-f]{40}", item["commit"]), "invalid Git commit")
        require(item["url"] == "https://codeload.github.com/" + repository + "/legacy.tar.gz/" + item["commit"],
                "source URL must select the declared official Git commit")
        require(item["prefix"] == repository.replace("/", "-") + "-" + item["commit"][:7],
                "unexpected archive root")
        require(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), "invalid SHA256")
        require(type(item["bytes"]) is int and 0 < item["bytes"] <= 16 * 1024 * 1024,
                "invalid source archive size")
    return archives


def download(item, path):
    # No GitHub token or CLI configuration is required. Preserve normal CA
    # verification, including an explicitly configured native execution CA.
    subprocess.run([
        "/usr/bin/curl", "--disable", "--fail", "--location", "--silent", "--show-error",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "20",
        "--max-time", "120", "--retry", "2", "--retry-max-time", "300",
        "--max-filesize", str(item["bytes"]), "--output", str(path), item["url"],
    ], check=True, timeout=360)


def relative_path(name):
    path = PurePosixPath(name)
    require(name and not path.is_absolute() and str(path) == name and
            all(part not in ("..", ".git") for part in path.parts) and "\0" not in name,
            "unsafe source archive path: " + repr(name))
    return path


def extract(archive, destination, prefix):
    """Extract regular Git files and safe relative symlinks without following links."""
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        entries = {}
        total = 0
        for member in source:
            name = member.name.rstrip("/")
            path = relative_path(name)
            require(path.parts[0] == prefix, "unexpected source archive prefix")
            if len(path.parts) == 1:
                require(member.isdir(), "archive root must be a directory")
                continue
            relative = PurePosixPath(*path.parts[1:])
            require(relative not in entries, "duplicate source archive path")
            require(member.isfile() or member.isdir() or member.issym(), "unsupported source archive entry")
            require(not member.mode & 0o7000 and not member.sparse, "unsupported source archive metadata")
            if member.issym():
                target = PurePosixPath(member.linkname)
                require(member.linkname and not target.is_absolute() and ".git" not in target.parts,
                        "unsafe source symlink")
                stack = list(relative.parent.parts)
                for part in target.parts:
                    if part == "..":
                        require(stack, "source symlink escapes its component")
                        stack.pop()
                    elif part != ".":
                        stack.append(part)
            total += member.size
            require(total <= 128 * 1024 * 1024 and len(entries) < 20000, "oversized source archive")
            entries[relative] = member
        require(entries, "empty source archive")
        for path in entries:
            require(all(parent not in entries or entries[parent].isdir() for parent in path.parents),
                    "archive entry has a non-directory ancestor")
        # Regular files precede symlinks, and no entry can traverse a symlink.
        for path, member in sorted(entries.items(), key=lambda pair: (pair[1].issym(), len(pair[0].parts), str(pair[0]))):
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.issym():
                target.symlink_to(member.linkname)
            else:
                with source.extractfile(member) as original, target.open("xb") as output:
                    shutil.copyfileobj(original, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        for path, member in entries.items():
            if member.issym():
                try:
                    try:
                        # Python 3.13 leaves loops unresolved in non-strict mode.
                        resolved = (destination / path).resolve(strict=True)
                    except FileNotFoundError:
                        # Root PI links can target submodules installed later.
                        resolved = (destination / path).resolve(strict=False)
                except (OSError, RuntimeError) as error:
                    raise ValueError("invalid source symlink chain") from error
                require(resolved.is_relative_to(destination.resolve()),
                        "source symlink chain escapes its component")


def stage(manifest, output, fetch=download):
    archives = validate_manifest(manifest)
    output = Path(output).absolute()
    require(output.name == "p4lang-pi-0.1.3" and output.parent.resolve(strict=True) == output.parent,
            "unexpected PI destination or linked parent")
    require(not os.path.lexists(output), "PI source destination already exists")
    with tempfile.TemporaryDirectory(prefix=".pi-source-", dir=output.parent) as temporary:
        scratch = Path(temporary)
        trees = {}
        for number, item in enumerate(archives):
            archive = scratch / (str(number) + ".tar.gz")
            fetch(item, archive)
            require(archive.is_file() and not archive.is_symlink(), "source download is not a regular file")
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            require(archive.stat().st_size == item["bytes"] and digest == item["sha256"],
                    "PI archive failed size/SHA256 validation: " + item["destination"])
            tree = scratch / str(number)
            extract(archive, tree, item["prefix"])
            trees[item["destination"]] = tree
        source = trees.pop(".")
        packaging = trees.pop("packaging") / "p4lang-pi"
        require(packaging.is_dir() and not packaging.is_symlink(), "missing official PI packaging")
        require((packaging / "changelog").read_text().splitlines()[0].startswith("p4lang-pi (" + VERSION + ") "),
                "official PI packaging version differs")
        require(not os.path.lexists(source / "debian"), "unexpected upstream Debian packaging")
        packaging.rename(source / "debian")
        for name, tree in trees.items():
            target = source / name
            require(target.parent.resolve() == target.parent and target.parent.is_dir(),
                    "missing or linked submodule parent")
            if os.path.lexists(target):
                require(target.is_dir() and not target.is_symlink() and not any(target.iterdir()),
                        "submodule destination is not empty")
                target.rmdir()  # Only an empty placeholder inside this temporary export.
            tree.rename(target)
        for parent, directories, files in os.walk(source, followlinks=False):
            for name in directories + files:
                path = Path(parent) / name
                require(name != ".git", "Git metadata in source export")
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o755)
                os.utime(path, (0, 0), follow_symlinks=False)
        source.chmod(0o755)
        os.utime(source, (0, 0))
        # Exclusive creation protects even an existing empty directory or a
        # broken symlink. Only our temporary downloads are removed on failure.
        output.mkdir()
        shutil.copytree(source, output, symlinks=True, dirs_exist_ok=True)
    return {"status": "passed", "version": VERSION, "archives": archives,
            "scope": "Pinned official Git sources and packaging plus declared recursive gitlinks; no OBS byte-equivalence claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("pi_source.json").read_text())
    print(json.dumps(stage(manifest, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
