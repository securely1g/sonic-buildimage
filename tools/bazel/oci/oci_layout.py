#!/usr/bin/env python3
"""Validate a Make-produced OCI layout without rewriting image contents."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import NamedTuple


class OciLayout(NamedTuple):
    """Validated image metadata and its hash-checked layer paths."""
    descriptor: dict
    manifest: dict
    config: dict
    layers: tuple[Path, ...]


def validate_layout(directory: Path, expected_platform: str) -> OciLayout:
    """Require one complete image and return its validated metadata and layers."""
    try:
        return _validate_layout(directory, expected_platform)
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError("invalid OCI layout metadata structure") from error


def _validate_layout(directory: Path, expected_platform: str) -> OciLayout:
    if len(expected_platform.split("/")) != 2 or not all(expected_platform.split("/")):
        raise ValueError("expected platform must be os/architecture")
    marker = json.loads((directory / "oci-layout").read_bytes())
    if marker.get("imageLayoutVersion") != "1.0.0":
        raise ValueError("unsupported OCI image layout version")
    index = json.loads((directory / "index.json").read_bytes())
    if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
        raise ValueError("expected a single OCI image for " + expected_platform)

    def blob(descriptor):
        digest = descriptor.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid OCI SHA256 blob reference")
        path = directory / "blobs" / "sha256" / digest[7:]
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
                size += len(chunk)
        if hasher.hexdigest() != digest[7:] or size != descriptor.get("size"):
            raise ValueError("OCI blob digest or size mismatch: " + digest)
        return path

    descriptor = index["manifests"][0]
    manifest = json.loads(blob(descriptor).read_bytes())
    if manifest.get("schemaVersion") != 2:
        raise ValueError("unsupported OCI manifest schema")
    config = json.loads(blob(manifest["config"]).read_bytes())
    platforms = [config]
    if "platform" in descriptor:
        platforms.append(descriptor["platform"])
    for platform in platforms:
        actual = platform.get("os", "") + "/" + platform.get("architecture", "")
        if actual != expected_platform:
            raise ValueError(f"OCI image platform {actual!r} does not match {expected_platform!r}")
    layers = manifest["layers"]
    rootfs = config.get("rootfs", {})
    if rootfs.get("type") != "layers" or len(rootfs.get("diff_ids", [])) != len(layers):
        raise ValueError("OCI layer count does not match image rootfs")
    return OciLayout(descriptor, manifest, config, tuple(blob(layer) for layer in layers))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", required=True, type=Path)
    parser.add_argument("--expected-platform", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    validate_layout(args.marker.parent, args.expected_platform)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # The checked marker makes validation a required input to the directory
    # action; index, manifest, config and layer bytes are never rewritten.
    args.out.write_bytes(args.marker.read_bytes())


if __name__ == "__main__":
    main()
