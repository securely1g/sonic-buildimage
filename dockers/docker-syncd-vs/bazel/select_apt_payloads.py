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
from tools.bazel.ci.artifact_validation import require, sha
from sonic_apt import selection
import validate_image
import validate_payloads


def provided_packages(mapping_path, *, variant, retained):
    """Read runtime overlays that are not recorded in the inherited dpkg status."""
    records = {}
    for item in json.loads(mapping_path.read_bytes()).get("provided_packages", []):
        record = selection.control_record(Path(item["control"]), architecture="amd64")
        require(record["Package"] not in records, "duplicate provided runtime package")
        records[record["Package"]] = record
    replacements = []
    replacement = retained.get("openssh-client", {})
    previous = records.get("openssh-client")
    if variant == "debug" and previous and "+fips" in replacement.get("version", ""):
        # The debug Make handoff deliberately replaces the runtime APT OpenSSH.
        # checked_debug_payloads validates its bytes before layer assembly, and
        # validate_image accepts changed ELF files only from this FIPS owner.
        # Here we describe that final inventory without altering installed base
        # records or weakening the shared check for any other duplicate.
        require(re.fullmatch(r"[0-9a-f]{64}", replacement.get("source_sha256", "")) and
                re.fullmatch(r"[0-9a-f]{64}", replacement.get("payload_sha256", "")) and
                re.fullmatch(r"[0-9a-f]{64}", replacement.get("control_sha256", "")),
                "debug FIPS OpenSSH replacement lacks Make package identity")
        require(replacement.get("architecture") == previous["Architecture"] == "amd64" and
                isinstance(replacement.get("control_fields"), dict),
                "invalid debug FIPS OpenSSH package record")
        current = {**replacement["control_fields"], "Version": replacement["version"]}
        require(all(current.get(field, "") == previous.get(field, "")
                    for field in ("Depends", "Pre-Depends", "Provides")),
                "debug FIPS OpenSSH replacement changes runtime package relationships")
        records.pop("openssh-client")
        replacements.append({"package": "openssh-client", "runtime_version": previous["Version"],
                             "debug_version": current["Version"],
                             "source_sha256": replacement["source_sha256"],
                             "payload_sha256": replacement["payload_sha256"],
                             "control_sha256": replacement["control_sha256"]})
    return records, replacements


def select(base, lock_path, make_manifest_path, mapping_path, *, variant):
    make_bytes = make_manifest_path.read_bytes()
    make = json.loads(make_bytes)
    require(make.get("schema") == 1 and make.get("image") == "docker-syncd-vs" and
            make.get("variant") == variant and make.get("architecture") == "amd64" and
            make.get("distribution") == "trixie" and make.get("features") == validate_payloads.FEATURES,
            "invalid syncd Make package manifest")
    retained = {item["package"]: item for item in make.get("packages", [])}
    require(retained and len(retained) == len(make["packages"]), "missing or duplicate Make packages")
    descriptor, _, _, layers = validate_image.image(base)
    files = {}
    for layer in layers:
        validate_image.apply_layer(layer, files)

    def inspect_payload(path):
        entries = {}
        validate_image.apply_layer(path, entries, checked_overlay=True)
        return entries

    provided, replacements = provided_packages(mapping_path, variant=variant, retained=retained)
    receipt = selection.validate(
        lock_path, mapping_path, group=variant, architecture="amd64",
        installed=selection.base_packages(layers, architecture="amd64"), base_files=files,
        retained_packages=retained, inspect_payload=inspect_payload,
        check_overlay=validate_image.assert_overlay_paths,
        provided_packages=provided)
    receipt["provided_package_replacements"] = replacements
    receipt["variant"] = receipt.pop("group")
    receipt["skipped_make"] = receipt.pop("skipped_retained")
    receipt.update(base_manifest_digest=descriptor["digest"],
                   make_manifest_sha256=hashlib.sha256(make_bytes).hexdigest())
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--retained-manifest", "--make-manifest", dest="make_manifest", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = select(args.base, args.lock, args.make_manifest, args.mapping, variant=args.variant)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd APT package validation failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
