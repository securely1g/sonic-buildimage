#!/bin/bash
# Validate both OCI image families and P4 debug packages in one Trixie builder.
set -euo pipefail

if (( $# == 0 )); then
    echo 'Usage: verify_vs_oci.sh MAKE [MAKE_ARGUMENT ...]' >&2
    exit 2
fi
make_command=("$@")
mkdir -p artifacts/vs/syncd-p4-images

# Ask the complete slave configuration for its resolved values. This no-op
# query defines no package or aggregate build target and never parses Make's
# implementation details or hardcodes a package version.
configuration=$("${make_command[@]}" --no-print-directory -s -j1 -f - .vs-ci-config <<'MAKE'
$(info VS_CI:DEBS_PATH=$(strip $(DEBS_PATH)))
$(info VS_CI:TARGET_PATH=$(strip $(TARGET_PATH)))
$(info VS_CI:DOCKER_CONFIG_ENGINE_TRIXIE=$(strip $(DOCKER_CONFIG_ENGINE_TRIXIE)))
$(info VS_CI:SONIC_P4RT=$(strip $(SONIC_P4RT)))
$(info VS_CI:SONIC_P4RT_DBG=$(strip $(SONIC_P4RT_DBG)))
$(info VS_CI:BUILD_WITH_BAZEL_WHEN_AVAILABLE=$(strip $(BUILD_WITH_BAZEL_WHEN_AVAILABLE)))
$(info VS_CI:BLDENV=$(strip $(BLDENV)))
$(info VS_CI:CONFIGURED_PLATFORM=$(strip $(CONFIGURED_PLATFORM)))
$(info VS_CI:CONFIGURED_ARCH=$(strip $(CONFIGURED_ARCH)))
$(info VS_CI:INCLUDE_FIPS=$(strip $(INCLUDE_FIPS)))
$(info VS_CI:INCLUDE_VS_DASH_SAI=$(strip $(INCLUDE_VS_DASH_SAI)))
$(info VS_CI:CROSS_BUILD_ENVIRON=$(strip $(CROSS_BUILD_ENVIRON)))
$(info VS_CI:MULTIARCH_QEMU_ENVIRON=$(strip $(MULTIARCH_QEMU_ENVIRON)))
$(info VS_CI:ENABLE_ASAN=$(strip $(ENABLE_ASAN)))
$(info VS_CI:ENABLE_SYNCD_RPC=$(strip $(ENABLE_SYNCD_RPC)))
$(info VS_CI:SONIC_BAZEL_DOCKER_IMAGES=$(strip $(SONIC_BAZEL_DOCKER_IMAGES)))
$(info VS_CI:SONIC_BAZEL_DBG_DOCKER_IMAGES=$(strip $(SONIC_BAZEL_DBG_DOCKER_IMAGES)))
.PHONY: .vs-ci-config
.vs-ci-config: ; @:
MAKE
)
declare -A config=()
while IFS= read -r line; do
    [[ $line == VS_CI:* ]] || continue
    key=${line#VS_CI:}
    key=${key%%=*}
    [[ $key =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo 'Invalid Make configuration key' >&2; exit 1; }
    config[$key]=${line#*=}
done <<< "$configuration"
if [[ ${config[BUILD_WITH_BAZEL_WHEN_AVAILABLE]}:${config[BLDENV]}:${config[CONFIGURED_PLATFORM]}:${config[CONFIGURED_ARCH]}:${config[INCLUDE_FIPS]}:${config[INCLUDE_VS_DASH_SAI]} != y:trixie:vs:amd64:y:y ]]; then
    echo 'VS validation requires native AMD64 Trixie VS with Bazel, FIPS and DASH enabled' >&2
    exit 1
fi
for flag in CROSS_BUILD_ENVIRON MULTIARCH_QEMU_ENVIRON ENABLE_ASAN ENABLE_SYNCD_RPC; do
    if [[ ${config[$flag]} == y ]]; then
        echo 'VS validation does not support cross/emulated, ASAN or Syncd RPC builds' >&2
        exit 1
    fi
done
for image in docker-orchagent docker-syncd-vs; do
    if [[ " ${config[SONIC_BAZEL_DOCKER_IMAGES]} " != *" $image.gz "* ||
          " ${config[SONIC_BAZEL_DBG_DOCKER_IMAGES]} " != *" $image-dbg.gz "* ]]; then
        echo 'VS validation requires both runtime/debug pairs to select Bazel producers' >&2
        exit 1
    fi
done
for key in DEBS_PATH SONIC_P4RT SONIC_P4RT_DBG TARGET_PATH DOCKER_CONFIG_ENGINE_TRIXIE; do
    [[ -n ${config[$key]} ]] || { echo "Missing Make package setting: $key" >&2; exit 1; }
done
if [[ " ${config[SONIC_BAZEL_DOCKER_IMAGES]} " != *" ${config[DOCKER_CONFIG_ENGINE_TRIXIE]} "* ]]; then
    echo 'Shared image validation requires the Bazel config-engine archive producer' >&2
    exit 1
fi
runtime_deb=${config[DEBS_PATH]}/${config[SONIC_P4RT]}
debug_deb=${config[DEBS_PATH]}/${config[SONIC_P4RT_DBG]}
base_archive=${config[TARGET_PATH]}/${config[DOCKER_CONFIG_ENGINE_TRIXIE]}
# Preserve the owner's dependency installation, freshness and cache rules.
# Finish the legacy P4 workspace before testing the image workspace.
# SWSS inherits this OCI base directly, so its Docker-load archive is not a
# service-image prerequisite. Materialize it once for the shared Docker checks.
"${make_command[@]}" --no-print-directory -j1 "$runtime_deb" "$debug_deb" "$base_archive"
for package in "$runtime_deb" "$debug_deb" "$base_archive"; do
    [[ -s $package ]] || { echo "Missing or empty P4RT prerequisite: $package" >&2; exit 1; }
done

# Match the archive recipes, including the caller's cache and distdir settings.
source tools/bazel/private_server.sh
option_lines=$(python3 tools/bazel/build_helpers.py options \
    --cache-directory="${BAZEL_CONTAINER_CACHE_DIR-${BAZEL_SWSS_CACHE_DIR:-}}" \
    --arguments="${BAZEL_CONTAINER_ARGS-${BAZEL_SWSS_ARGS:-}}")
bazel_options=()
if test -n "$option_lines"; then mapfile -t bazel_options <<< "$option_lines"; fi
bazel_options+=(--platforms=@sonic_build_infra//platforms:x86_64_trixie
    --host_platform=@sonic_build_infra//platforms:x86_64_trixie --symlink_prefix=/)

check_images() (
    set -e
    local test_name=$1 report_name=$2 status=0 file path
    local -a reports
    shift 2
    start_private_bazel "$test_name"
    "${private_bazel[@]}" test "${bazel_options[@]}" "$@" "//tools/bazel/tests:$test_name" \
        --nocache_test_results --nozip_undeclared_test_outputs \
        2>&1 | tee "artifacts/vs/$report_name.log" || status=$?

    # Each suite has a fresh private output base and runs in one configuration.
    # Bazel 8.5.1 info bazel-testlogs fails on apparent Bzlmod platform labels.
    shopt -s nullglob
    reports=("$bazel_output_base"/execroot/*/bazel-out/*/testlogs/tools/bazel/tests/"$test_name")
    if (( ${#reports[@]} != 1 )); then
        printf 'Expected one test report directory for %s, found %s\n' "$test_name" "${#reports[@]}" >&2
        if (( status )); then exit "$status"; fi
        exit 1
    fi
    mkdir -p "artifacts/vs/$report_name"
    for file in test.log test.xml test.outputs/validation.json; do
        path="${reports[0]}/$file"
        if [[ -f $path ]]; then
            cp -- "$path" "artifacts/vs/$report_name/${file##*/}"
        fi
    done
    exit "$status"
)

# Retain both suites' diagnostics, including when the first test fails.
set +e
check_images swss_images_test swss-images
swss_status=$?
check_images syncd_p4_images_test syncd-p4-images --config=syncd-dash
syncd_status=$?
set -e
if (( syncd_status )); then exit "$syncd_status"; fi
if (( swss_status )); then exit "$swss_status"; fi

materialize_policy() (
    local family=$1 label="//tools/bazel/tests:${1}_installed_images_policy" policy execution_root
    shift
    start_private_bazel "${family}_installed_images_policy"
    "${private_bazel[@]}" build "${bazel_options[@]}" "$@" "$label"
    policy=$("${private_bazel[@]}" cquery "${bazel_options[@]}" "$@" --output=files "$label")
    execution_root=$(private_bazel_execution_root)
    cp -- "$execution_root/$policy" "artifacts/vs/$family-installed-images-policy.json"
)
materialize_policy swss
materialize_policy syncd --config=syncd-dash

python3 -B tools/bazel/ci/verify_p4rt_debug.py \
    --runtime-deb "$runtime_deb" --debug-deb "$debug_deb" \
    --source-root src/sonic-p4rt/sonic-pins \
    --output artifacts/vs/p4rt-debug-verification.json \
    2>&1 | tee artifacts/vs/p4rt-debug-verification.log
