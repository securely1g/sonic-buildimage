#!/usr/bin/env bash
# Validate one source package family in the prepared native installer worker.
set -euo pipefail

if (( $# != 1 )) || [[ $1 != swss && $1 != syncd ]]; then
    echo 'Usage: validate_vs_source.sh swss|syncd' >&2
    exit 2
fi
family=$1

# The installer preparation owns this worker's private daemon. Keep validation
# in the same environment and fail before Bazel when it is unavailable.
docker info > /dev/null
option_lines=$(python3 tools/bazel/build_helpers.py options \
    --arguments="${BAZEL_CONTAINER_ARGS-${BAZEL_SWSS_ARGS:-}}")
ci_options=()
if test -n "$option_lines"; then
    while IFS= read -r option; do ci_options+=("--bazel-arg=$option"); done <<< "$option_lines"
fi
native_runner="$PWD/tools/bazel/oci/native_build.py"

if [[ $family == swss ]]; then
    python3 -B tools/bazel/ci/container.py --reuse-build \
        --container //dockers/docker-orchagent --source-file rules/docker-orchagent.mk \
        --source-file tools/bazel/oci/native_build.py \
        --source-file tools/bazel/ci/validate_vs_source.sh \
        --bazel "$native_runner" --artifacts artifacts/swss \
        --bazel-arg=--platforms=@sonic_build_infra//platforms:x86_64_trixie \
        --bazel-arg=--host_platform=@sonic_build_infra//platforms:x86_64_trixie \
        "${ci_options[@]}"
else
    mkdir -p artifacts/syncd-vs
    git rev-parse HEAD > artifacts/syncd-vs/revision.txt
    sha256sum platform/vs/docker-syncd-vs/bazel/apt.lock.json \
        platform/vs/docker-syncd-vs/BUILD.bazel > artifacts/syncd-vs/input-locks.sha256
    python3 -B tools/bazel/ci/container.py --reuse-build \
        --container //platform/vs/docker-syncd-vs --source-file platform/vs/docker-syncd-vs.mk \
        --source-file tools/bazel/oci/native_build.py \
        --source-file tools/bazel/ci/validate_vs_source.sh \
        --bazel "$native_runner" --artifacts artifacts/syncd-vs/bazel \
        --bazel-arg=--platforms=@sonic_build_infra//platforms:x86_64_trixie \
        --bazel-arg=--host_platform=@sonic_build_infra//platforms:x86_64_trixie \
        --bazel-arg=--config=syncd-dash "${ci_options[@]}"
    python3 -B tools/bazel/ci/bazel_commands.py \
        --bazel "$native_runner" --bazel-arg=--config=syncd-dash "${ci_options[@]}" \
        --output artifacts/syncd-vs/p4-image-test-analysis.json \
        //tools/bazel/tests:syncd_p4_images_test
fi
