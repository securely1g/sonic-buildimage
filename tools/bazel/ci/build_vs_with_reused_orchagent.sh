#!/usr/bin/env bash
# Build the fixed native VS profile and retain both runtime/debug image pairs.
set -euo pipefail

measure=false
if [[ ${1:-} == --measure-redundant-request && $# == 1 ]]; then
    measure=true
elif (( $# )); then
    echo 'Usage: build_vs_with_reused_orchagent.sh [--measure-redundant-request]' >&2
    exit 2
fi

mkdir -p artifacts/vs
options=(
    BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y
    ENABLE_ASAN=n ENABLE_SYNCD_RPC=n
    "SONIC_BUILDER_EXTRA_CMDLINE=--label ${SONIC_CI_CONTAINER_LABEL:?} -e BAZEL=${BAZEL:?} -e BAZEL_CONTAINER_ARGS"
)
receipt=artifacts/vs/orchagent-reuse.json
pairs=artifacts/vs/runtime-debug-pairs.json
revision=$(git rev-parse HEAD)
clock() { python3 -c 'import time; print(time.monotonic())'; }
build_start=$(clock)
make target/sonic-vs.img.gz "${options[@]}"
build_end=$(clock)
capture_start=$(clock)
# Fail immediately if Make did not produce the debug side outputs. Check the
# actual installer archive list before any follow-up builder can repair them.
python3 -B tools/bazel/ci/vs_archive_reuse.py capture-pairs --workspace "$PWD" \
    --receipt "$pairs" --orchagent-receipt "$receipt"
capture_end=$(clock)

# Opt-in calibration: execute the old redundant request against the very same
# revision, flags, worker and warmed caches. Verification rejects changed bytes.
# Ordinary PR builds never execute this request.
baseline_seconds=
if $measure; then
    baseline_start=$(clock)
    make target/docker-orchagent.gz "${options[@]}"
    baseline_end=$(clock)
    python3 -B tools/bazel/ci/vs_archive_reuse.py verify-runtime --workspace "$PWD" --receipt "$receipt"
    baseline_seconds=$(python3 - "$baseline_start" "$baseline_end" <<'PY'
import sys
print(float(sys.argv[2]) - float(sys.argv[1]))
PY
    )
fi

# Only legacy P4 packages and validation remain; do not request images again.
followup_start=$(clock)
make bazel-vs-oci-verify "${options[@]}"
followup_end=$(clock)
verify_start=$(clock)
python3 -B tools/bazel/ci/vs_archive_reuse.py verify-pairs --workspace "$PWD" \
    --receipt "$pairs" --orchagent-receipt "$receipt"
verify_end=$(clock)
python3 - "$capture_start" "$capture_end" "$verify_start" "$verify_end" "$baseline_seconds" \
    "$build_start" "$build_end" "$followup_start" "$followup_end" "$revision" <<'PY'
import json
from pathlib import Path
import subprocess
import sys

capture_start, capture_end, verify_start, verify_end = map(float, sys.argv[1:5])
validation_seconds = capture_end - capture_start + verify_end - verify_start
baseline_seconds = float(sys.argv[5]) if sys.argv[5] else None
report = {
    "schema": 1,
    "validation_seconds": validation_seconds,
    "redundant_request_seconds": baseline_seconds,
    "net_avoided_seconds": None if baseline_seconds is None else baseline_seconds - validation_seconds,
    "scope": "Same-run redundant request minus archive validation; not a paired full-job speedup.",
}
Path("artifacts/vs/orchagent-reuse-timing.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, sort_keys=True))
build_start, build_end, followup_start, followup_end = map(float, sys.argv[6:10])
revision = sys.argv[10]
if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != revision:
    raise SystemExit("Source revision changed during VS build")
timing = {
    "schema": 1,
    "revision": revision,
    "profile": {"architecture": "amd64", "platform": "vs", "bazel": True,
                "fips": True, "dash": True, "asan": False, "syncd_rpc": False,
                "install_debug_tools": False},
    "vs_build_seconds": build_end - build_start,
    "followup_seconds": followup_end - followup_start,
    "pair_validation_seconds": validation_seconds,
    "total_seconds_excluding_calibration": (build_end - build_start) +
        (followup_end - followup_start) + validation_seconds,
    "scope": "VS build plus required follow-up and pair validation, excluding later CI steps/uploads; compare with a separate baseline run.",
}
Path("artifacts/vs/runtime-debug-pairs-timing.json").write_text(json.dumps(timing, indent=2) + "\n")
print(json.dumps(timing, sort_keys=True))
PY
