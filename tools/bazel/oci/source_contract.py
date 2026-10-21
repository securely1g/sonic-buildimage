"""Connect shared owner metadata to the image's installed-path validation contract."""

import argparse
import json
from pathlib import Path
import re

from sonic_oci.owner_metadata import resolve
from tools.bazel.oci.package_contract import json_bytes, read_contract


def generate(expectations, owners, modules, sources, public_sources, output, architecture=None, mode="contract"):
    policy = json.loads(expectations.read_bytes())
    architecture = architecture or policy.get("architecture")
    if architecture not in ("amd64", "arm64"):
        raise ValueError("source provenance requires a native architecture")
    # Baseline exclusions remain image path policy, never producer identities.
    resolution_policy = {**policy, "packages": {
        name: {key: value for key, value in paths.items() if key != "excluded_baseline_paths"}
        for name, paths in policy["packages"].items()}}
    records, identities = resolve(resolution_policy, owners, modules, sources, architecture)
    packages = []
    for name, item in records["packages"].items():
        module = item["source_module"]
        version = identities[module]["version"]
        source = {"module": module, "version": version, "target": public_sources[name]}
        commit = re.search(r"-([0-9a-f]{40})$", version)
        if commit:
            source["commit"] = commit[1]
        else:
            source["release"] = version.rsplit(".sonic.", 1)[0]
        packages.append({**policy["packages"][name], "package": name,
                         "kind": "runtime-layer", "version": version,
                         "architecture": architecture, "source": source})
    if mode == "provenance":
        result = {"schema": 1, "packages": {
            item["package"]: {**policy["packages"][item["package"]],
                              "target": item["source"]["target"],
                              "source_revision": item["source"].get("commit", item["source"].get("release")),
                              "source_version": item["source"]["version"]}
            for item in packages}}
    elif mode == "contract":
        result = {**policy, "architecture": architecture, "packages": packages}
    else:
        raise ValueError("unsupported source contract mode: " + mode)
    result["_generated"] = "AUTO-GENERATED from source-owner metadata. DO NOT EDIT."
    output.write_bytes(json_bytes(result))
    if mode == "contract":
        read_contract(output)
    return result


def bindings(values, paths=True):
    result = {}
    for value in values:
        name, path = value.split("=", 1)
        if name in result:
            raise ValueError("duplicate input: " + name)
        result[name] = Path(path) if paths else path
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"))
    parser.add_argument("--mode", choices=("contract", "provenance"), default="contract")
    for name in ("metadata", "module", "source", "public-source"):
        parser.add_argument("--" + name, action="append", default=[])
    args = parser.parse_args()
    generate(args.expectations, bindings(args.metadata), bindings(args.module),
             bindings(args.source, False), bindings(args.public_source, False), args.output, args.architecture, args.mode)


if __name__ == "__main__":
    main()
