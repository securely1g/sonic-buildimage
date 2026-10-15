#!/usr/bin/env python3
"""Capture reviewed Debian lifecycle metadata from existing Make packages.

This preparation tool calls dpkg-deb only to extract existing control members.
It never builds a Debian package or installs/runs a package script. Ordinary
Bazel builds consume the checked-in result and do not invoke this program.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


CONTROL_MEMBERS = {
    "control", "conffiles", "preinst", "postinst", "prerm", "postrm",
    "config", "templates", "triggers", "shlibs", "symbols",
}
DERIVED_MEMBERS = {"md5sums"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def control_fields(data):
    fields = {}
    key = None
    for line in data.decode().splitlines():
        if line[:1].isspace():
            if key is None:
                raise ValueError("Control continuation without a field")
            fields[key] += "\n" + line
        elif line:
            key, separator, value = line.partition(":")
            if not separator or key in fields:
                raise ValueError("Invalid or repeated control field")
            fields[key] = value.lstrip()
    return fields


def capture(deb_paths, output):
    manifest = {
        "_generated": {
            "notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
            "generator": "tools/bazel/dpkg/source_controls/capture.py",
            "description": "Original control member bytes extracted from reviewed existing Debian packages."
        },
        "schema": 1,
        "packages": {},
    }
    output.mkdir(parents=True, exist_ok=True)
    for deb in sorted(deb_paths):
        before = digest(deb)
        with tempfile.TemporaryDirectory(prefix="sonic-source-controls-") as temporary:
            directory = Path(temporary) / "control"
            subprocess.run(["dpkg-deb", "--control", str(deb), str(directory)], check=True)
            fields = control_fields((directory / "control").read_bytes())
            name = fields["Package"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
                raise ValueError("Invalid package name")
            if name in manifest["packages"]:
                raise ValueError("More than one reference package: " + name)
            names = {path.name for path in directory.iterdir()}
            if names - CONTROL_MEMBERS - DERIVED_MEMBERS:
                raise ValueError("Unreviewed control members: " + str(names - CONTROL_MEMBERS - DERIVED_MEMBERS))
            record = {
                "version": fields["Version"],
                "reference_architecture": fields["Architecture"],
                "reference": {"kind": "deb", "filename": deb.name, "sha256": before},
                "control_files": {},
            }
            for member in sorted(names & CONTROL_MEMBERS):
                source = directory / member
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Nonregular control member: " + str(source))
                destination = output / name / member
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                mode = source.stat().st_mode & 0o777
                destination.chmod(mode)
                record["control_files"][member] = {
                    "path": name + "/" + member,
                    "sha256": digest(destination),
                    "mode": mode,
                }
            if name == "python3-libyang":
                # dh_python3 adds a native package qualifier, not a filesystem
                # path or architecture-dependent program. Keep original bytes
                # and declare the exact two reviewed substitutions separately.
                record["architecture_substitutions"] = {
                    member: [{"from": "python3-libyang:amd64", "to": "python3-libyang:{architecture}"}]
                    for member in ("postinst", "prerm")
                }
                for member in record["architecture_substitutions"]:
                    if "python3-libyang:amd64" not in (directory / member).read_text():
                        raise ValueError("Review changed Python lifecycle metadata")
            if name == "libprotobuf32t64":
                if fields["Version"] != "3.21.12-11+deb13u1":
                    raise ValueError("Review changed Protobuf lifecycle metadata")
                record.update({
                    "source_version": "3.21.12+sonic.1",
                    "control_field_substitutions": {
                        "Breaks": [{
                            "from": "libprotobuf32 (<< 3.21.12-11+deb13u1)",
                            "to": "libprotobuf32 (<< 3.21.12+sonic.1)",
                        }],
                        "Provides": [{
                            "from": "libprotobuf32 (= 3.21.12-11+deb13u1)",
                            "to": "libprotobuf32 (= 3.21.12+sonic.1)",
                        }],
                    },
                    "source_version_reason": (
                        "The owner builds upstream v21.12 with a Bazel integration patch. "
                        "Borrowing Debian lifecycle metadata does not imply that Debian "
                        "revision 11+deb13u1 source changes are present."
                    ),
                    "source_reference": {
                        "module": "protobuf-legacy",
                        "version": "3.21.12-sonic.1",
                        "url": "https://github.com/protocolbuffers/protobuf/archive/refs/tags/v21.12.tar.gz",
                        "integrity": "sha256-Iv2vZBsxZV1LIpf5mB+lIDsoZvgzLTxjM/awEHuzIN4=",
                        "registry_patch": "0001-bazel8-shared-runtime.patch",
                        "registry_patch_integrity": "sha256-PGeLgmerYUc0j/EG/rnqMy3XYy+4UGNLBcFYe7668fM=",
                    },
                })
            manifest["packages"][name] = record
        if digest(deb) != before:
            raise ValueError("Reference package changed during extraction")
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--contract", type=Path, action="append", default=[])
    parser.add_argument("--extra-deb", type=Path, action="append", default=[])
    args = parser.parse_args()
    selected = set(args.extra_deb)
    for contract in args.contract:
        packages = json.loads(contract.read_text())["packages"]
        records = packages.values() if isinstance(packages, dict) else packages
        for record in records:
            fields = record["control_fields"]
            pattern = fields["Package"] + "_" + fields["Version"].split(":", 1)[-1] + "_*.deb"
            matches = list(args.deb_dir.glob(pattern))
            if len(matches) != 1:
                raise ValueError("Expected one reviewed reference package for " + pattern)
            selected.add(matches[0])
    if not selected:
        parser.error("Select source package contracts or explicit existing packages")
    capture(selected, args.output)


if __name__ == "__main__":
    main()
