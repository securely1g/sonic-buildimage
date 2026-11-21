#!/usr/bin/env bash
# Build the native VS ONIE installer with the selected container archive variants.
set -euo pipefail

if (( $# )); then
    echo 'Usage: build_vs.sh' >&2
    exit 2
fi

builder_args="--label ${SONIC_CI_CONTAINER_LABEL:?} -e BAZEL=${BAZEL:?} -e BAZEL_CONTAINER_ARGS"
if [[ ${SONIC_VS_VALIDATION_BUNDLES:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_VALIDATION_BUNDLES"
fi
if [[ ${SONIC_VS_IMAGE_TIMINGS:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_IMAGE_TIMINGS"
    if [[ ${DOCKER_LOAD_ON_BOOT:-n} == y ]]; then
        builder_args+=" -e SONIC_BOOT_BUNDLE_EVIDENCE_DIR=artifacts/vs/image-timings/boot-bundle"
    fi
fi
if [[ ${SONIC_VS_REUSE_BAZEL_PRODUCERS:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_REUSE_BAZEL_PRODUCERS"
fi
if [[ ${SONIC_VS_RETRY_GO_FETCH:-} == 1 ]]; then
    builder_args+=" -e SONIC_VS_RETRY_GO_FETCH"
fi
unset SONIC_VS_RFS_HANDOFF_SESSION
if [[ ${SONIC_VS_RFS_HANDOFF:-} == 1 ]]; then
    export SONIC_VS_RFS_HANDOFF_SESSION
    SONIC_VS_RFS_HANDOFF_SESSION=$(mktemp -d .vs-rootfs-handoff.XXXXXXXX)
    builder_args+=" -e SONIC_VS_RFS_HANDOFF_SESSION"
    cleanup_rootfs_handoff() {
        local status=$?
        trap - EXIT
        if ! sudo python3 scripts/installer_rootfs_handoff.py cleanup \
            --session "$SONIC_VS_RFS_HANDOFF_SESSION"; then
            echo 'ERROR: failed to clean the private rootfs handoff directory' >&2
            if (( status == 0 )); then status=1; fi
        fi
        exit "$status"
    }
    trap cleanup_rootfs_handoff EXIT
fi
options=(
    BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y
    ENABLE_ASAN=n ENABLE_SYNCD_RPC=n
    "SONIC_BUILDER_EXTRA_CMDLINE=$builder_args"
    "DOCKER_LOAD_ON_BOOT=${DOCKER_LOAD_ON_BOOT:-n}"
    "SONIC_ENABLE_DEBUG_LAYER=${SONIC_ENABLE_DEBUG_LAYER:-n}"
)
revision=$(git rev-parse HEAD)
mkdir -p artifacts/vs
printf '%s\n' "$revision" > artifacts/vs/build-revision.txt
make target/sonic-vs.bin "${options[@]}"
