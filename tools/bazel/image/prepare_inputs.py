#!/usr/bin/env python3
"""Declare retained native predecessors for the cacheable VS assembly graph.

The pre-container host snapshot, rendered native source bundle, and other
service archives remain phase-one inputs. No completed image is accepted as
the host predecessor. SWSS always comes from its source-built Bazel target.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer
import host


def file_info(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "size": path.stat().st_size}


def prepare(args):
    output = args.output.resolve()
    expected = Path(__file__).resolve().parents[3] / "target/bazel-image-inputs"
    if output != expected:
        raise ValueError("the VS target requires --output " + str(expected))
    package = Path(__file__).resolve().parent / "vs"
    template = (package / "BUILD.bazel.in").read_bytes()
    build_file = package / "BUILD.bazel"
    if build_file.exists() and build_file.read_bytes() != template:
        raise ValueError("refusing to replace a customized VS BUILD.bazel")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "inputs.bzl").exists():
        raise ValueError("inputs already prepared; preserve the existing bundle before preparing a replacement")
    config = host.load_config(args.host_config)
    inventory = json.loads(args.inventory.read_text())
    if (inventory["arch"], inventory["platform"], inventory["distro"]) != ("amd64", "vs", "trixie"):
        raise ValueError("only amd64 Trixie VS inputs are supported")
    if inventory["remote_packages"]:
        raise ValueError("remote package inputs are not supported")
    builtins, local = host.image_names(config["environment"])
    if (builtins, local) != (set(inventory["installed_dockers"]), set(inventory["local_packages"])):
        raise ValueError("host environment and image inventory disagree")
    image_sources = json.loads(args.images.read_text())
    if "docker-orchagent.gz" not in builtins:
        raise ValueError("VS inventory is missing SWSS")
    receipt = {"schema": 1, "scope": "Declared native predecessors; SWSS is built from source by Bazel", "files": {}}

    def stage(source, relative):
        source = Path(source).resolve(strict=True)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source != destination.resolve():
            if destination.exists():
                raise ValueError("refusing to replace prepared input: " + str(destination))
            shutil.copyfile(source, destination)
        receipt["files"][relative] = {"source": str(source), **file_info(destination)}
        return "//target/bazel-image-inputs:" + relative

    images = {}
    for name in sorted(builtins | local):
        if name == "docker-orchagent.gz":
            images[name] = "//dockers/docker-orchagent:docker-orchagent.gz"
        else:
            images[name] = stage(image_sources[name], "images/" + name)
    installer_dir = output / "installer"
    installer.prepare_inputs(args.installer_source, args.installer_config, installer_dir)
    installer_entries = json.loads((installer_dir / "files-manifest.json").read_text())
    attributes = {
        "images": images, "local_images": sorted(local),
        "source": stage(args.host_source, "host-source.tar"),
        "snapshot": stage(args.host_snapshot, "host-onie.squashfs"),
        "host_config": stage(args.host_config, "host-config.json"),
        "execution_environment": stage(args.execution_environment, "execution-environment.json"),
        "installer_config": "//target/bazel-image-inputs:installer/config.json",
        "installer_files": {"//target/bazel-image-inputs:installer/" + item["source"]: item["path"] for item in installer_entries},
        "installer_modes": {item["path"]: str(item["mode"]) for item in installer_entries},
        "image_version": config["identity"]["image_version"],
        "epoch": int(config["identity"]["source_date_epoch"]),
    }
    if json.loads((installer_dir / "config.json").read_text())["image_version"] != attributes["image_version"]:
        raise ValueError("host and installer image versions disagree")
    (output / "BUILD.bazel").write_text('package(default_visibility = ["//visibility:public"])\nexports_files(glob(["**"], exclude = ["BUILD.bazel"]))\n')
    (output / "inputs.bzl").write_text("# Generated from explicit retained native predecessors.\nIMAGE_INPUTS = " + json.dumps(attributes, indent=4, sort_keys=True) + "\n")
    (output / "provenance.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    # Create the optional package only after its declared inputs exist. A fresh
    # checkout can still load //... without a prepared native image bundle.
    if not build_file.exists():
        with build_file.open("xb") as stream:
            stream.write(template)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "inventory", "images", "host-source", "host-config", "host-snapshot", "installer-source", "installer-config", "execution-environment"):
        parser.add_argument("--" + name, type=Path, required=True)
    prepare(parser.parse_args())
