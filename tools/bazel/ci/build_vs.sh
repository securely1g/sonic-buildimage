#!/usr/bin/env bash
# Build the native VS ONIE installer and both runtime/debug container pairs.
set -euo pipefail

if (( $# )); then
    echo 'Usage: build_vs.sh' >&2
    exit 2
fi

options=(
    BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y
    ENABLE_ASAN=n ENABLE_SYNCD_RPC=n
    "SONIC_BUILDER_EXTRA_CMDLINE=--label ${SONIC_CI_CONTAINER_LABEL:?} -e BAZEL=${BAZEL:?} -e BAZEL_CONTAINER_ARGS"
)
revision=$(git rev-parse HEAD)
mkdir -p artifacts/vs
printf '%s\n' "$revision" > artifacts/vs/build-revision.txt
make target/sonic-vs.bin "${options[@]}"
