#!/usr/bin/env python3
"""Observe six fixed original host siblings using the unchanged package reader."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.bazel.ci import observe_vs_host_binaries as host
from tools.bazel.ci import recover_vs_original_inputs as original

MANIFEST_SHA256 = "845a3de37a00fff7f599b2de42be79540250c83c7f14265fea0b718a85e5ae3c"
require = original.require


def load_manifest(path):
    with original.HeldFile(path) as held:
        require(0 < held.initial.st_size <= 65536, "Fixed sibling manifest exceeds bound")
        raw = os.pread(held.fd, held.initial.st_size, 0)
        held.verify()
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256, "Fixed sibling manifest changed")
    value = json.loads(raw)
    require(len(value["inputs"]) == 6 and len(value["required_names"]) == 3,
            "Expected only the six original sibling variants")
    return value


def observe(value, artifact, scratch):
    report = {"status": "ORIGINAL_HOST_SIBLING_OBSERVATION_INCOMPLETE", "results": [],
              "package_generation": False, "package_installation": False,
              "package_scripts_executed": False, "source_admission": False,
              "VS_assembly_admission": False}
    budget = host.MetadataBudget()
    def inspect_and_reserve(path, row, directory):
        observed = host.inspect_path(path, row, directory)
        budget.reserve(observed)
        return observed
    grouped = collections.defaultdict(list)
    for row in value["inputs"]:
        grouped[row["cache_name"]].append(row)
    active = "manifest"
    try:
        for name, rows in sorted(grouped.items()):
            active = name
            report["results"].extend(host.inspect_cached(artifact / "execution" / name,
                rows, scratch, observer=inspect_and_reserve))
        require(len(report["results"]) == 6 and
                {r["name"] for r in report["results"]} == set(value["required_names"]),
                "Incomplete original sibling observation")
        classes = collections.defaultdict(lambda: collections.defaultdict(list))
        for row in report["results"]:
            classes[row["name"]][row["semantic_identity"]].append(row["sha256"])
        report.update(status="SIX_ORIGINAL_HOST_SIBLINGS_OBSERVED_NOT_ADMITTED", equivalence_classes=classes)
        code = 0
    except (OSError, ValueError, EOFError, tarfile.TarError, subprocess.SubprocessError, KeyError, TypeError) as error:
        site = traceback.extract_tb(error.__traceback__)[-1]
        report["failure"] = {"fixed_input": active, "error_type": type(error).__name__,
            "site": {"file": Path(site.filename).name, "line": site.lineno, "function": site.name}}
        if isinstance(error, original.RecoveryError):
            report["failure"]["reason"] = str(error)[:256]
        code = 1
    return report, code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    def deadline(*_):
        raise TimeoutError("Fixed sibling metadata deadline")
    previous = {n: signal.signal(n, deadline) for n in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
    signal.alarm(600)
    try:
        value = load_manifest(args.manifest)
        report, code = observe(value, args.artifact, args.scratch)
        encoded = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
        require(len(encoded) <= host.MAX_REPORT, "Sibling metadata report exceeds bound")
        with args.report.open("xb") as stream:
            stream.write(encoded)
        return code
    finally:
        signal.alarm(0)
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
