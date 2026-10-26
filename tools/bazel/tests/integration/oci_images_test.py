"""Validate complete source-layer images declared by their container owner."""

import argparse
import json
import os
from pathlib import Path

from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci import image_validation
from tools.bazel.oci.package_contract import read_policy
from tools.bazel.oci.source_base_validation import validate_source_base

IMAGE_INPUTS = (
    "runtime_path", "debug_path", "runtime_manifest_path", "debug_manifest_path",
    "apt_lock_path", "base_path", "apt_layer", "debug_tools_layer",
    "runtime_apt_selection", "debug_apt_selection", "runtime_archive", "debug_archive",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (*IMAGE_INPUTS, "base_symbols", "base_metadata", "config_metadata", "workspace_module",
                 "policy", "make_runtime_archive", "make_debug_archive", "readelf", "objcopy"):
        parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    for name in ("runtime_payload_layer", "debug_payload_layer"):
        parser.add_argument("--" + name.replace("_", "-"), action="append", required=True, type=Path)
    inputs = vars(parser.parse_args())
    policy = read_policy(inputs["policy"])
    for variant in ("runtime", "debug"):
        require(sha(inputs["make_" + variant + "_archive"]) == sha(inputs[variant + "_archive"]),
                variant + " Make archive differs from the Bazel output")
    images = image_validation.validate_images(
        source_root=Path.cwd(), policy=policy, runtime_handoff=None, debug_handoff=None,
        runtime_payload_layers=inputs["runtime_payload_layer"],
        debug_payload_layers=inputs["debug_payload_layer"],
        **{name: inputs[name] for name in IMAGE_INPUTS})
    inherited = validate_source_base(
        inputs["runtime_path"], inputs["debug_path"],
        metadata_paths=[inputs["base_metadata"], inputs["config_metadata"]],
        source_debug_tar=inputs["base_symbols"], architecture=policy["architecture"],
        workspace_module=inputs["workspace_module"],
        readelf=str(inputs["readelf"].resolve()), objcopy=str(inputs["objcopy"].resolve()))
    report = {"schema": 1, "images": images, "source_base": inherited,
              "scope": "Actual source-layer runtime/debug images and exported archives; boot and forwarding are separate."}
    output = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ".")) / "validation.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
