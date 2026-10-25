"""Build an image receipt contract from producer metadata and runtime TARs."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

from sonic_oci.owner_metadata import module_identity, resolve
from sonic_oci.paths import expand_required_paths
from tools.bazel.oci.package_contract import elf_machine, inventory, json_bytes, read_contract


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


def generate(owners, modules, sources, public_sources, output, payloads, *, architecture,
             image=None, distribution=None, mode="contract", expectations=None, module_sources=None, root_module=None):
    if architecture not in ("amd64", "arm64"):
        raise ValueError("source provenance requires a native architecture")
    if set(payloads) != set(sources) or set(public_sources) != set(sources):
        raise ValueError("source metadata requires every declared runtime TAR and public target")
    inventories = {name: inventory(payloads[name]) for name in sources}
    observed = {}
    for name, files in inventories.items():
        paths = {}
        for path, item in files.items():
            kind = "elf" if "elf_machine" in item else item["kind"]
            if kind == "elf" and item["elf_machine"] != elf_machine(architecture):
                raise ValueError("source TAR has a foreign ELF architecture: " + name + ":" + path)
            if kind in ("file", "elf", "symlink"):
                paths[path] = kind
        if not paths:
            raise ValueError("source TAR has no runtime files: " + name)
        observed[name] = {"required_paths": paths}
    # Contracts derive requirements from producers. Existing base-image path
    # policies are used only for their separate installed-provenance reports.
    if mode == "contract":
        if expectations is not None or not image or not distribution:
            raise ValueError("source contracts require image/distribution and producer-owned paths")
        policy = {"schema": 1, "image": image, "architecture": architecture, "distribution": distribution,
                  "packages": observed}
    elif mode == "provenance":
        if expectations is None:
            raise ValueError("source provenance requires its installed path policy")
        policy = json.loads(expectations.read_bytes())
    else:
        raise ValueError("unsupported source contract mode: " + mode)
    resolution_policy = {**policy, "packages": {
        name: {key: value for key, value in paths.items() if key != "excluded_baseline_paths"}
        for name, paths in policy["packages"].items()}}
    module_sources = module_sources or {}
    if mode != "provenance" and module_sources:
        raise ValueError("module-only sources are supported only for provenance")
    image_packages = set(policy["packages"])
    if not set(module_sources) <= set(sources) or not image_packages <= set(sources):
        raise ValueError("image paths differ from source TAR declarations")
    if mode == "contract" and image_packages != set(sources):
        raise ValueError("typed image contracts require every source TAR declaration")
    if not set(module_sources) <= image_packages:
        raise ValueError("module-only sources require image paths")
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
        required_paths = resolution_policy["packages"][name].get("required_paths", [])
        if not expand_required_paths(list(required_paths) if isinstance(required_paths, dict) else required_paths,
                                     architecture):
            raise ValueError("module-only sources require image paths: " + name)
        version = module_identity(modules[module], module)
        identities[module] = {"version": version}
        records["packages"][name] = {"source_module": module}
    overrides = git_revisions(root_module)
    packages = []
    path_policies = {}
    for name, item in records["packages"].items():
        if "payload_sha256" in item:
            with payloads[name].open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != item["payload_sha256"]:
                raise ValueError("generated metadata differs from runtime TAR: " + name)
        files = inventories[name]
        paths = {}
        for path in sorted(expand_required_paths(item.get("producer_required_paths", []), architecture)):
            if path not in files:
                raise ValueError("source TAR lacks its producer-required path: " + name + ":" + path)
            actual = files[path]
            kind = "elf" if "elf_machine" in actual else actual["kind"]
            if kind not in ("file", "elf", "symlink"):
                raise ValueError("unsupported producer-required path kind: " + name + ":" + path)
            paths[path] = kind
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
        requirements = {"required_paths": paths} if mode == "contract" else dict(policy["packages"].get(name, {}))
        if mode == "provenance" and name not in module_sources:
            # Keep sparse image additions and exclusions beside producer checks.
            requirements["required_paths"] = list(dict.fromkeys([
                *item["required_paths"], *item.get("producer_required_paths", [])]))
        path_policies[name] = requirements
        packages.append({**requirements, "package": name, "kind": "runtime-layer", "version": version,
                         "architecture": architecture, "source": source})
    if mode == "provenance":
        result = {"schema": 1, "packages": {
            item["package"]: {**path_policies[item["package"]],
                              "target": item["source"]["target"],
                              "source_revision": item["source"].get("commit", item["source"].get("release")),
                              "source_version": item["source"]["version"]}
            for item in packages}}
    else:
        result = {**policy, "packages": packages}
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
    for name in ("image", "distribution"):
        parser.add_argument("--" + name)
    parser.add_argument("--architecture", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("contract", "provenance"), default="contract")
    parser.add_argument("--expectations", type=Path)
    parser.add_argument("--root-module", type=Path)
    parser.add_argument("--module-source", action="append", default=[])
    for name in ("metadata", "module", "source", "public-source", "payload"):
        parser.add_argument("--" + name, action="append", default=[])
    args = parser.parse_args()
    generate(bindings(args.metadata), bindings(args.module), bindings(args.source, False),
             bindings(args.public_source, False), args.output, bindings(args.payload),
             image=args.image, architecture=args.architecture, distribution=args.distribution,
             mode=args.mode, expectations=args.expectations,
             module_sources=bindings(args.module_source, False), root_module=args.root_module)


if __name__ == "__main__":
    main()
