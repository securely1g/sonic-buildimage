#!/usr/bin/env python3
"""Fetch and verify the explicit native predecessors for Bazel VS image CI.

Release archives contain only the declared target/ inputs. Each small archive
is verified, extracted and removed before downloading the next, so staging
does not require another complete copy of the multi-gigabyte input bundle.
"""

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_verified(source, output, metadata, description):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(min(1024 * 1024, metadata["bytes"] - size + 1)):
        size += len(chunk)
        require(size <= metadata["bytes"], "input exceeds declared size: " + description)
        digest.update(chunk)
        output.write(chunk)
    require(size == metadata["bytes"] and digest.hexdigest() == metadata["sha256"],
            "input failed size/SHA256 validation: " + description)


def input_path(name):
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == name,
            "noncanonical input path: " + name)
    require(name.startswith("target/bazel-image-inputs/") or name in {
        "target/docker-config-engine-trixie.gz",
        "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl",
    }, "input outside the declared native predecessors: " + name)
    return path


def load_manifest(path):
    manifest = json.loads(Path(path).read_text())
    require(manifest.get("schema") == 1, "unsupported input manifest schema")
    worker = manifest["worker"]
    require(re.fullmatch(r"sonic-bazel-vs-worker:[A-Za-z0-9_.-]+", worker["reference"]),
            "invalid worker archive reference")
    require(worker["image_ids"] and all(re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for value in worker["image_ids"]), "worker must declare immutable image IDs")
    require(worker["environment"] == {"schema": 1, "platform": "linux/amd64",
            "docker_version": "28.5.2", "storage_driver": "overlay2", "distribution": "trixie"},
            "unsupported image execution environment")
    base = urllib.parse.urlparse(manifest.get("release_url", ""))
    require(base.scheme == "https" and base.hostname == "github.com" and
            re.fullmatch(r"/securely1g/sonic-buildimage/releases/download/[A-Za-z0-9_.-]+", base.path)
            and not base.query and not base.fragment and not base.username,
            "expected the published SONiC native-input release URL")
    seen, names, workers = set(), set(), 0
    for asset in manifest["assets"]:
        name = asset["name"]
        require(re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in names,
                "invalid or duplicate release asset")
        names.add(name)
        require(0 < asset["bytes"] < 2 * 1024 ** 3 and
                re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]), "invalid asset size or digest")
        require(asset["kind"] in {"worker", "inputs"}, "invalid asset kind")
        if asset["kind"] == "worker":
            workers += 1
            continue
        require(asset.get("files"), "empty predecessor archive")
        for name, metadata in asset["files"].items():
            input_path(name)
            require(name != "target/bazel-image-inputs/execution-environment.json",
                    "worker identity is generated from the verified worker archive")
            require(name not in seen, "duplicate input across release assets: " + name)
            seen.add(name)
            require(metadata["bytes"] >= 0 and re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])
                    and metadata["mode"] in {0o644, 0o755}, "invalid file metadata: " + name)
    require(workers == 1, "manifest must contain exactly one worker archive")
    require({"target/bazel-image-inputs/inputs.bzl",
             "target/bazel-image-inputs/BUILD.bazel",
             "target/bazel-image-inputs/host-onie.squashfs",
             "target/bazel-image-inputs/host-source.tar",
             "target/bazel-image-inputs/host-config.json",
             "target/docker-config-engine-trixie.gz",
             "target/python-wheels/trixie/scapy-2.6.1.dev0-py3-none-any.whl"} <= seen,
            "missing required native predecessors")
    return manifest


def fetch_asset(asset, base_url, destination, local_assets=None):
    with destination.open("xb") as output:
        if local_assets is not None:
            source = (local_assets / asset["name"]).open("rb")
        else:
            source = urllib.request.urlopen(base_url + "/" + asset["name"], timeout=120)
        with source:
            copy_verified(source, output, asset, asset["name"])


def extract_inputs(archive_path, expected, workspace):
    seen = set()
    with tarfile.open(archive_path, "r|*") as archive:
        for member in archive:
            name = member.name
            input_path(name)
            require(member.isfile() and name in expected and name not in seen,
                    "unexpected, duplicate, or non-regular input: " + name)
            info = expected[name]
            require(member.size == info["bytes"] and member.mode == info["mode"],
                    "input size/mode does not match manifest: " + name)
            output = workspace / name
            require(output.resolve().is_relative_to(workspace.resolve()), "input escapes workspace")
            output.parent.mkdir(parents=True, exist_ok=True)
            # Refuse overwrites, including symlinks: CI must start with a fresh
            # checkout, not silently reuse mutable native inputs from a prior job.
            with output.open("xb") as stream, archive.extractfile(member) as source:
                copy_verified(source, stream, info, name)
            output.chmod(info["mode"])
            seen.add(name)
    require(seen == set(expected), "release archive omits declared inputs")


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


def refresh_installer_inputs(workspace, source=None):
    """Replace only installer sources after all released inputs were verified.

    Native host/service predecessors and the image identity remain pinned. The
    regenerated installer files and data mappings have their own provenance;
    their bytes must not be represented as the released asset's contents.
    """
    source = (source or workspace).resolve(strict=True)
    bundle = workspace / "target/bazel-image-inputs"
    staged = bundle / "installer"
    require(staged.is_dir() and not staged.is_symlink(), "missing verified installer source bundle")
    inputs_file = bundle / "inputs.bzl"
    attributes = read_image_inputs(inputs_file)
    require(attributes.get("installer_config") == "//target/bazel-image-inputs:installer/config.json",
            "unexpected installer configuration input")
    source_commit = subprocess.check_output([
        "git", "-c", "safe.directory=" + str(source), "-C", str(source), "rev-parse", "HEAD"],
        text=True).strip()
    require(re.fullmatch(r"[0-9a-f]{40}", source_commit), "invalid installer source commit")

    def files_in(directory, prefix):
        result = {}
        for path in sorted(directory.rglob("*")):
            require(not path.is_symlink(), "installer bundle must not contain symlinks")
            if path.is_file():
                result[prefix + str(path.relative_to(directory))] = {
                    "bytes": path.stat().st_size, "sha256": sha256(path), "mode": path.stat().st_mode & 0o777,
                }
        return result

    released = files_in(staged, "target/bazel-image-inputs/installer/")
    released["target/bazel-image-inputs/inputs.bzl"] = {
        "bytes": inputs_file.stat().st_size, "sha256": sha256(inputs_file),
        "mode": inputs_file.stat().st_mode & 0o777,
    }
    module_path = Path(__file__).resolve().parents[1] / "image/installer.py"
    spec = importlib.util.spec_from_file_location("sonic_ci_installer_inputs", module_path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    with tempfile.TemporaryDirectory(prefix="sonic-installer-source-", dir=bundle.parent) as temporary:
        temporary = Path(temporary)
        regenerated = temporary / "installer"
        installer.prepare_inputs(source, staged / "config.json", regenerated)
        config = json.loads((regenerated / "config.json").read_text())
        require(config["image_version"] == attributes.get("image_version"),
                "host and regenerated installer image versions disagree")
        entries = json.loads((regenerated / "files-manifest.json").read_text())
        attributes["installer_files"] = {
            "//target/bazel-image-inputs:installer/" + item["source"]: item["path"] for item in entries
        }
        attributes["installer_modes"] = {item["path"]: str(item["mode"]) for item in entries}
        updated_inputs = temporary / "inputs.bzl"
        updated_inputs.write_text(
            "# Native predecessors are pinned; installer sources come from the current checkout.\n"
            "IMAGE_INPUTS = " + json.dumps(attributes, indent=4, sort_keys=True) + "\n")
        derived = files_in(regenerated, "target/bazel-image-inputs/installer/")
        derived["target/bazel-image-inputs/inputs.bzl"] = {
            "bytes": updated_inputs.stat().st_size, "sha256": sha256(updated_inputs),
            "mode": updated_inputs.stat().st_mode & 0o777,
        }
        provenance = json.loads((regenerated / "provenance.json").read_text())
        # Construct the complete replacement before touching the verified
        # bundle. No host snapshot or service archive is moved or rewritten.
        os.replace(staged, temporary / "released-installer")
        os.replace(regenerated, staged)
        os.replace(updated_inputs, inputs_file)
    return {
        "source_commit": source_commit, "source_root": str(source),
        "generator_sha256": sha256(module_path),
        "released_files": released, "derived_files": derived, "source_provenance": provenance,
        "scope": "Installer source files regenerated from the checkout; native host/service inputs and image identity retained.",
    }


def prepare(manifest_path, workspace, scratch, receipt_path, local_assets=None):
    manifest = load_manifest(manifest_path)
    workspace = workspace.resolve(strict=True)
    scratch.mkdir(parents=True, exist_ok=True)
    receipt = {"schema": 1, "status": "running", "manifest_sha256": sha256(manifest_path),
               "release_url": manifest["release_url"], "assets": []}
    worker_image = None
    try:
        for asset in manifest["assets"]:
            print("Verifying and staging " + asset["name"], flush=True)
            with tempfile.TemporaryDirectory(prefix="sonic-vs-input-", dir=scratch) as temporary:
                path = Path(temporary) / asset["name"]
                fetch_asset(asset, manifest["release_url"], path, local_assets)
                if asset["kind"] == "worker":
                    subprocess.run(["docker", "load", "--input", str(path)], check=True)
                    result = json.loads(subprocess.check_output([
                        "docker", "image", "inspect", manifest["worker"]["reference"]], text=True))
                    require(len(result) == 1 and result[0]["Id"] in manifest["worker"]["image_ids"]
                            and result[0]["Os"] == "linux" and result[0]["Architecture"] == "amd64",
                            "loaded worker image identity/platform mismatch")
                    worker_image = result[0]["Id"]
                else:
                    extract_inputs(path, asset["files"], workspace)
                receipt["assets"].append({key: asset[key] for key in ("name", "bytes", "sha256", "kind")})
        require(worker_image is not None, "worker archive was not loaded")
        receipt["installer_refresh"] = refresh_installer_inputs(workspace)
        # Docker's classic and containerd image stores identify the same saved
        # image by its config and manifest/index digest respectively. Both IDs
        # come from the pinned archive; record the ID accepted by this daemon.
        spec = dict(manifest["worker"]["environment"], worker_image=worker_image)
        spec_path = workspace / "target/bazel-image-inputs/execution-environment.json"
        with spec_path.open("x") as output:
            output.write(json.dumps(spec, indent=2, sort_keys=True) + "\n")
        template = workspace / "tools/bazel/image/vs/BUILD.bazel.in"
        (template.parent / "BUILD.bazel").write_bytes(template.read_bytes())
        receipt.update(status="passed", worker_image=worker_image, execution_environment_sha256=sha256(spec_path))
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--local-assets", type=Path, help="Verify an unpublished bundle using local release assets")
    args = parser.parse_args()
    prepare(args.manifest, args.workspace, args.scratch, args.receipt, args.local_assets)


if __name__ == "__main__":
    main()
