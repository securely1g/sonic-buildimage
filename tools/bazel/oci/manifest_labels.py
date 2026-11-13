#!/usr/bin/env python3
"""Convert a Make-generated SONiC manifest into an OCI image label."""

import argparse
import json
from pathlib import Path


def reject_constant(value):
    raise ValueError(f"Container manifest contains a non-JSON number: {value}")


def manifest_label(source):
    """Preserve the JSON object as one line of OCI label-file content."""
    manifest = json.loads(source, parse_constant=reject_constant)
    if not isinstance(manifest, dict):
        raise ValueError("Container manifest must be a JSON object")
    return "com.azure.sonic.manifest=" + json.dumps(manifest, separators=(",", ":"), allow_nan=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Read and validate before opening the output, including when it exists.
    label = manifest_label(args.manifest.read_text(encoding="utf-8"))
    args.output.write_text(label, encoding="utf-8")


if __name__ == "__main__":
    main()
