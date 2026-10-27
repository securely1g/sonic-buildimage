#!/bin/bash
# Run the existing P4 checks before leaving the archive builder's Bazel session.
set -euo pipefail

runtime_deb=$1
debug_deb=$2
mkdir -p artifacts/vs/syncd-p4-images

# Match the Syncd archive recipe's options, including the caller's cache and
# distdir settings. Changing output bases here would defeat in-session reuse.
option_lines=$(python3 tools/bazel/build_helpers.py options \
    --cache-directory="${BAZEL_CONTAINER_CACHE_DIR-${BAZEL_SWSS_CACHE_DIR:-}}" \
    --arguments="${BAZEL_CONTAINER_ARGS-${BAZEL_SWSS_ARGS:-}} --config=syncd-dash")
bazel_options=()
if test -n "$option_lines"; then mapfile -t bazel_options <<< "$option_lines"; fi
bazel_options+=(--platforms=@sonic_build_infra//platforms:x86_64_trixie
    --host_platform=@sonic_build_infra//platforms:x86_64_trixie)

test_status=0
"${BAZEL:-bazel}" test "${bazel_options[@]}" //tools/bazel/tests:syncd_p4_images_test \
    --nocache_test_results --nozip_undeclared_test_outputs \
    2>&1 | tee artifacts/vs/syncd-p4-images.log || test_status=$?
for file in test.log test.xml test.outputs/validation.json; do
    path="bazel-testlogs/tools/bazel/tests/syncd_p4_images_test/$file"
    if test -f "$path"; then
        cp "$path" "artifacts/vs/syncd-p4-images/$(basename "$file")"
    fi
done
if test "$test_status" -ne 0; then exit "$test_status"; fi

python3 -B tools/bazel/ci/verify_p4rt_debug.py \
    --runtime-deb "$runtime_deb" --debug-deb "$debug_deb" \
    --source-root src/sonic-p4rt/sonic-pins \
    --output artifacts/vs/p4rt-debug-verification.json \
    2>&1 | tee artifacts/vs/p4rt-debug-verification.log
