#!/usr/bin/env python3
"""Verify same-job native source outputs and declare the Bazel image inputs.

There is no download or retained-input mode. The controller must first run the
native producer from the current checkout and supply its invocation receipt.
"""

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


NATIVE = "target/bazel-native/"
CONFIG_ENGINE = "target/docker-config-engine-trixie.gz"
SCAPY = "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl"
REQUIRED = {NATIVE + name for name in (
    "inventory.json", "captured-host-environment.json", "host-onie.squashfs",
    "host-source.tar", "host-config.json", "installer-config.json", "images.json",
)} | {CONFIG_ENGINE, SCAPY}
ENVIRONMENT = {"schema": 1, "platform": "linux/amd64", "docker_version": "28.5.2",
               "storage_driver": "overlay2", "distribution": "trixie"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_input(workspace, name):
    """Accept producer outputs beneath target, never aliases outside the clone."""
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and ".." not in relative.parts and str(relative) == name,
            "noncanonical native input path: " + name)
    require(name.startswith(NATIVE) or name == SCAPY or
            (relative.parent == PurePosixPath("target") and relative.name.startswith("docker-")
             and relative.name.endswith(".gz")), "unexpected native input path: " + name)
    path = workspace / name
    require(path.resolve(strict=True) == path and path.is_file(),
            "native input is a symlink or escapes the workspace: " + name)
    return path


def read_image_inputs(path):
    """Read the generated data assignment without executing Starlark/Python."""
    tree = ast.parse(path.read_text(), filename=str(path))
    require(len(tree.body) == 1 and isinstance(tree.body[0], ast.Assign),
            "inputs.bzl must contain only the IMAGE_INPUTS data assignment")
    statement = tree.body[0]
    require(len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "IMAGE_INPUTS", "unexpected inputs.bzl assignment")
    value = ast.literal_eval(statement.value)
    require(isinstance(value, dict), "IMAGE_INPUTS must be a literal dictionary")
    return value


def verify_native(native_receipt, workspace, source, invocation):
    run = json.loads(native_receipt.read_text())
    require(run.get("schema") == 1 and run.get("status") == "passed",
            "native source build did not pass")
    require(run.get("invocation") == invocation and run.get("source_commit") == source["source_commit"],
            "native receipt does not belong to this invocation and source commit")
    require(run.get("native_provenance") == NATIVE + "provenance.json",
            "native build did not declare its producer provenance")
    provenance_path = local_input(workspace, run["native_provenance"])
    provenance = json.loads(provenance_path.read_text())
    require(provenance.get("schema") == 1 and provenance.get("source_commit") == source["source_commit"],
            "native producer source commit differs from this checkout")
    expected_submodules = {name: item["commit"] for name, item in source["components"].items()}
    require(provenance.get("source_submodules") == expected_submodules,
            "native producer submodule revisions differ from this checkout")
    require(isinstance(provenance.get("native_transformations"), dict),
            "native producer omitted its source transformation record")
    files = provenance["files"]
    require(REQUIRED <= files.keys(), "native producer omitted required image inputs")
    for name, info in files.items():
        path = local_input(workspace, name)
        require(type(info.get("bytes")) is int and info["bytes"] > 0 and
                re.fullmatch(r"[0-9a-f]{64}", info.get("sha256", "")),
                "invalid native input metadata: " + name)
        require(path.stat().st_size == info["bytes"] and sha256(path) == info["sha256"],
                "native input failed size/SHA256 validation: " + name)
    config = json.loads((workspace / NATIVE / "host-config.json").read_text())
    identity = config["identity"]
    for key in ("source_commit", "source_branch", "source_date_epoch", "image_version"):
        require(identity.get(key) == provenance.get(key), "native host identity differs: " + key)
    images = json.loads((workspace / NATIVE / "images.json").read_text())
    require("docker-orchagent.gz" not in images, "SWSS must be built by the Bazel source target")
    for name, relative in images.items():
        require(relative in files and PurePosixPath(relative).name == name,
                "service archive is not declared in native provenance: " + name)
        local_input(workspace, relative)
    return provenance, images, sha256(provenance_path)


def stage_fixed_input(native_workspace, bazel_workspace, name, expected):
    """Copy one declared root-label input without importing native source dirt."""
    require(name in (CONFIG_ENGINE, SCAPY), "unexpected fixed Bazel input: " + name)
    source = local_input(native_workspace, name)
    destination = bazel_workspace / name
    parent = bazel_workspace
    for part in Path(name).parts[:-1]:
        parent = parent / part
        require(not parent.is_symlink(), "staged input parent is a symlink: " + str(parent))
        parent.mkdir(exist_ok=True)
        require(parent.resolve(strict=True) == parent and parent.is_dir(),
                "staged input parent escapes the Bazel workspace")
    # Exclusive creation rejects ordinary files and symlinks, including broken
    # links. Do not hardlink native inputs: later changes must not affect them.
    with source.open("rb") as original, destination.open("xb") as output:
        shutil.copyfileobj(original, output)
    staged = local_input(bazel_workspace, name)
    actual = {"bytes": staged.stat().st_size, "sha256": sha256(staged)}
    require(actual == expected, "staged input failed size/SHA256 validation: " + name)
    return actual


def prepare(native_receipt, native_workspace, bazel_workspace, scratch, receipt_path, worker_spec, source, invocation):
    native_workspace = native_workspace.resolve(strict=True)
    bazel_workspace = bazel_workspace.resolve(strict=True)
    scratch.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": 1, "status": "running", "invocation": invocation,
               "source_commit": source["source_commit"], "native_receipt_sha256": sha256(native_receipt),
               "native_workspace": str(native_workspace), "bazel_workspace": str(bazel_workspace),
               "staged_files": {}}
    try:
        require(not native_workspace.is_relative_to(bazel_workspace) and
                not bazel_workspace.is_relative_to(native_workspace),
                "native and Bazel workspaces must be separate, non-overlapping checkouts")
        output = bazel_workspace / "target/bazel-image-inputs"
        require(not output.exists() and not output.is_symlink(), "image inputs already exist; a fresh native build is required")
        provenance, images, digest = verify_native(native_receipt, native_workspace, source, invocation)
        receipt["native_provenance_sha256"] = digest
        receipt["native_files"] = provenance["files"]
        receipt["native_transformations"] = provenance["native_transformations"]
        worker = json.loads(worker_spec.read_text())
        native_run = json.loads(native_receipt.read_text())
        require(native_run.get("worker_image") == worker.get("worker_image"),
                "native source build used a different execution worker")
        require(all(worker.get(key) == value for key, value in ENVIRONMENT.items()) and
                re.fullmatch(r"sha256:[0-9a-f]{64}", worker.get("worker_image", "")),
                "unsupported locally built worker environment")
        for name in (CONFIG_ENGINE, SCAPY):
            receipt["staged_files"][name] = stage_fixed_input(
                native_workspace, bazel_workspace, name, provenance["files"][name])
        resolved_images = scratch / "images.json"
        with resolved_images.open("x") as stream:
            stream.write(json.dumps({name: str(native_workspace / path) for name, path in images.items()}, indent=2) + "\n")
        native = native_workspace / NATIVE
        command = [sys.executable, str(bazel_workspace / "tools/bazel/image/prepare_inputs.py"),
                   "--output", str(output), "--inventory", str(native / "inventory.json"),
                   "--images", str(resolved_images), "--host-source", str(native / "host-source.tar"),
                   "--host-config", str(native / "host-config.json"),
                   "--host-snapshot", str(native / "host-onie.squashfs"),
                   "--installer-source", str(bazel_workspace), "--installer-config", str(native / "installer-config.json"),
                   "--execution-environment", str(worker_spec)]
        receipt["prepare_command"] = command
        subprocess.run(command, cwd=bazel_workspace, check=True)
        attributes = read_image_inputs(output / "inputs.bzl")
        require(attributes["images"].get("docker-orchagent.gz") ==
                "//dockers/docker-orchagent:docker-orchagent.gz", "prepared graph must compile SWSS from source")
        require(json.loads((output / "execution-environment.json").read_text()) == worker,
                "prepared worker environment changed")
        receipt.update(status="passed", worker_image=worker["worker_image"],
                       prepared_provenance_sha256=sha256(output / "provenance.json"),
                       image_inputs_sha256=sha256(output / "inputs.bzl"))
        return receipt
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
