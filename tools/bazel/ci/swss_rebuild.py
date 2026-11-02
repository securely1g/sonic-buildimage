#!/usr/bin/env python3
"""Probe an installed SWSS source-file edit through its declared repository patch."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.artifact_validation import require, sha

PROBE = b'''\ndiff --git a/fpmsyncd/bgp_eoiu_marker.py b/fpmsyncd/bgp_eoiu_marker.py
--- a/fpmsyncd/bgp_eoiu_marker.py
+++ b/fpmsyncd/bgp_eoiu_marker.py
@@ -1,2 +1,3 @@
 #!/usr/bin/env python
+# SWSS source-TAR invalidation probe; removed after this check.
 
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", required=True)
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    patch = ROOT / "tools/bazel/0001-sonic-swss-libteam.patch"
    original = patch.read_bytes()
    targets = ["@sonic_swss//dist:swss_pkg", "@sonic_swss//dist:swss_pkg.debug_symbols"]
    records = []

    def build(phase):
        start = time.monotonic()
        subprocess.run([args.bazel, "build", *args.bazel_arg, "--jobs=2",
                        "--build_event_json_file=" + str(args.output / (phase + "-events.json")), *targets], check=True)
        digests = {}
        for target in targets:
            result = subprocess.run([args.bazel, "cquery", *args.bazel_arg, target, "--output=files"],
                                    capture_output=True, text=True, check=True)
            paths = [Path(line) for line in result.stdout.splitlines() if line]
            require(len(paths) == 1 and paths[0].is_file(), "missing SWSS archive: " + target)
            digests[target] = sha(paths[0])
        records.append({"phase": phase, "seconds": round(time.monotonic() - start, 3),
                        "outputs_sha256": digests, "patch_sha256": sha(patch)})
        return digests

    try:
        baseline = build("baseline")
        require(build("no-op") == baseline, "unchanged inputs changed a SWSS TAR")
        patch.write_bytes(original + PROBE)
        changed = build("source-edit")
        require(changed[targets[0]] != baseline[targets[0]] and changed[targets[1]] == baseline[targets[1]],
                "an installed Python edit must change runtime TAR only, keeping compiled symbols")
        patch.write_bytes(original)
        require(build("restored") == baseline, "restored inputs did not reproduce both SWSS TARs")
    finally:
        patch.write_bytes(original)
        (args.output / "rebuild.json").write_text(json.dumps({
            "scope": "Same-output-base no-op and installed-source invalidation, not a cold-cache or C++ recompilation claim",
            "source_restored": patch.read_bytes() == original, "builds": records,
        }, indent=2) + "\n")


if __name__ == "__main__":
    main()
