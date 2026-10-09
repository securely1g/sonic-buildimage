#!/usr/bin/env python3
"""Orchagent policy adapter for shared OCI APT payload selection."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).absolute().parents[3]))
from tools.bazel.ci.artifact_validation import require, sha
from tools.bazel.oci import apt_selection


def select(base, lock, policy_path, mapping, *, variant, base_package_metadata=None):
    require(variant in ("runtime", "debug"), "unsupported orchagent APT variant")
    policy = json.loads(policy_path.read_bytes())
    # Orchagent adds its native source-built tar targets after the APT layer.
    # It has no Make-produced DEB handoff to retain by Debian package name.
    require(policy == {"schema": 1, "image": "docker-orchagent", "architecture": "amd64",
                       "distribution": "trixie", "retained_packages": []},
            "invalid orchagent APT policy")
    selected, receipt = apt_selection.select(
        base, lock, mapping, variant=variant, architecture="amd64",
        retained_packages={}, base_package_metadata=base_package_metadata)
    receipt.update(image="docker-orchagent", policy_sha256=sha(policy_path))
    return selected, receipt


def main():
    apt_selection.main(select, description=__doc__, error_prefix="orchagent APT selection failed")


if __name__ == "__main__":
    main()
