#!/usr/bin/env python3
"""Check explicit syncd package files against its base and native packages."""

import argparse
import hashlib
import json
import re
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
sys.path.insert(0, str(Path(__file__).absolute().parent))
from tools.bazel.ci.artifact_validation import require
from sonic_apt import dependencies, selection
import validate_image
import validate_payloads


def inherited_replacements(metadata_path, *, variant, retained, make):
    """Authorize only the checked debug Make OpenSSH overlay transition."""
    if variant == "runtime":
        require(metadata_path is None, "runtime cannot consume inherited package metadata")
        return {}, []
    require(metadata_path is not None, "debug requires the runtime package selection receipt")
    document = json.loads(metadata_path.read_bytes())
    require(document.get("variant") == "runtime" and
            make.get("runtime_manifest_sha256") == document.get("make_manifest_sha256") and
            re.fullmatch(r"[0-9a-f]{64}", make.get("runtime_manifest_sha256", "")),
            "debug manifest does not match the runtime Make package selection")
    records = document.get("dependency_check", {}).get("packages", {})
    replacement = retained.get("openssh-client", {})
    previous = records.get("openssh-client")
    replacements, evidence = {}, []
    if previous and "+fips" in replacement.get("version", ""):
        # Shared selection checks the exact original record and forbids changing
        # dpkg-installed packages. The payload/image checks bind this inventory
        # transition to the corresponding Make files and their FIPS owner.
        require(all(re.fullmatch(r"[0-9a-f]{64}", replacement.get(field, ""))
                    for field in ("source_sha256", "payload_sha256", "control_sha256")),
                "debug FIPS OpenSSH replacement lacks Make package identity")
        current = replacement["control"]
        require(current["Package"] == "openssh-client" and
                current["Architecture"] == previous["Architecture"] == "amd64",
                "invalid debug FIPS OpenSSH package record")
        require(all(current.get(field, "") == previous.get(field, "")
                    for field in ("Depends", "Pre-Depends", "Provides", "Multi-Arch")),
                "debug FIPS OpenSSH replacement changes runtime package relationships")
        replacements["openssh-client"] = previous
        evidence.append({"package": "openssh-client", "runtime_version": previous["Version"],
                         "debug_version": current["Version"],
                         **{field: replacement[field] for field in
                            ("source_sha256", "payload_sha256", "control_sha256")}})
    return replacements, evidence


def select(base, lock_path, make_manifest_path, mapping_path, *, variant, base_package_metadata=None):
    make_bytes = make_manifest_path.read_bytes()
    make = json.loads(make_bytes)
    require(make.get("schema") == 1 and make.get("image") == "docker-syncd-vs" and
            make.get("variant") == variant and make.get("architecture") == "amd64" and
            make.get("distribution") == "trixie" and make.get("features") == validate_payloads.FEATURES,
            "invalid syncd Make package manifest")
    retained = {item["package"]: item for item in make.get("packages", [])}
    require(retained and len(retained) == len(make["packages"]), "missing or duplicate Make packages")
    for name, item in retained.items():
        fields = item.get("control_fields")
        require(isinstance(fields, dict) and
                all(fields.get(field) == item[key] for field, key in
                    (("Package", "package"), ("Version", "version"), ("Architecture", "architecture"))),
                "Make package handoff lacks full original control metadata: " + name)
        item["control"] = dependencies.control_fields(
            dependencies.package_from_fields(fields, origin="Make " + name))
    descriptor, _, _, layers = validate_image.image(base)
    files = {}
    for layer in layers:
        validate_image.apply_layer(layer, files)

    def inspect_payload(path):
        entries = {}
        validate_image.apply_layer(path, entries, checked_overlay=True)
        return entries

    replacements, replacement_evidence = inherited_replacements(
        base_package_metadata, variant=variant, retained=retained, make=make)
    selected, receipt = selection.select(
        lock_path, mapping_path, group=variant, architecture="amd64",
        installed=selection.base_packages(layers, architecture="amd64"), base_files=files,
        retained_packages=retained, inspect_payload=inspect_payload,
        check_overlay=validate_image.assert_overlay_paths,
        base_package_metadata=base_package_metadata, retained_replacements=replacements)
    receipt["provided_package_replacements"] = replacement_evidence
    receipt["variant"] = receipt.pop("group")
    receipt["skipped_make"] = receipt.pop("skipped_retained")
    receipt.update(base_manifest_digest=descriptor["digest"],
                   make_manifest_sha256=hashlib.sha256(make_bytes).hexdigest())
    return selected, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--retained-manifest", "--make-manifest", dest="make_manifest", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--base-package-metadata", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        selected, receipt = select(args.base, args.lock, args.make_manifest, args.mapping, variant=args.variant,
                                   base_package_metadata=args.base_package_metadata)
        selection.stage_payloads(selected, args.out_dir)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd APT package validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
