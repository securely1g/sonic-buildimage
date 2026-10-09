"""Shared OCI inspection and CLI for container-owned APT selection policies."""

import argparse
import json
from pathlib import Path
import tarfile

from sonic_apt import selection
from tools.bazel.ci.artifact_validation import require
from tools.bazel.oci.oci_inventory import apply_layer, assert_overlay_paths
from tools.bazel.oci.oci_layout import validate_layout


def select(base, lock, mapping, *, variant, architecture, retained_packages,
           base_package_metadata=None, retained_replacements=None):
    """Select locked additions against a checked OCI base and owner-provided state.

    The container validates its policy or Make manifest before calling this
    function. It also owns any permission to replace an inherited package and
    any additional receipt fields required by its image checks.
    """
    require(variant in ("runtime", "debug"), "unsupported APT variant")
    layout = validate_layout(base, "linux/" + architecture)
    files = {}
    for layer in layout.layers:
        apply_layer(layer, files)

    def inspect_payload(path):
        entries = {}
        apply_layer(path, entries, checked_overlay=True)
        return entries

    # The ordinary selection API predates explicit inherited replacements.
    # Only owners that authorize replacements need the newer infrastructure API.
    replacement_args = {}
    if retained_replacements is not None:
        replacement_args["retained_replacements"] = retained_replacements
    selected, receipt = selection.select(
        lock, mapping, group=variant, architecture=architecture,
        installed=selection.base_packages(layout.layers, architecture=architecture),
        base_files=files, retained_packages=retained_packages,
        inspect_payload=inspect_payload, check_overlay=assert_overlay_paths,
        base_package_metadata=base_package_metadata, **replacement_args)
    receipt["base_manifest_digest"] = layout.descriptor["digest"]
    return selected, receipt


def main(selector, *, description, error_prefix):
    """Run an owner's policy adapter with the shared apt_layer command contract."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--retained-manifest", "--make-manifest", dest="retained_manifest",
                        required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--base-package-metadata", type=Path)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        selected, receipt = selector(
            args.base, args.lock, args.retained_manifest, args.mapping,
            variant=args.variant, base_package_metadata=args.base_package_metadata)
        selection.stage_payloads(selected, args.out_dir)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, error_prefix + ": " + str(error) + "\n")
