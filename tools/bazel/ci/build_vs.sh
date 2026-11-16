#!/usr/bin/env bash
# Build the native VS ONIE installer and both runtime/debug container pairs.
set -euo pipefail

if (( $# )); then
    echo 'Usage: build_vs.sh' >&2
    exit 2
fi

builder_args="--label ${SONIC_CI_CONTAINER_LABEL:?} -e BAZEL=${BAZEL:?} -e BAZEL_CONTAINER_ARGS"
if [[ ${SONIC_VS_IMAGE_TIMINGS:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_IMAGE_TIMINGS"
fi
if [[ ${SONIC_VS_REUSE_BAZEL_PRODUCERS:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_REUSE_BAZEL_PRODUCERS"
fi
if [[ ${SONIC_VS_GROUP_BAZEL_IMAGES:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_GROUP_BAZEL_IMAGES"
fi
options=(
    BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y
    ENABLE_ASAN=n ENABLE_SYNCD_RPC=n
    "SONIC_BUILDER_EXTRA_CMDLINE=$builder_args"
)
revision=$(git rev-parse HEAD)
mkdir -p artifacts/vs
printf '%s\n' "$revision" > artifacts/vs/build-revision.txt
if [[ ${SONIC_VS_PREWARM_BAZEL_REPOSITORIES:-} == 1 ]]; then
    make -f Makefile.work BLDENV=trixie sonic-slave-run "${options[@]}" \
        SONIC_RUN_CMDS='bash tools/bazel/ci/prewarm_vs_repositories.sh'
fi
make target/sonic-vs.bin "${options[@]}"
