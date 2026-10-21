"""Pass only the checked SONiC APT selection to rules_distroless installation.

The shared installer consumes actual Debian archives. SONiC's existing selector
decides which locked packages are needed without replacing satisfied base
packages. This adapter checks the original archive hashes recorded by that
selector and writes the installer's generic list of declared archive paths.
"""

import argparse
import hashlib
import json
from pathlib import Path


def selected_archives(selection, packages, additional_packages=()):
    """Return the exact selected archives, rejecting undeclared or changed inputs."""
    selected = selection.get("selected")
    if not isinstance(selected, list):
        raise ValueError("APT selection receipt must contain a selected list")
    result = []
    keys = set()
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError("invalid selected package record")
        key = item.get("key")
        if not isinstance(key, str) or key not in packages:
            raise ValueError("selected package has no declared Debian archive: " + str(key))
        if key in keys:
            raise ValueError("duplicate selected package: " + key)
        keys.add(key)
        archive = packages[key]
        if not isinstance(archive, str) or not archive:
            raise ValueError("invalid Debian archive path for " + key)
        with Path(archive).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item.get("source_sha256"):
            raise ValueError("selected Debian archive checksum differs from the lock: " + key)
        result.append(archive)
    for archive in additional_packages:
        if not isinstance(archive, str) or not archive or not Path(archive).is_file():
            raise ValueError("invalid additional Debian archive: " + str(archive))
        result.append(archive)
    if len(set(result)) != len(result):
        raise ValueError("selected packages refer to the same Debian archive")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--packages", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--additional-package", action="append", default=[])
    args = parser.parse_args()
    packages = json.loads(args.packages.read_text())
    if not isinstance(packages, dict):
        parser.error("package mapping must be an object")
    archives = selected_archives(json.loads(args.selection.read_text()), packages, args.additional_package)
    # This build output is generated. Do not edit it: change the checked APT
    # lock or selector inputs instead. The upstream schema is a JSON array.
    args.output.write_text(json.dumps(archives, indent=2) + "\n")


if __name__ == "__main__":
    main()
