#!/usr/bin/env python3
"""Copy receipt-bound installer inputs into declared Bazel outputs."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

SCHEMA = 2
INPUT_DIRECTORY = "target/bazel-image-inputs"
GRAPH_DIRECTORY = "target/bazel-image-graphs"
GRAPH_CONTROLS = {
    INPUT_DIRECTORY + "/BUILD.bazel",
    INPUT_DIRECTORY + "/inputs.bzl",
    "tools/bazel/image/vs/BUILD.bazel",
    "tools/bazel/image/vs/BUILD.bazel.in",
}
REQUIRED_INPUTS = {
    "execution-environment.json", "host-config.json", "host-onie.squashfs",
    "host-source.tar", "installer/config.json", "installer/files-manifest.json",
    "installer/provenance.json",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def relative_name(name):
    require(isinstance(name, str), "input name must be text")
    relative = PurePosixPath(name)
    require(relative.parts and str(relative) == name and not relative.is_absolute() and ".." not in relative.parts and
            "\\" not in name and ":" not in name and "\0" not in name,
            "invalid input name: " + name)
    return name


def identity(metadata, name):
    require(isinstance(metadata, dict) and type(metadata.get("bytes")) is int and
            metadata["bytes"] >= 0 and isinstance(metadata.get("sha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]),
            "invalid input metadata: " + name)
    return {key: metadata[key] for key in ("bytes", "sha256")}


def bytes_identity(value):
    return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}


def file_identity(path):
    require(path.is_file(), "missing regular input: " + str(path))
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate provenance key: " + key)
        value[key] = item
    return value


def parse_receipt(value):
    receipt = json.loads(value, object_pairs_hook=unique_object)
    require(isinstance(receipt, dict) and receipt.get("schema") == SCHEMA,
            "unbound or unsupported image input bundle; move it and the generated tools/bazel/image/vs/BUILD.bazel "
            "aside, then prepare a fresh receipt-bound bundle")
    require(isinstance(receipt.get("native_config_flags"), dict),
            "image input receipt omitted native configuration flags")
    files = receipt.get("files")
    require(isinstance(files, dict) and REQUIRED_INPUTS <= files.keys(),
            "image input receipt omitted required staged inputs")
    for name, metadata in files.items():
        relative_name(name)
        require(name not in {"BUILD.bazel", "inputs.bzl", "provenance.json"},
                "graph control cannot be a staged input: " + name)
        require(PurePosixPath(name).name not in {"BUILD", "BUILD.bazel"},
                "staged input cannot define a Bazel subpackage: " + name)
        identity(metadata, name)
    controls = receipt.get("graph_controls")
    require(isinstance(controls, dict) and set(controls) == GRAPH_CONTROLS,
            "image input receipt must bind exactly the generated graph controls")
    for name, metadata in controls.items():
        identity(metadata, name)
    require(identity(controls["tools/bazel/image/vs/BUILD.bazel"], "active VS graph") ==
            identity(controls["tools/bazel/image/vs/BUILD.bazel.in"], "VS graph template"),
            "active VS BUILD.bazel must match its bound template")
    return receipt


def load_receipt(path):
    require(path.is_file(), "prepare a receipt-bound native VS input bundle first")
    value = path.read_bytes()
    return value, parse_receipt(value)


def verify_bundle(workspace):
    """Check the complete bundle inventory and graph controls before Bazel loads it."""
    workspace = workspace.absolute()
    directory = workspace / INPUT_DIRECTORY
    require(directory.is_dir() and directory.resolve(strict=True) == directory,
            "prepare a fresh unlinked receipt-bound native VS input bundle first")
    receipt_path = directory / "provenance.json"
    require(not receipt_path.is_symlink(), "image input receipt is linked")
    value, receipt = load_receipt(receipt_path)
    expected = set(receipt["files"]) | {"BUILD.bazel", "inputs.bzl", "provenance.json"}
    directories = {str(parent) for name in expected for parent in PurePosixPath(name).parents
                   if str(parent) != "."}
    observed = set()
    for path in directory.rglob("*"):
        name = path.relative_to(directory).as_posix()
        require(not path.is_symlink(), "prepared input is linked: " + name)
        if path.is_dir():
            require(name in directories, "unexpected prepared input directory: " + name)
        else:
            require(path.is_file(), "prepared input is not a regular file: " + name)
            observed.add(name)
    require(observed == expected,
            "prepared input inventory differs from receipt; missing=" + str(sorted(expected - observed)) +
            ", extra=" + str(sorted(observed - expected)))
    for name in sorted(GRAPH_CONTROLS):
        path = workspace / name
        require(path.is_file() and not path.is_symlink() and path.resolve(strict=True) == path,
                "missing or linked graph control: " + name)
        require(file_identity(path) == identity(receipt["graph_controls"][name], name),
                "graph control failed size/SHA256 validation: " + name)
    require(receipt_path.read_bytes() == value, "image input provenance changed while checking graph controls")
    return value, receipt


def copy_verified_file(source, destination, metadata):
    """Hash the bytes being copied, so later source changes cannot affect an output."""
    expected = identity(metadata, str(source))
    require(source.is_file(), "missing regular input: " + str(source))
    digest = hashlib.sha256()
    size = 0
    created = False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            created = True
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(block)
                digest.update(block)
                size += len(block)
                require(size <= expected["bytes"], "input failed size/SHA256 validation: " + str(source))
        require({"bytes": size, "sha256": digest.hexdigest()} == expected,
                "input failed size/SHA256 validation: " + str(source))
        destination.chmod(0o555)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def write_receipt(destination, value):
    created = False
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(value)
        destination.chmod(0o555)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


def validate_graph_snapshot(overlay, workspace, value):
    """Validate every retained graph copy, input link, and directory."""
    receipt = parse_receipt(value)
    copies = {name: identity(metadata, name) for name, metadata in receipt["graph_controls"].items()}
    copies[INPUT_DIRECTORY + "/provenance.json"] = bytes_identity(value)
    links = {INPUT_DIRECTORY + "/" + name: workspace / INPUT_DIRECTORY / name
             for name in receipt["files"]}
    expected = set(copies) | set(links)
    directories = {str(parent) for name in expected for parent in PurePosixPath(name).parents
                   if str(parent) != "."}
    message = "retained image graph snapshot is incomplete, linked, writable, or differs from its receipt; " \
              "move it aside for inspection before retrying: "
    require(overlay.is_dir() and not overlay.is_symlink() and overlay.resolve(strict=True) == overlay and
            overlay.stat().st_mode & 0o777 == 0o555, message + str(overlay))
    observed = set()
    for path in overlay.rglob("*"):
        name = path.relative_to(overlay).as_posix()
        observed.add(name)
        if name in directories:
            require(path.is_dir() and not path.is_symlink() and path.resolve(strict=True) == path and
                    path.stat().st_mode & 0o777 == 0o555, message + str(path))
        elif name in copies:
            require(path.is_file() and not path.is_symlink() and path.resolve(strict=True) == path and
                    path.stat().st_nlink == 1 and path.stat().st_mode & 0o777 == 0o555 and
                    file_identity(path) == copies[name], message + str(path))
        elif name in links:
            source = links[name]
            require(path.is_symlink() and path.readlink() == Path(os.path.relpath(source, path.parent)) and
                    path.is_file() and path.resolve(strict=True) == source, message + str(path))
        else:
            require(False, message + str(path))
    require(observed == expected | directories, message + str(overlay))
    return overlay


def verified_graph_snapshot(workspace, value):
    """Create or reuse a receipt-specific graph while the publisher lock is held."""
    workspace = workspace.absolute()
    require(":" not in str(workspace), "workspace path contains the Bazel package path separator ':'")
    receipt = parse_receipt(value)
    collection = workspace / GRAPH_DIRECTORY
    collection.mkdir(parents=True, exist_ok=True)
    require(collection.is_dir() and not collection.is_symlink() and collection.resolve(strict=True) == collection,
            "verified image graph directory must be unlinked: " + str(collection))
    directory = collection / bytes_identity(value)["sha256"]
    overlay = directory / "workspace"
    if not directory.exists() and not directory.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=collection) as temporary:
            pending = Path(temporary) / "workspace"
            pending.mkdir()
            for name, metadata in sorted(receipt["graph_controls"].items()):
                copy_verified_file(workspace / name, pending / name, metadata)
            write_receipt(pending / INPUT_DIRECTORY / "provenance.json", value)
            for name in sorted(receipt["files"]):
                path = pending / INPUT_DIRECTORY / name
                path.parent.mkdir(parents=True, exist_ok=True)
                source = workspace / INPUT_DIRECTORY / name
                path.symlink_to(os.path.relpath(source, (overlay / INPUT_DIRECTORY / name).parent))
            for path in pending.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o555)
            pending.chmod(0o555)
            validate_graph_snapshot(pending, workspace, value)
            # Claim this receipt exclusively, then reveal the complete package
            # root with one rename. An interrupted claim remains for inspection.
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                # Moving a directory to a new parent updates its ".." entry.
                # Seal that root again before the publisher can use it.
                pending.chmod(0o755)
                pending.rename(overlay)
                overlay.chmod(0o555)
                directory.chmod(0o555)
    require(directory.is_dir() and not directory.is_symlink() and directory.resolve(strict=True) == directory and
            directory.stat().st_mode & 0o777 == 0o555 and
            {path.name for path in directory.iterdir()} == {"workspace"},
            "retained image graph snapshot is incomplete, linked, writable, or differs from its receipt; "
            "move it aside for inspection before retrying: " + str(directory))
    return validate_graph_snapshot(overlay, workspace, value)


def verify_and_copy(receipt_path, inputs, outputs, receipt_output):
    value, receipt = load_receipt(receipt_path)
    expected = set(receipt["files"])
    require(set(inputs) == expected,
            "declared input names differ from receipt; missing=" + str(sorted(expected - set(inputs))) +
            ", extra=" + str(sorted(set(inputs) - expected)))
    require(set(outputs) == expected, "declared output names differ from receipt")
    destinations = [path.absolute() for path in outputs.values()] + [receipt_output.absolute()]
    require(len(set(destinations)) == len(destinations), "verified outputs must have distinct paths")
    require(not set(destinations) & {path.absolute() for path in [receipt_path, *inputs.values()]},
            "verified outputs must not replace prepared inputs")
    completed = []
    try:
        for name in sorted(expected):
            copy_verified_file(inputs[name], outputs[name], receipt["files"][name])
            completed.append(outputs[name])
        write_receipt(receipt_output, value)
        completed.append(receipt_output)
    except BaseException:
        for path in completed:
            path.unlink(missing_ok=True)
        raise
    return receipt


def parse_pairs(values, kind):
    result = {}
    for value in values:
        require("=" in value, kind + " must be NAME=PATH")
        name, path = value.split("=", 1)
        relative_name(name)
        require(name not in result, "duplicate " + kind + " name: " + name)
        require(path, kind + " path must not be empty")
        result[name] = Path(path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--output", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        verify_and_copy(args.receipt, parse_pairs(args.input, "input"),
                        parse_pairs(args.output, "output"), args.receipt_output)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
