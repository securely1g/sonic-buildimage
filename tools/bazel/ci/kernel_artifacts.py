#!/usr/bin/env python3
"""Select verified kernel packages and a fixed public validation summary."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import kernel, public_artifacts


def collect(workspace, state, output):
    kernel.require(not output.exists(), "kernel artifact destination already exists")
    verified = kernel.verify(state / "bundle", workspace)
    selected = verified["provenance"]["resolution"]
    records = {}
    for name, path, hash_field in (
        ("MODULE.bazel.lock", state / "consumer/MODULE.bazel.lock", "lock_sha256"),
        ("module-graph.json", state / "build/module-graph.json", "graph_sha256"),
        ("infra-source.json", state / "build/infra-source.json", "infra_source_json_sha256"),
    ):
        data = kernel.regular(path).read_bytes()
        kernel.require(hashlib.sha256(data).hexdigest() == selected[hash_field],
                       "kernel dependency record differs from verified provenance")
        records[name] = hashlib.sha256(data).hexdigest()
    packages = []
    for item in verified["manifest"]["packages"]:
        packages.append({key: item[key] for key in ("name", "sha256", "size")})
    summary = {
        "schema": 1,
        "source_commit": verified["provenance"]["source_commit"],
        "kernel_gitlink": verified["provenance"]["kernel_gitlink"],
        "target": kernel.TARGET,
        "package_count": len(packages),
        "packages": sorted(packages, key=lambda item: item["name"]),
        "manifest_sha256": kernel.sha256(state / "bundle" / kernel.MANIFEST),
        "provenance_sha256": kernel.sha256(state / "bundle" / kernel.PROVENANCE),
        "resolution_sha256": records,
    }
    public_artifacts.safe_json(summary)
    # Only values constrained by kernel.verify's exact package/source contract
    # enter the summary. Complete metadata and diagnostic bytes stay private.
    output.mkdir(parents=True)
    try:
        for item in packages:
            destination = output / item["name"]
            shutil.copyfile(kernel.regular(state / "bundle" / item["name"]), destination)
            kernel.require(destination.stat().st_size == item["size"]
                           and kernel.sha256(destination) == item["sha256"],
                           "kernel package changed during artifact collection")
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
    args = parser.parse_args()
    collect(args.workspace.resolve(strict=True), args.state_dir.resolve(strict=True), args.output)
    print("Kernel packages and public validation summary verified.")


if __name__ == "__main__":
    main()
