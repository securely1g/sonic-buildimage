#!/usr/bin/env python3
"""Select orchagent APT files while preserving its base and exact runtime image."""

import argparse
import json
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
from sonic_apt import selection
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci.oci_inventory import apply_layer, assert_overlay_paths
from tools.bazel.oci.oci_layout import validate_layout


def select(base, lock, policy_path, mapping, *, variant):
    require(variant in ("runtime", "debug"), "unsupported orchagent APT variant")
    policy = json.loads(policy_path.read_bytes())
    # Orchagent adds its native source-built tar targets after the APT layer.
    # It has no Make-produced DEB handoff to retain by Debian package name.
    require(policy == {"schema": 1, "image": "docker-orchagent", "architecture": "amd64",
                       "distribution": "trixie", "retained_packages": []},
            "invalid orchagent APT policy")
    layout = validate_layout(base, "linux/amd64")
    files = {}
    for layer in layout.layers:
        apply_layer(layer, files)

    def inspect_payload(path):
        entries = {}
        apply_layer(path, entries, checked_overlay=True)
        return entries

    selected, receipt = selection.select(
        lock, mapping, group=variant, architecture="amd64",
        installed=selection.base_packages(layout.layers, architecture="amd64"),
        base_files=files, retained_packages={}, inspect_payload=inspect_payload,
        check_overlay=assert_overlay_paths)
    receipt.update(image="docker-orchagent", base_manifest_digest=layout.descriptor["digest"],
                   policy_sha256=sha(policy_path))
    return selected, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--retained-manifest", dest="policy", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--variant", required=True, choices=("runtime", "debug"))
    parser.add_argument("--out-manifest", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        selected, receipt = select(args.base, args.lock, args.policy, args.mapping, variant=args.variant)
        args.out_manifest.write_text("".join(str(path) + "\n" for path in selected))
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError) as error:
        parser.exit(1, "orchagent APT selection failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
