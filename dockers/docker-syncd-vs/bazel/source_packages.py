#!/usr/bin/env python3
"""Check the owner-built libraries shared by SWSS and Syncd before OCI assembly.

The package contract declares dependency compatibility; it is not a Debian
installation record. Receipts identify the actual owner modules and tar bytes.
Make manifests remain separately identifiable when their package inventories are
combined with these source records for APT dependency selection.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
sys.path.insert(0, str(Path(__file__).absolute().parent))
from tools.bazel.ci.artifact_validation import file_metadata, metadata, path_name, require, sha
from tools.bazel.oci.oci_inventory import apply_layer, assert_overlay_paths, resolve_path
from tools.bazel.oci.oci_layout import validate_layout
import validate_payloads


PACKAGES = ("libswsscommon", "libsairedis", "libsaimetadata")
CONTROL_FIELDS = {"Package", "Version", "Architecture", "Depends", "Pre-Depends", "Provides", "Multi-Arch"}
SOURCE_FIELDS = {"module", "version", "commit", "target"}
IDENTITY = {"schema": 1, "image": "docker-syncd-vs", "architecture": "amd64", "distribution": "trixie"}


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def package_identity(record):
    """Reject incomplete dependency declarations and unverifiable source labels."""
    name = record.get("package")
    require(name in PACKAGES and record.get("version") == "1.0.0" and
            record.get("architecture") == "amd64", "unsupported source package identity")
    fields = record.get("control_fields")
    require(isinstance(fields, dict) and set(fields) == CONTROL_FIELDS and
            all(isinstance(value, str) for value in fields.values()) and
            (fields["Package"], fields["Version"], fields["Architecture"]) ==
            (name, record["version"], record["architecture"]),
            "invalid source package dependency controls: " + name)
    source = record.get("source")
    require(isinstance(source, dict) and set(source) == SOURCE_FIELDS and
            all(isinstance(value, str) and value for value in source.values()),
            "invalid source package provenance: " + name)
    module = "sonic-swss-common" if name == "libswsscommon" else "sonic-sairedis"
    targets = {"libswsscommon": "@sonic_swss_common//:libswsscommon_pkg",
               "libsairedis": "@sonic_sairedis//lib:libsairedis_pkg",
               "libsaimetadata": "@sonic_sairedis//meta:libsaimetadata_pkg"}
    require(source["module"] == module and source["target"] == targets[name] and
            re.fullmatch(r"[0-9a-f]{40}", source["commit"]) and
            source["version"].endswith("-" + source["commit"]),
            "source package does not identify its public owner target: " + name)


def read_contract(path):
    """Read the reviewed install/dependency contract without importing Make data."""
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict) and all(value.get(key) == expected for key, expected in IDENTITY.items()),
            "unsupported source package contract")
    packages = value.get("packages")
    require(isinstance(packages, list) and len(packages) == len(PACKAGES) and
            {record.get("package") for record in packages} == set(PACKAGES),
            "source package contract must describe the three shared libraries")
    for record in packages:
        package_identity(record)
        paths = record.get("required_paths")
        require(isinstance(paths, dict) and paths and
                all(isinstance(path, str) and kind in ("file", "elf", "symlink") for path, kind in paths.items()),
                "source package contract lacks required paths: " + record["package"])
        for name in paths:
            require(path_name(name) == name, "noncanonical required source path: " + name)
        modes = record.get("path_modes", {})
        require(isinstance(modes, dict) and all(isinstance(path, str) and type(mode) is int and
                0 <= mode <= 0o777 for path, mode in modes.items()), "invalid source package file modes")
        for name in modes:
            require(path_name(name) == name, "noncanonical source mode path: " + name)
        inherited = record.get("required_inherited_files", {})
        require(isinstance(inherited, dict), "invalid inherited source package files")
        for name, item in inherited.items():
            require(path_name(name) == name and isinstance(item, dict) and item.get("kind") in ("file", "symlink"),
                    "invalid inherited source package file: " + name)
    return value


def verify_modules(contract, module_files):
    """Bind provenance to the resolved registry modules, including their versions."""
    expected = {record["source"]["module"]: record["source"]["version"] for record in contract["packages"]}
    require(all(expected[record["source"]["module"]] == record["source"]["version"]
                for record in contract["packages"]), "source packages disagree on their owner module version")
    require(set(module_files) == set(expected), "missing or unexpected source module files")
    digests = {}
    for name, version in expected.items():
        raw = module_files[name].read_bytes()
        definitions = re.findall(r"(?m)^module\s*\((.*?)\)", raw.decode(), re.DOTALL)
        require(len(definitions) == 1, "source module declaration is missing or ambiguous: " + name)
        fields = {key: re.findall(r"\b" + key + r"\s*=\s*[\"']([^\"']+)[\"']", definitions[0])
                  for key in ("name", "version")}
        require(fields == {"name": [name], "version": [version]},
                "resolved source module differs from the reviewed contract: " + name)
        digests[name] = hashlib.sha256(raw).hexdigest()
    return digests


def normalized_source_member(member, *, path_modes=None):
    """Use the imported adapter's checked directory mapping and SWSS root ownership."""
    owned = copy.copy(member)
    owned.uid = owned.gid = 0
    owned.uname = owned.gname = "root"
    owned.pax_headers = {key: value for key, value in member.pax_headers.items()
                         if key not in {"uid", "gid", "uname", "gname"}}
    # Linux symlink permissions are always 0777. Owner mtree entries may omit
    # that mode and produce 0000 tar headers despite identical link behavior.
    if owned.issym():
        owned.mode = 0o777
    normalized = validate_payloads.normalized_member(owned)
    if normalized is not None and normalized.isfile():
        mode = (path_modes or {}).get(path_name(normalized.name))
        if mode is not None:
            normalized.mode = mode
    return normalized


def inventory(path):
    """Inventory normalized tar bytes without following host filesystem links."""
    files = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            name = path_name(member.name)
            require(name not in files, "duplicate source package archive path: " + name)
            require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                    "unsupported source package archive member: " + name)
            require(not PurePosixPath(name).name.startswith(".wh."), "source package archive contains a whiteout")
            item = metadata(member)
            require(item["uid"] == item["gid"] == 0, "source package archive is not root-owned: " + name)
            if member.isfile():
                item.update(file_metadata(archive.extractfile(member), name))
            files[name] = item
    assert_overlay_paths(files, {})
    return files


def combine_tars(contract, paths, output_path, base_files, *, debug=False):
    """Normalize declared owner tars, preserving per-package inventories and bytes."""
    require(set(paths) == set(PACKAGES), "missing or unexpected shared-library tar inputs")
    files, records = {}, {}
    with tarfile.open(output_path, "w", format=tarfile.PAX_FORMAT) as output:
        for record in contract["packages"]:
            name = record["package"]
            before = sha(paths[name])
            package_files = {}
            with tarfile.open(paths[name], "r:*") as source:
                for member in source:
                    normalized = normalized_source_member(member, path_modes=None if debug else record.get("path_modes"))
                    if normalized is None:
                        continue
                    path = path_name(normalized.name)
                    item = metadata(normalized)
                    if member.isfile():
                        item.update(file_metadata(source.extractfile(member), path))
                    require(path not in package_files or
                            ((debug or item["kind"] == "directory") and package_files[path] == item),
                            "duplicate source package path: " + name + ":" + path)
                    require("elf_machine" not in item or item["elf_machine"] == 62,
                            "source package has a foreign ELF architecture: " + name + ":" + path)
                    if debug and item["kind"] != "directory":
                        require(re.fullmatch(r"usr/lib/debug/\.build-id/[0-9a-f]{2}/[0-9a-f]+\.debug", path) and
                                item.get("elf_machine") == 62,
                                "source symbols contain an unexpected file: " + name + ":" + path)
                    package_files[path] = item
                    if path in files:
                        require((debug or item["kind"] == "directory") and files[path] == item,
                                "shared source packages overlap: " + path)
                        continue
                    files[path] = item
                    output.addfile(normalized, source.extractfile(member) if member.isfile() else None)
            require(sha(paths[name]) == before, "source package tar changed during assembly: " + name)
            if debug:
                require(any(item.get("elf_machine") == 62 for item in package_files.values()),
                        "source package has no debug symbols: " + name)
            else:
                for path, kind in record["required_paths"].items():
                    item = package_files.get(path, {})
                    require(item.get("kind") == ("file" if kind == "elf" else kind) and
                            (kind != "elf" or item.get("elf_machine") == 62),
                            "source package lacks a required installed path: " + name + ":" + path)
                for path, mode in record.get("path_modes", {}).items():
                    require(package_files.get(path, {}).get("kind") == "file" and
                            package_files[path]["mode"] == mode,
                            "source package lacks a reviewed file mode: " + name + ":" + path)
            records[name] = {"input_tar_sha256": before, "files": package_files}
    assert_overlay_paths(files, base_files)
    installed = base_files | files
    for path, item in files.items():
        if item["kind"] in ("symlink", "hardlink"):
            require(resolve_path(path, installed) in installed,
                    "source package has an unresolved or unsafe link: " + path)
    return records, {"sha256": sha(output_path), "size": output_path.stat().st_size, "members": len(files)}


def assemble(contract_path, packages, debug_packages, module_files, base, runtime_tar, debug_tar):
    """Produce checked runtime/symbol archives and their source-owned receipt."""
    contract = read_contract(contract_path)
    modules = verify_modules(contract, module_files)
    base_digest = validate_payloads.base_aliases(base)
    base_files = {}
    for layer in validate_layout(base, "linux/amd64").layers:
        apply_layer(layer, base_files)
    inherited = {}
    for record in contract["packages"]:
        for path, expected in record.get("required_inherited_files", {}).items():
            require(base_files.get(path) == expected,
                    "source package requires unchanged inherited content: " + path)
            inherited[path] = expected
    runtime_tar.parent.mkdir(parents=True, exist_ok=True)
    debug_tar.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".source-packages-", dir=runtime_tar.parent) as temporary:
        runtime_output, debug_output = Path(temporary) / "runtime.tar", Path(temporary) / "debug.tar"
        runtime_records, payload = combine_tars(contract, packages, runtime_output, base_files)
        runtime_files = dict(base_files)
        apply_layer(runtime_output, runtime_files, checked_overlay=True)
        require(all(runtime_files.get(path) == expected for path, expected in inherited.items()),
                "source package changes required inherited content")
        debug_records, debug_payload = combine_tars(contract, debug_packages, debug_output, runtime_files, debug=True)
        records = []
        for record in contract["packages"]:
            name = record["package"]
            records.append({key: copy.deepcopy(record[key]) for key in
                            ("package", "version", "architecture", "control_fields", "source")} |
                           runtime_records[name] | {"debug": debug_records[name]})
        result = {**IDENTITY, "kind": "bazel_source", "contract_sha256": sha(contract_path),
                  "base_manifest_digest": base_digest, "module_file_sha256": modules,
                  "packages": records, "payload": payload, "debug_payload": debug_payload,
                  "inherited_files": inherited}
        runtime_output.replace(runtime_tar)
        debug_output.replace(debug_tar)
    return result


def receipt_records(value):
    """Check receipt structure before consuming package ownership or provenance."""
    require(isinstance(value, dict) and all(value.get(key) == expected for key, expected in IDENTITY.items()) and
            value.get("kind") == "bazel_source", "unsupported source package receipt")
    require(re.fullmatch(r"[0-9a-f]{64}", value.get("contract_sha256", "")) and
            re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("base_manifest_digest", "")),
            "source receipt lacks its contract or base identity")
    modules = value.get("module_file_sha256")
    require(isinstance(modules, dict) and set(modules) == {"sonic-swss-common", "sonic-sairedis"} and
            all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) for digest in modules.values()),
            "source receipt lacks its resolved module identities")
    records = value.get("packages")
    require(isinstance(records, list) and len(records) == len(PACKAGES) and
            {record.get("package") for record in records} == set(PACKAGES),
            "source receipt must contain the three shared packages")
    for record in records:
        package_identity(record)
        for inputs in (record, record.get("debug", {})):
            require(re.fullmatch(r"[0-9a-f]{64}", inputs.get("input_tar_sha256", "")) and
                    isinstance(inputs.get("files"), dict) and inputs["files"],
                    "source receipt lacks a package tar identity and inventory")
    return records


def validate_receipt(receipt_path, runtime_tar, debug_tar=None, *, contract_path=None):
    """Bind image/native validation to the exact source payloads and package owners."""
    value = json.loads(receipt_path.read_bytes())
    records = receipt_records(value)
    if contract_path is not None:
        contract = read_contract(contract_path)
        require(value["contract_sha256"] == sha(contract_path), "source receipt uses a different reviewed contract")
        expected_records = {record["package"]: record for record in contract["packages"]}
        expected_inherited = {path: item for record in contract["packages"]
                              for path, item in record.get("required_inherited_files", {}).items()}
        require(value.get("inherited_files", {}) == expected_inherited,
                "source receipt differs from required inherited content")
        for record in records:
            expected = expected_records[record["package"]]
            require(all(record[key] == expected[key] for key in
                        ("package", "version", "architecture", "control_fields", "source")),
                    "source receipt package differs from the reviewed contract: " + record["package"])
            for path, kind in expected["required_paths"].items():
                item = record["files"].get(path, {})
                require(item.get("kind") == ("file" if kind == "elf" else kind) and
                        (kind != "elf" or item.get("elf_machine") == 62),
                        "source receipt lacks a required installed path: " + path)
            for path, mode in expected.get("path_modes", {}).items():
                item = record["files"].get(path, {})
                require(item.get("kind") == "file" and item.get("mode") == mode,
                        "source receipt differs from a reviewed file mode: " + path)
    for path, field, section in ((runtime_tar, "payload", None), (debug_tar, "debug_payload", "debug")):
        if path is None:
            continue
        descriptor = value.get(field, {})
        require(path.is_file() and sha(path) == descriptor.get("sha256") and
                path.stat().st_size == descriptor.get("size"), "source receipt archive hash differs: " + field)
        expected = {}
        for record in records:
            for name, item in (record[section] if section else record)["files"].items():
                require(name not in expected or ((section == "debug" or item.get("kind") == "directory") and
                                                expected[name] == item),
                        "source receipt packages overlap: " + name)
                expected[name] = item
        require(inventory(path) == expected and len(expected) == descriptor.get("members"),
                "source receipt archive inventory differs: " + field)
    return value


def retained_manifest(manifest_path, receipt, receipt_sha256, *, variant):
    """Augment dependency selection inputs without relabeling source tars as DEBs."""
    receipt_records(receipt)
    require(variant in ("runtime", "debug"), "invalid retained source package variant")
    raw = manifest_path.read_bytes()
    value = json.loads(raw)
    require(isinstance(value, dict) and all(value.get(key) == expected for key, expected in IDENTITY.items()) and
            value.get("variant") == variant and value.get("features") == validate_payloads.FEATURES,
            "invalid Make manifest for source package selection")
    require(not any(key in value for key in ("source_packages", "source_receipt_sha256", "make_manifest_sha256")),
            "Make manifest already contains source package extensions")
    validate_payloads.reject_source_packages(value.get("packages", []), allow_base_symbols=(variant == "debug"))
    value.update(source_packages=copy.deepcopy(receipt["packages"]),
                 source_receipt_sha256=receipt_sha256,
                 make_manifest_sha256=hashlib.sha256(raw).hexdigest())
    return value


def bindings(values):
    result = {}
    for value in values:
        name, separator, path = value.partition("=")
        require(separator and name and path and name not in result, "invalid or duplicate source input binding: " + value)
        result[name] = Path(path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--debug-package", action="append", default=[])
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--out-tar", type=Path)
    parser.add_argument("--out-debug-tar", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--merge-manifest", type=Path)
    parser.add_argument("--source-receipt", type=Path)
    parser.add_argument("--out-manifest", type=Path)
    for variant in ("runtime", "debug"):
        parser.add_argument("--" + variant + "-manifest", type=Path)
        parser.add_argument("--" + variant + "-retained-manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.merge_manifest is not None:
            require(args.source_receipt is not None and args.out_manifest is not None,
                    "manifest merge requires a source receipt and output manifest")
            require(not any((args.contract, args.base, args.out_tar, args.out_debug_tar, args.receipt,
                             args.package, args.debug_package, args.module, args.runtime_manifest,
                             args.debug_manifest, args.runtime_retained_manifest, args.debug_retained_manifest)),
                    "manifest merge cannot also assemble source packages")
            # The receipt is an output of the declared source assembly action;
            # its runtime/debug archives are verified there and by image checks.
            result = json.loads(args.source_receipt.read_bytes())
            variant = json.loads(args.merge_manifest.read_bytes()).get("variant")
            combined = retained_manifest(args.merge_manifest, result, sha(args.source_receipt), variant=variant)
            args.out_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.out_manifest.write_bytes(json_bytes(combined))
            return
        require(args.source_receipt is None and args.out_manifest is None and
                all((args.contract, args.base, args.out_tar, args.out_debug_tar, args.receipt)),
                "source assembly requires its contract, base, archives and receipt")
        result = assemble(args.contract, bindings(args.package), bindings(args.debug_package), bindings(args.module),
                          args.base, args.out_tar, args.out_debug_tar)
        receipt_bytes = json_bytes(result)
        digest = hashlib.sha256(receipt_bytes).hexdigest()
        combined = []
        for variant in ("runtime", "debug"):
            source = getattr(args, variant + "_manifest")
            destination = getattr(args, variant + "_retained_manifest")
            require((source is None) == (destination is None), "retained manifest requires both input and output")
            if source is not None:
                combined.append((destination, json_bytes(retained_manifest(source, result, digest, variant=variant))))
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_bytes(receipt_bytes)
        for destination, contents in combined:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "Syncd source package validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
