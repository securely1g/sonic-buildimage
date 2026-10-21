"""Connect shared owner metadata to the image's installed-path validation contract."""

import argparse
import json
from pathlib import Path
import re

from sonic_oci.owner_metadata import resolve
from tools.bazel.oci.package_contract import CONTROL_FIELDS, json_bytes, read_contract


def generate(expectations, owners, modules, sources, public_sources, output):
    policy = json.loads(expectations.read_bytes())
    records, identities = resolve(policy, owners, modules, sources, policy["architecture"])
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
        controls = {key: item["control_fields"].get(key, "no" if key == "Multi-Arch" else "")
                    for key in CONTROL_FIELDS}
        packages.append({**policy["packages"][name], "package": name,
                         "version": controls["Version"], "architecture": policy["architecture"],
                         "control_fields": controls, "source": source})
    result = {**policy, "_generated": "AUTO-GENERATED from source-owner metadata. DO NOT EDIT.",
              "packages": packages}
    output.write_bytes(json_bytes(result))
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
    for name in ("metadata", "module", "source", "public-source"):
        parser.add_argument("--" + name, action="append", default=[])
    args = parser.parse_args()
    generate(args.expectations, bindings(args.metadata), bindings(args.module),
             bindings(args.source, False), bindings(args.public_source, False), args.output)


if __name__ == "__main__":
    main()
