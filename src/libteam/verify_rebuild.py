#!/usr/bin/env python3
"""Probe declared libteam config invalidation and restore its exact source."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", required=True, help="Existing Bazel entry point")
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    targets = ["@libteam//:" + name for name in (
        "libteam_runtime", "libteam_runtime.debug_symbols",
        "libteamdctl_runtime", "libteamdctl_runtime.debug_symbols",
        "libteam_utils_runtime", "libteam_utils_runtime.debug_symbols",
        "libteam_dev", "runtime_packages.json")]
    config = Path(__file__).parent / "config.h.in"
    original = config.read_bytes()
    needle = b'#define PACKAGE_VERSION "@PACKAGE_VERSION@"'
    if original.count(needle) != 1:
        raise ValueError("config probe requires the reviewed PACKAGE_VERSION input")
    records = []

    def build(phase):
        start = time.monotonic()
        subprocess.run([args.bazel, "build", *args.bazel_arg, "--jobs=2",
                        "--build_event_json_file=" + str(args.output / (phase + "-events.json")), *targets], check=True)
        result = subprocess.run([args.bazel, "cquery", *args.bazel_arg, "set(" + " ".join(targets) + ")", "--output=files"],
                                capture_output=True, text=True, check=True)
        outputs = [Path(line) for line in result.stdout.splitlines() if line.strip()]
        if len(outputs) != len(targets) or not all(path.is_file() for path in outputs):
            raise ValueError("expected the eight declared libteam archive/metadata outputs")
        digests = {str(path): sha(path) for path in outputs}
        records.append({"phase": phase, "seconds": round(time.monotonic() - start, 3),
                        "outputs_sha256": digests, "config_sha256": sha(config)})
        return digests

    baseline = build("baseline")
    if build("no-op") != baseline:
        raise ValueError("unchanged inputs changed the runtime TAR")
    try:
        config.write_bytes(original.replace(needle, b'#define PACKAGE_VERSION "1.31-input-probe"'))
        changed = build("config-edit")
        utils = [path for path in baseline if path.endswith("libteam_utils_runtime_rttar.tar")]
        if len(utils) != 1 or changed[utils[0]] == baseline[utils[0]]:
            raise ValueError("declared config edit did not invalidate the runtime TAR")
    finally:
        config.write_bytes(original)
    if build("restored") != baseline:
        raise ValueError("restored source did not reproduce the original runtime TAR")
    (args.output / "rebuild.json").write_text(json.dumps({
        "scope": "Same-output-base no-op and config invalidation; not a fresh-cache or remote-hit claim",
        "source_restored": config.read_bytes() == original, "builds": records,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
