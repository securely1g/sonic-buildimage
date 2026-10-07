#!/usr/bin/env python3
"""Retain base and Make packages when assembling syncd's locked APT layers."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
sys.path.insert(0, str(Path(__file__).absolute().parent))
from tools.bazel.ci.artifact_validation import require, sha
from sonic_apt import selection
import validate_image
import validate_payloads


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

    selected, receipt = selection.select(
        lock_path, mapping_path, group=variant, architecture="amd64",
        installed=selection.base_packages(layers, architecture="amd64"), base_files=files,
        retained_packages=retained, inspect_payload=inspect_payload,
        check_overlay=validate_image.assert_overlay_paths)
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
    parser.add_argument("--out-manifest", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        selected, receipt = select(args.base, args.lock, args.make_manifest, args.mapping, variant=args.variant)
        args.out_manifest.write_text("".join(str(path) + "\n" for path in selected))
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "syncd APT payload selection failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
