#!/usr/bin/env python3
"""Check the actual listener DEB against its source-built payload and control."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci.artifact_validation import command, payload, require, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--architecture", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="listener-deb-") as directory:
        root = Path(directory)
        control = root / "control"
        command(["dpkg-deb", "--control", str(args.deb), str(control)])
        require({entry.name for entry in control.iterdir()} == {"control", "md5sums"},
                "listener unexpectedly has maintainer scripts or extra control files")
        require((control / "control").read_bytes() == args.control.read_bytes(),
                "packaged control differs from source-generated control")
        data = root / "data.tar"
        with data.open("wb") as stream:
            subprocess.run(["dpkg-deb", "--fsys-tarfile", str(args.deb)], stdout=stream, check=True)
        extracted = root / "payload"
        command(["dpkg-deb", "--extract", str(args.deb), str(extracted)])
        command(["md5sum", "--check", str(control / "md5sums")], cwd=extracted)
        expected, actual = payload(args.data), payload(data)
        require(actual == expected, "DEB payload differs from its source-built data TAR")
        binary = actual["usr/bin/supervisor-proc-exit-listener-rs"]
        require(binary["elf_machine"] == {"amd64": 62, "arm64": 183}[args.architecture],
                "listener DEB contains the wrong ELF architecture")
        fields = command(["dpkg-deb", "--field", str(args.deb), "Package", "Version", "Architecture", "Depends"])
        require("Package: sonic-supervisord-utilities-rs\n" in fields, "wrong package name")
        require("Version: 1.0.0\n" in fields, "wrong package version")
        require("Architecture: " + args.architecture + "\n" in fields, "wrong package architecture")
    args.output.write_text(json.dumps({
        "_generated": {"notice": "AUTO-GENERATED. DO NOT EDIT MANUALLY.",
                       "generator": "tools/bazel/ci/check_listener_deb.py"},
        "deb_sha256": sha(args.deb), "architecture": args.architecture,
        "control": fields, "payload": actual,
        "checks": ["generated control", "complete payload", "ownership and modes", "md5sums", "native ELF"],
        "installed_package_check": "separate integration with source-built Common DEB",
    }, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
