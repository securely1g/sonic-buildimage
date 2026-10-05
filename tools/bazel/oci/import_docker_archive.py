#!/usr/bin/env python3
"""Import with rules_oci's regctl and enforce the consumer's image platform."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def validate_platform(layout: Path, expected: str) -> None:
    """Check both the image config and any platform declared in the index."""
    def read_blob(descriptor):
        digest = descriptor["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid OCI SHA256 blob reference")
        return json.loads((layout / "blobs" / "sha256" / digest[7:]).read_bytes())

    index = json.loads((layout / "index.json").read_bytes())
    manifests = index["manifests"]
    if len(manifests) != 1:
        raise ValueError("expected a single imported image for " + expected)
    descriptor = manifests[0]
    config = read_blob(read_blob(descriptor)["config"])
    platforms = [config]
    if "platform" in descriptor:
        platforms.append(descriptor["platform"])
    for platform in platforms:
        actual = platform.get("os", "") + "/" + platform.get("architecture", "")
        if actual != expected:
            raise ValueError(f"imported image platform {actual!r} does not match {expected!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regctl", required=True, type=Path)
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--expected-platform", help="require a single image for os/architecture")
    args = parser.parse_args()
    if args.expected_platform:
        parts = args.expected_platform.split("/")
        if len(parts) != 2 or not all(parts):
            parser.error("expected platform must be os/architecture")

    # A fixed tag makes the imported metadata independent of the output path.
    # Resolve the executable because a relative single filename invokes PATH search.
    subprocess.run([
        str(args.regctl.resolve()), "image", "import",
        "ocidir://" + str(args.out) + ":base", str(args.src),
    ], check=True)
    if args.expected_platform:
        validate_platform(args.out, args.expected_platform)


if __name__ == "__main__":
    main()
