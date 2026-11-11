#!/usr/bin/env python3
"""Read two original image blobs with the regctl binary pinned by rules_oci.

No Docker daemon, image execution, package generation or layer extraction occurs.
The upstream client owns registry transport; original digests bind both outputs.
"""
import argparse
import json
import os
from pathlib import Path
import platform

from tools.bazel.ci import vs_management_image_metadata as metadata

REPOSITORY = "sonicdev-microsoft.azurecr.io/docker-sonic-mgmt"
REGCTL_VERSION = "v0.8.0"
REGCTL_SHA256 = "2276edee5a1e5cb89bb4e448e5d023564acd6d84a5419cc92ff262d85dfb8e9a"
REGCTL_URL = "https://github.com/regclient/regclient/releases/download/v0.8.0/regctl-linux-amd64"
CONFIG = dict(metadata.CONFIG_DESCRIPTOR)
LAYER = {"digest": "sha256:2f3f982d43fc34373e04a5e9f22e765f1b98015b7aa4896d26d61e2072cc1fc8",
         "size": 16302731, "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip"}
DIFF_ID = "sha256:5943c6099464eaa84cd409fce40b78a90d0fe284585c442c4d767021cddf156f"
COPY_COMMAND = "COPY debs/sonic-device-data_*.deb debs/ # buildkit"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_blob(raw, descriptor):
    require(len(raw) == descriptor["size"] and "sha256:" + metadata.sha(raw) == descriptor["digest"],
            "Original blob size or digest differs")


def verify_config(raw):
    verify_blob(raw, CONFIG)
    value = json.loads(raw)
    require(value.get("architecture") == "amd64" and value.get("os") == "linux",
            "Original configuration is not Linux AMD64")
    rootfs, history = value.get("rootfs", {}), value.get("history", [])
    require(rootfs.get("type") == "layers" and len(rootfs.get("diff_ids", [])) == 28,
            "Original rootfs layout differs")
    require(type(history) is list and all(type(row) is dict for row in history), "Invalid original history")
    nonempty = [row for row in history if not row.get("empty_layer", False)]
    require(len(nonempty) == 28 and nonempty[13].get("created_by") == COPY_COMMAND and
            rootfs["diff_ids"][13] == DIFF_ID, "Original COPY layer mapping differs")


def anonymous_environment(directory):
    home, docker = directory / "anonymous-home", directory / "anonymous-docker"
    home.mkdir(mode=0o700)
    docker.mkdir(mode=0o700)
    metadata.write_json(docker / "config.json", {"auths": {"sonicdev-microsoft.azurecr.io": {}}})
    # No inherited GitHub, Bazel, Docker, registry or proxy credentials.
    return {"HOME": str(home), "DOCKER_CONFIG": str(docker), "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb"}


def collect(output, tool):
    require(tool.is_absolute() and tool.resolve() == tool, "Regctl path must be canonical")
    identity = metadata.file_identity(tool, require_root=False)
    require(identity["sha256"] == REGCTL_SHA256, "Regctl differs from the rules_oci pin")
    metadata.write_json(output / "tool-identity.json", {**identity, "pinned_release": REGCTL_VERSION,
                        "pin_owner": "bazel-contrib/rules_oci v2.2.6"})
    env = anonymous_environment(output)
    receipt = {"status": "FAILED_NOT_RECOVERED", "manifest_digest": metadata.MANIFEST_DIGEST,
               "registry_repository": REPOSITORY, "blobs": [], "package_presence_verified": False,
               "source_compatibility_verified": False, "package_generation": False}
    try:
        for name, descriptor in (("config", CONFIG), ("layer", LAYER)):
            receipt["stage"] = name
            directory = output / name
            directory.mkdir(mode=0o700)
            argv = [str(tool), "blob", "get", "--format", "raw-body", REPOSITORY, descriptor["digest"]]
            seconds = 120 if name == "layer" else 30
            metadata.write_json(directory / "plan.json", {"argv": argv, "stdout_cap": descriptor["size"],
                                "seconds": seconds, "anonymous": True})
            observation = metadata.observe_client(directory, argv, env,
                stdout_cap=descriptor["size"], seconds=seconds)
            require(observation["status"] == "CAPTURED", "Upstream original-blob read failed")
            raw = (directory / "stdout.data").read_bytes()
            verify_blob(raw, descriptor)
            if name == "config":
                verify_config(raw)
            receipt["blobs"].append({"name": name, **descriptor, "original_bytes_verified": True})
        require(metadata.file_identity(tool, require_root=False) == identity, "Regctl changed during observation")
        receipt.update(status="ORIGINAL_CONFIG_AND_COPY_LAYER_VERIFIED", stage="complete",
                       original_config_digest_verified=True, expected_uncompressed_diff_id=DIFF_ID,
                       uncompressed_diff_id_verified=False)
    finally:
        receipt["at"] = metadata.now()
        metadata.write_json(output / "summary.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--regctl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = {"GITHUB_REPOSITORY": "securely1g/sonic-buildimage", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_ACTOR": "securely1g", "GITHUB_TRIGGERING_ACTOR": "securely1g"}
    require(all(os.environ.get(key) == value for key, value in expected.items()), "Unexpected manual operator")
    require(platform.system() == "Linux" and platform.machine() == "x86_64", "Expected Linux AMD64")
    require(args.output.is_absolute() and args.output.resolve() == args.output,
            "Observation directory must be canonical")
    args.output.mkdir(mode=0o700)
    collect(args.output, args.regctl)


if __name__ == "__main__":
    main()
