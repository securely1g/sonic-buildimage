#!/usr/bin/env python3
"""Declare verified native predecessors for the VS installer assembly graph.

Make produces every service archive and the pre-container host snapshot. This
step verifies its provenance and freezes those bytes as ordinary Bazel inputs;
it never selects or rebuilds a component image.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host
import installer
from native.source_identity import source_identity

ROOT = Path(__file__).resolve().parents[3]
NATIVE = "target/bazel-native/"
NATIVE_FILES = {
    "inventory.json", "source.json", "captured-host-environment.json",
    "host-source.tar", "host-onie.squashfs", "host-config.json",
    "host-input-provenance.json", "installer-config.json", "images.json",
}
ENVIRONMENT = {"schema": 1, "platform": "linux/amd64", "docker_version": "28.5.2",
               "storage_driver": "overlay2", "distribution": "trixie"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_info(path, *, allow_empty=False):
    require(path.is_file() and not path.is_symlink() and (allow_empty or path.stat().st_size > 0),
            "expected " + ("" if allow_empty else "nonempty ") + "regular input: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def native_file(workspace, name):
    require(isinstance(name, str), "native input path must be text")
    relative = PurePosixPath(name)
    require(str(relative) == name and not relative.is_absolute() and ".." not in relative.parts,
            "invalid native input path: " + name)
    require(name in {NATIVE + item for item in NATIVE_FILES | {"provenance.json"}} or
            (relative.parent == PurePosixPath("target") and
             re.fullmatch(r"[A-Za-z0-9_.-]+\.gz", relative.name)),
            "unexpected native input path: " + name)
    path = workspace / name
    require(path.resolve(strict=True) == path, "native input is linked: " + name)
    return path


def verify_native(directory):
    directory = directory.absolute()
    require(directory.resolve(strict=True) == directory and directory.is_dir() and
            directory.name == "bazel-native" and directory.parent.name == "target",
            "native directory must be the producer's target/bazel-native directory")
    workspace = directory.parent.parent
    provenance_path = native_file(workspace, NATIVE + "provenance.json")
    provenance = json.loads(provenance_path.read_text())
    require(provenance.get("schema") == 1 and
            provenance.get("source_boundary") == "before-container-loading",
            "unsupported native producer provenance")
    current = source_identity(workspace)
    require(all(current.get(key) == provenance.get(key) for key in
                ("source_commit", "source_branch", "source_submodules")),
            "native producer source identity differs from the current checkout")
    require(current["native_transformations"] == provenance.get("native_transformations"),
            "native source transformations changed after snapshot production")
    files = provenance.get("files")
    require(isinstance(files, dict) and {NATIVE + name for name in NATIVE_FILES} <= files.keys(),
            "native producer omitted required image inputs")
    for name, expected in files.items():
        require(isinstance(expected, dict) and type(expected.get("bytes")) is int and
                expected["bytes"] > 0 and isinstance(expected.get("sha256"), str) and
                re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]),
                "invalid native input metadata: " + name)
        require(file_info(native_file(workspace, name)) == expected,
                "native input failed size/SHA256 validation: " + name)
    config = host.load_config(directory / "host-config.json")
    identity = config["identity"]
    require(all(identity[key] == provenance.get(key) for key in
                ("source_commit", "source_branch", "source_date_epoch", "image_version")),
            "native host identity differs from producer provenance")
    inventory = json.loads((directory / "inventory.json").read_text())
    require((inventory["arch"], inventory["platform"], inventory["distro"]) == ("amd64", "vs", "trixie")
            and not inventory["remote_packages"], "unsupported native image inventory")
    require(inventory["config_flags"] == provenance.get("config_flags"),
            "native build configuration differs from producer provenance")
    builtins, local = host.image_names(config["environment"])
    require((builtins, local) == (set(inventory["installed_dockers"]), set(inventory["local_packages"])),
            "host environment and image inventory disagree")
    images = json.loads((directory / "images.json").read_text())
    require(isinstance(images, dict) and set(images) == builtins | local,
            "native archives must exactly match the service inventory")
    for name, relative in images.items():
        require(relative == "target/" + name and relative in files,
                "service archive is not declared in native provenance: " + name)
        native_file(workspace, relative)
    require(set(files) == {NATIVE + name for name in NATIVE_FILES} | set(images.values()),
            "native provenance contains inputs outside the installer boundary")
    installer_config = installer.load_config(directory / "installer-config.json")
    require(installer_config["image_version"] == identity["image_version"] and
            installer_config["epoch"] == int(identity["source_date_epoch"]),
            "host and installer identities disagree")
    with (directory / "host-onie.squashfs").open("rb") as stream:
        require(stream.read(4) == b"hsqs", "native predecessor is not a SquashFS snapshot")
    host.validate_archive(directory / "host-source.tar")
    return workspace, provenance, config, images, local


def prepare(args):
    output = args.output.absolute()
    require(output == ROOT / "target/bazel-image-inputs", "the VS target requires --output " + str(ROOT / "target/bazel-image-inputs"))
    require(output.resolve() == output and not output.exists(), "image input output must be a fresh unlinked directory")
    installer_source = args.installer_source.resolve(strict=True)
    workspace, provenance, config, image_sources, local = verify_native(args.native_directory)
    assembler_source = source_identity(installer_source)
    native = workspace / NATIVE
    worker = json.loads(args.execution_environment.read_text())
    require(all(worker.get(key) == value for key, value in ENVIRONMENT.items()) and
            isinstance(worker.get("worker_image"), str) and
            re.fullmatch(r"sha256:[0-9a-f]{64}", worker["worker_image"]),
            "unsupported pinned assembly worker environment")
    package = ROOT / "tools/bazel/image/vs"
    template = (package / "BUILD.bazel.in").read_bytes()
    build_file = package / "BUILD.bazel"
    require(not build_file.exists() or (not build_file.is_symlink() and build_file.read_bytes() == template),
            "refusing to replace a customized VS BUILD.bazel")
    output.mkdir(parents=True)
    receipt = {"schema": 1, "scope": "Verified native snapshot and all service archives; installer assembly only.",
               "native_provenance_sha256": file_info(native / "provenance.json")["sha256"],
               "native_source_identity": {key: provenance[key] for key in
                                          ("source_commit", "source_branch", "source_submodules",
                                           "native_transformations", "source_date_epoch", "image_version")},
               "assembler_source_identity": assembler_source, "files": {}}
    receipt["native_config_flags"] = provenance["config_flags"]

    def stage(source, relative, expected=None):
        source = Path(source)
        before = file_info(source)
        require(expected is None or before == expected, "native input changed before staging: " + str(source))
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        require(file_info(destination) == before, "input changed while staging: " + str(source))
        receipt["files"][relative] = {"source": str(source), **before}
        return "//target/bazel-image-inputs:" + relative

    def stage_native(name, relative):
        return stage(workspace / name, relative, provenance["files"][name])

    images = {name: stage_native(relative, "images/" + name) for name, relative in sorted(image_sources.items())}
    installer_dir = output / "installer"
    require(file_info(native / "installer-config.json") == provenance["files"][NATIVE + "installer-config.json"],
            "installer configuration changed before staging")
    installer.prepare_inputs(installer_source, native / "installer-config.json", installer_dir)
    require(file_info(native / "installer-config.json") == provenance["files"][NATIVE + "installer-config.json"],
            "installer configuration changed while staging")
    prepared_config = installer.load_config(installer_dir / "config.json")
    require(prepared_config["image_version"] == config["identity"]["image_version"] and
            prepared_config["epoch"] == int(config["identity"]["source_date_epoch"]),
            "installer configuration changed while staging")
    installer_entries = json.loads((installer_dir / "files-manifest.json").read_text())
    installer_provenance = json.loads((installer_dir / "provenance.json").read_text())
    for path in sorted(installer_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        generated = relative in {"installer/config.json", "installer/files-manifest.json",
                                 "installer/provenance.json", "installer/files/platforms_asic"}
        receipt["files"][relative] = {
            "source": "installer.prepare_inputs" if generated else str(installer_source),
            "source_kind": "generated" if generated else "assembler-copy",
            "source_provenance": "installer/provenance.json",
            **file_info(path, allow_empty=True),
        }
    receipt["files"]["installer/config.json"]["source"] = str(native / "installer-config.json")
    attributes = {
        "images": images, "local_images": sorted(local),
        "source": stage_native(NATIVE + "host-source.tar", "host-source.tar"),
        "snapshot": stage_native(NATIVE + "host-onie.squashfs", "host-onie.squashfs"),
        "host_config": stage_native(NATIVE + "host-config.json", "host-config.json"),
        "execution_environment": stage(args.execution_environment, "execution-environment.json"),
        "installer_config": "//target/bazel-image-inputs:installer/config.json",
        "installer_files": {"//target/bazel-image-inputs:installer/" + item["source"]: item["path"] for item in installer_entries},
        "installer_modes": {item["path"]: str(item["mode"]) for item in installer_entries},
        "image_version": config["identity"]["image_version"],
        "epoch": int(config["identity"]["source_date_epoch"]),
    }
    require(json.loads((output / "execution-environment.json").read_text()) == worker,
            "assembly worker changed while staging")
    require(source_identity(installer_source) == assembler_source,
            "assembler source identity changed while staging")
    for name, expected in installer_provenance["source_sha256"].items():
        require(host.digest(installer_source / name) == expected,
                "assembler input changed while staging: " + name)
    (output / "BUILD.bazel").write_text('package(default_visibility = ["//visibility:public"])\nexports_files(glob(["**"], exclude = ["BUILD.bazel"]))\n')
    (output / "inputs.bzl").write_text("# Generated from verified native predecessors.\nIMAGE_INPUTS = " + json.dumps(attributes, indent=4, sort_keys=True) + "\n")
    (output / "provenance.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    # Install the optional package only after all declared inputs exist. Fresh
    # checkouts can still load //... before native preparation.
    if not build_file.exists():
        with build_file.open("xb") as stream:
            stream.write(template)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("native-directory", "installer-source", "execution-environment", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    prepare(parser.parse_args(argv))


if __name__ == "__main__":
    main()
