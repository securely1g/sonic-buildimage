"""Require Common and SWSS to select one shared Serde/provider graph."""

import re
import sys
from pathlib import Path

COMMON_RUST_TARGET = "@@sonic-swss-common+//crates/swss-common:swss_common"


def verify(output):
    entries = [line.split("\t", 1) for line in output.splitlines()]
    if not entries or any(len(entry) != 2 for entry in entries):
        raise ValueError("configured dependency kinds and labels are required")
    labels = {label for kind, label in entries if kind == "rust_library"}
    if any("swss_crates" in label for label in labels):
        raise ValueError("legacy SWSS-owned Rust graph is still selected")
    common_matches = {
        label for label in labels
        if label == COMMON_RUST_TARGET or label.endswith("//crates/swss-common:swss_common")
    }
    if common_matches != {COMMON_RUST_TARGET}:
        raise ValueError("expected Common-owned Rust target " + COMMON_RUST_TARGET)
    selected = {"swss_common": COMMON_RUST_TARGET}
    for name, suffix in (("serde", "//:serde"), ("serde_core", "//:serde_core")):
        matches = {label for label in labels if label.endswith(suffix)}
        if len(matches) != 1:
            raise ValueError("expected one shared Rust target for " + name)
        selected[name] = matches.pop()
    hubs = []
    for name in ("serde", "serde_core"):
        match = re.fullmatch(r"(@@?[^/]+[+~]sonic_rust_crates)__" + name + r"-[^/]+//:" + name,
                             selected[name])
        if match is None:
            raise ValueError(name + " is not supplied by the shared SONiC Rust graph")
        hubs.append(match.group(1))
    if hubs[0] != hubs[1]:
        raise ValueError("Serde crates use different repository hubs")
    return selected


if __name__ == "__main__":
    verify(Path(sys.argv[1]).read_text())
