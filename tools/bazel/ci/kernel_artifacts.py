#!/usr/bin/env python3
"""Select verified kernel artifacts and a fixed public validation summary."""

import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import public_artifacts
from tools.bazel.kernel import prepare as kernel


def public_metadata(manifest, provenance):
    """Validate the existing writers' fields without changing their bytes."""
    def fields(value, required, optional=()):
        required = set(required)
        kernel.require(isinstance(value, dict) and required <= set(value)
                       and set(value) <= required | set(optional),
                       "unexpected fields in public kernel metadata")

    # sonic-linux-kernel kernel_action.py and buildimage prepare.py own these
    # existing formats. Reject unknown fields rather than filtering originals.
    fields(manifest, ("schema", "architecture", "platform", "kernel_config",
                      "kernel_version", "kernel_abi", "package_version", "signing",
                      "source_date_epoch", "source_archives", "source_tree_sha256",
                      "source_files", "build_tools", "packages", "created_packages", "debug"))
    fields(provenance, ("schema", "source_commit", "consumer_inputs_sha256", "kernel_gitlink",
                        "modules", "launcher", "kernel_config", "manifest_sha256", "target", "resolution"))
    fields(manifest["build_tools"], ("schema_version", "kind", "architecture", "execution",
                                     "identity_sha256", "input_sha256", "packages"))
    kernel.require(manifest["build_tools"]["execution"] == "relocatable",
                   "public kernel tools must retain the producer execution identity")
    # Infra prepare_rootfs.py records the Debian control Package and Version,
    # not binary:Package. Validate the existing map before exporting it.
    tools = manifest["build_tools"]["packages"]
    kernel.require(isinstance(tools, dict) and bool(tools) and all(
        isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", name)
        and isinstance(version, str) and re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version)
        for name, version in tools.items()), "invalid public kernel tool package metadata")
    kernel.require(type(manifest["source_date_epoch"]) is int and manifest["source_date_epoch"] >= 0,
                   "invalid public kernel source timestamp")
    fields(manifest["debug"], ("kernel_build_id", "module_count", "module_build_ids_sha256"))
    package_fields = ("name", "kind", "package", "version", "architecture", "sha256", "size")
    for package in manifest["packages"]:
        fields(package, package_fields)
    for package in manifest["created_packages"]:
        fields(package, (*package_fields, "exported"))
    fields(provenance["resolution"], ("kernel_source_kind", "kernel_source_commit", "modules",
                                     "registry", "infra_source_commit", "graph_sha256", "lock_sha256",
                                     "infra_source_json_sha256", "kernel_source_json_sha256", "graph_inspection"))
    fields(provenance["resolution"]["graph_inspection"],
           ("returncode", "module_count", "module_graph_complete", "extension_inspection_complete"),
           ("unused_extension_failures",))
    inspection = provenance["resolution"]["graph_inspection"]
    code = inspection["returncode"]
    kernel.require(type(code) is int and code in (0, 2)
                   and type(inspection["module_count"]) is int and inspection["module_count"] > 0
                   and inspection["module_graph_complete"] is True
                   and inspection["extension_inspection_complete"] is (code == 0),
                   "invalid public kernel graph inspection")
    if code == 0:
        kernel.require("unused_extension_failures" not in inspection,
                       "unexpected public kernel graph diagnostics")
    else:
        # The two literal labels emitted by resolution.inspect_graph's local
        # groups. Keep raw diagnostic text out of the existing public record.
        known = ("abseil-cpp@20240722.0:cc_configure_extension", "rules_apple@3.5.1:apple")
        failures = inspection.get("unused_extension_failures")
        kernel.require(isinstance(failures, list) and bool(failures)
                       and failures == [name for name in known if name in failures],
                       "unexpected public kernel graph diagnostics")
    public_artifacts.safe_json(manifest)
    public_artifacts.safe_json(provenance)


def collect(workspace, state, output, architecture="amd64", debs_directory=None):
    kernel.require(not output.exists(), "kernel artifact destination already exists")
    packages = Path(debs_directory or kernel.DEBS_DIRECTORY)
    if not packages.is_absolute():
        packages = workspace / packages
    verified = kernel.verify(packages, workspace, architecture=architecture)
    retained = {}
    for name, kind in ((kernel.MANIFEST, "manifest"), (kernel.PROVENANCE, "provenance")):
        data = kernel.regular(packages / name).read_bytes()
        kernel.require(public_artifacts.load_json(data) == verified[kind],
                       "verified kernel metadata changed before collection")
        retained[name] = data
    public_metadata(verified["manifest"], verified["provenance"])
    kernel.require(hashlib.sha256(retained[kernel.MANIFEST]).hexdigest()
                   == verified["provenance"]["manifest_sha256"],
                   "kernel manifest bytes differ from verified provenance")
    selected = verified["provenance"]["resolution"]
    records = {}
    for name, path, hash_field in (
        ("MODULE.bazel.lock", state / "consumer/MODULE.bazel.lock", "lock_sha256"),
        ("module-graph.json", state / "build/module-graph.json", "graph_sha256"),
        ("kernel-source.json", state / "build/kernel-source.json", "kernel_source_json_sha256"),
        ("infra-source.json", state / "build/infra-source.json", "infra_source_json_sha256"),
    ):
        data = kernel.regular(path).read_bytes()
        kernel.require(hashlib.sha256(data).hexdigest() == selected[hash_field],
                       "kernel dependency record differs from verified provenance")
        records[name] = hashlib.sha256(data).hexdigest()
    artifacts = []
    for item in verified["manifest"]["packages"]:
        artifacts.append({key: item[key] for key in ("name", "sha256", "size")})
    summary = {
        "schema": 1,
        "architecture": architecture,
        "source_commit": verified["provenance"]["source_commit"],
        "kernel_gitlink": verified["provenance"]["kernel_gitlink"],
        "target": kernel.TARGET,
        "artifact_count": len(artifacts),
        "artifacts": sorted(artifacts, key=lambda item: item["name"]),
        "manifest_sha256": hashlib.sha256(retained[kernel.MANIFEST]).hexdigest(),
        "provenance_sha256": hashlib.sha256(retained[kernel.PROVENANCE]).hexdigest(),
        "resolution_sha256": records,
    }
    public_artifacts.safe_json(summary)
    # Retain only the two original records required by the unchanged consumer.
    # Raw dependency records remain hash-only; diagnostics stay private.
    output.mkdir(parents=True)
    try:
        for item in artifacts:
            destination = output / item["name"]
            shutil.copyfile(kernel.regular(packages / item["name"]), destination)
            kernel.require(destination.stat().st_size == item["size"]
                           and kernel.sha256(destination) == item["sha256"],
                           "kernel artifact changed during artifact collection")
        for name, data in retained.items():
            (output / name).write_bytes(data)
        (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    except Exception:
        shutil.rmtree(output)
        raise
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--debs-directory", type=Path)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), default=kernel.native_architecture())
    args = parser.parse_args()
    collect(args.workspace.resolve(strict=True), args.state_dir.resolve(strict=True), args.output,
            args.architecture, args.debs_directory)
    print("Kernel artifacts and public validation summary verified.")


if __name__ == "__main__":
    main()
