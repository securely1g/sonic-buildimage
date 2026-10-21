"""Connect shared owner metadata to the image's installed-path validation contract."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

from sonic_oci.owner_metadata import module_identity, resolve
from tools.bazel.oci.package_contract import json_bytes, read_contract


def git_revisions(root_module):
    """Read the actual root selections for owners still using git_override."""
    revisions = {}
    if root_module is None:
        return revisions
    for node in ast.parse(root_module.read_text()).body:
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "git_override"):
            continue
        fields = {item.arg: ast.literal_eval(item.value) for item in node.value.keywords
                  if item.arg in ("module_name", "commit")}
        name, commit = fields.get("module_name"), fields.get("commit")
        if not name or name in revisions or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("source provenance requires unique pinned git overrides")
        revisions[name] = commit
    return revisions


def generate(expectations, owners, modules, sources, public_sources, output,
             architecture=None, mode="contract", payloads=None, module_sources=None, root_module=None):
    policy = json.loads(expectations.read_bytes())
    architecture = architecture or policy.get("architecture")
    if architecture not in ("amd64", "arm64"):
        raise ValueError("source provenance requires a native architecture")
    # Baseline exclusions remain image path policy, never producer identities.
    resolution_policy = {**policy, "packages": {
        name: {key: value for key, value in paths.items() if key != "excluded_baseline_paths"}
        for name, paths in policy["packages"].items()}}
    module_sources = module_sources or {}
    if mode != "provenance" and module_sources:
        raise ValueError("module-only sources are supported only for provenance")
    if not set(module_sources) <= set(sources) or set(policy["packages"]) != set(sources):
        raise ValueError("image paths differ from source TAR declarations")
    # Existing package metadata remains authoritative where available. The
    # fallback records only source identity for owners without that metadata;
    # it never invents Debian controls or bypasses an owner's TAR hash check.
    declared_packages = {name for path in owners.values()
                         for name in json.loads(path.read_text()).get("packages", {})}
    if declared_packages.intersection(module_sources):
        raise ValueError("module-only provenance cannot replace owner package metadata")
    owner_policy = {**resolution_policy, "packages": {
        name: paths for name, paths in resolution_policy["packages"].items() if name not in module_sources}}
    owner_sources = {name: label for name, label in sources.items() if name not in module_sources}
    records, identities = resolve(owner_policy, owners, modules, owner_sources, architecture)
    for name, module in module_sources.items():
        if module not in modules:
            raise ValueError("source provenance lacks its selected module: " + module)
        if set(resolution_policy["packages"][name]) - {
                "required_paths", "path_modes", "path_owners", "required_inherited_files"}:
            raise ValueError("image policy duplicates owner package metadata: " + name)
        version = module_identity(modules[module], module)
        identities[module] = {"version": version}
        records["packages"][name] = {"source_module": module}
    overrides = git_revisions(root_module)
    packages = []
    for name, item in records["packages"].items():
        if "payload_sha256" in item:
            if payloads is None or name not in payloads:
                raise ValueError("generated metadata requires its runtime TAR: " + name)
            with payloads[name].open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != item["payload_sha256"]:
                raise ValueError("generated metadata differs from runtime TAR: " + name)
        module = item["source_module"]
        version = identities[module]["version"]
        source = {"module": module, "version": version, "target": public_sources[name]}
        commit = re.search(r"-([0-9a-f]{40})$", version)
        if module in overrides:
            source["commit"] = overrides[module]
        elif commit:
            source["commit"] = commit[1]
        elif version == "0.0.0":
            raise ValueError("unversioned source provenance requires its root git override: " + module)
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
    parser.add_argument("--root-module", type=Path)
    parser.add_argument("--module-source", action="append", default=[])
    for name in ("metadata", "module", "source", "public-source", "payload"):
        parser.add_argument("--" + name, action="append", default=[])
    args = parser.parse_args()
    generate(args.expectations, bindings(args.metadata), bindings(args.module),
             bindings(args.source, False), bindings(args.public_source, False), args.output,
             args.architecture, args.mode, payloads=bindings(args.payload),
             module_sources=bindings(args.module_source, False), root_module=args.root_module)


if __name__ == "__main__":
    main()
