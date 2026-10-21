"""Validate real Syncd images and their imported P4 package identities."""

import argparse
import json
import os
from pathlib import Path

from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci import image_validation, native_packages
from tools.bazel.oci.package_contract import read_policy
from tools.bazel.oci.source_base_validation import validate_source_base

IMAGE_INPUTS = (
    "runtime_path", "debug_path", "runtime_handoff", "debug_handoff",
    "runtime_manifest_path", "debug_manifest_path", "source_contract_path",
    "apt_lock_path", "dpkg_filter_path", "base_path",
    "configured_runtime_tar", "configured_debug_tar", "runtime_rootfs_tar",
    "runtime_installation", "debug_installation", "runtime_package_metadata",
    "runtime_source_layer",
    "runtime_apt_selection", "debug_apt_selection",
    "runtime_archive", "debug_archive", "source_runtime_tar", "source_debug_tar",
    "source_receipt",
)
NATIVE_INPUTS = (
    "source_contract_path", "source_runtime_tar", "source_debug_tar", "source_receipt",
    "base_path",
)
SOURCE_BASE_INPUTS = ("base_symbols", "base_metadata", "config_metadata")
P4_RUNTIME_PACKAGES = {"p4lang-pi", "p4lang-bmv2", "p4lang-p4c", "libsai"}


def check_p4_inputs(lock_path, debs, manifests):
    lock = json.loads(lock_path.read_bytes())
    require(lock["architecture"] == "amd64" and lock["distribution"] == "trixie",
            "P4 lock is not the supported AMD64 Trixie tuple")
    packages = [item for item in lock["packages"] if item["name"] in P4_RUNTIME_PACKAGES]
    require(len(packages) == 4 and {item["name"] for item in packages} == P4_RUNTIME_PACKAGES,
            "P4 lock does not contain exactly the four runtime packages")
    files = {path.name: path for path in debs}
    require(len(debs) == len(files) == 4 and set(files) == {item["filename"] for item in packages},
            "P4 runtime input files differ from the lock")
    runtime, debug = [json.loads(path.read_bytes()) for path in manifests]
    require(runtime["variant"] == "runtime" and debug["variant"] == "debug",
            "P4 receipts do not identify both image variants")
    require(debug["runtime_manifest_sha256"] == sha(manifests[0]),
            "P4 debug receipt is not bound to the runtime receipt")
    require(runtime["payload"] == debug["payload"], "P4 debug receipt changes the runtime payload")
    for manifest_path in manifests:
        payload_path = manifest_path.parent / runtime["payload"]["path"]
        require(payload_path.is_file() and sha(payload_path) == runtime["payload"]["sha256"],
                "P4 receipt cannot resolve its verified sibling payload: " + str(manifest_path))
    for package in packages:
        path = files[package["filename"]]
        require(path.stat().st_size == package["size"] and sha(path) == package["sha256"],
                "P4 package bytes differ from the lock: " + package["name"])
        for manifest_path in manifests:
            records = json.loads(manifest_path.read_bytes())["packages"]
            matches = [record for record in records if record["package"] == package["name"]]
            require(len(matches) == 1, "P4 package is missing or duplicated in " + str(manifest_path))
            record = matches[0]
            require((record["version"], record["architecture"], record["source_deb"],
                     record["source_size"], record["source_sha256"]) ==
                    (package["version"], lock["architecture"], package["filename"], package["size"], package["sha256"]),
                    "P4 image receipt differs from the lock: " + package["name"])
            control = record["control_fields"]
            require((control["Package"], control["Version"], control["Architecture"]) ==
                    (package["name"], package["version"], lock["architecture"]),
                    "P4 package control identity differs from the lock: " + package["name"])
    return {"lock_sha256": sha(lock_path), "packages": packages}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (*IMAGE_INPUTS, *SOURCE_BASE_INPUTS, "policy", "p4_lock", "make_runtime_archive", "make_debug_archive", "readelf", "objcopy", "debug_package_metadata"):
        parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    parser.add_argument("--p4-deb", action="append", required=True, type=Path)
    inputs = vars(parser.parse_args())
    policy = read_policy(inputs["policy"])
    p4 = check_p4_inputs(inputs["p4_lock"], inputs["p4_deb"],
                         [inputs["runtime_handoff"], inputs["debug_handoff"]])
    for variant in ("runtime", "debug"):
        require(sha(inputs["make_" + variant + "_archive"]) == sha(inputs[variant + "_archive"]),
                variant + " Make archive differs from the Bazel output")
    images = image_validation.validate_images(
        source_root=Path.cwd(), policy=policy,
        debug_metadata_files={"usr/share/sonic/bazel/syncd-packages.json": inputs["debug_package_metadata"]},
        **{name: inputs[name] for name in IMAGE_INPUTS})
    native = native_packages.validate_native(
        inputs["runtime_handoff"], inputs["debug_handoff"], policy=policy,
        readelf=str(inputs["readelf"].resolve()), objcopy=str(inputs["objcopy"].resolve()),
        **{name: inputs[name] for name in NATIVE_INPUTS})
    inherited = validate_source_base(
        inputs["runtime_path"], inputs["debug_path"],
        metadata_paths=[inputs["base_metadata"], inputs["config_metadata"]],
        source_debug_tar=inputs["base_symbols"], architecture=policy["architecture"],
        readelf=str(inputs["readelf"].resolve()), objcopy=str(inputs["objcopy"].resolve()))
    report = {"_generated": {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
                             "generator": "tools/bazel/tests/integration/syncd_p4_images_test.py"},
              "schema": 1, "p4": p4, "images": images, "native": native, "source_base": inherited,
              "scope": "Actual AMD64 Syncd runtime/debug images, package inputs and exported archives; boot and forwarding are separate."}
    output = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ".")) / "validation.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("Validated both Syncd images and archives against all four pinned P4 runtime packages.")


if __name__ == "__main__":
    main()
