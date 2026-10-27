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

# Match the archive recipes while keeping the runner's root-private
# cache defaults. Preserve caller settings; only Syncd selects DASH.
option_lines=$(python3 tools/bazel/build_helpers.py options \
    --arguments="${BAZEL_CONTAINER_ARGS-${BAZEL_SWSS_ARGS:-}}")
bazel_options=()
if test -n "$option_lines"; then mapfile -t bazel_options <<< "$option_lines"; fi
bazel_options+=(--platforms=@sonic_build_infra//platforms:x86_64_trixie
    --host_platform=@sonic_build_infra//platforms:x86_64_trixie)
native_runner=(python3 -B tools/bazel/oci/native_build.py)

collect_test_results() {
    local test_name="$1" output="artifacts/vs/$2" file path
    mkdir -p "$output"
    for file in test.log test.xml test.outputs/validation.json; do
        path="bazel-testlogs/tools/bazel/tests/$test_name/$file"
        if test -f "$path"; then
            cp "$path" "$output/$(basename "$file")"
        fi
    done
}
# Retain each result before another Bazel invocation changes bazel-testlogs.
test_status=0
"${native_runner[@]}" test "${bazel_options[@]}" //tools/bazel/tests:swss_images_test \
    --nocache_test_results --nozip_undeclared_test_outputs \
    2>&1 | tee artifacts/vs/swss-images.log || test_status=$?
collect_test_results swss_images_test swss-images
"${native_runner[@]}" test "${bazel_options[@]}" --config=syncd-dash //tools/bazel/tests:syncd_p4_images_test \
    --nocache_test_results --nozip_undeclared_test_outputs \
    2>&1 | tee artifacts/vs/syncd-p4-images.log || test_status=$?
collect_test_results syncd_p4_images_test syncd-p4-images
if test "$test_status" -ne 0; then exit "$test_status"; fi

# cquery alone does not materialize cached policy outputs.
"${native_runner[@]}" build "${bazel_options[@]}" //tools/bazel/tests:swss_installed_images_policy
policy=$("${native_runner[@]}" cquery "${bazel_options[@]}" --output=files //tools/bazel/tests:swss_installed_images_policy)
cp "$policy" artifacts/vs/swss-installed-images-policy.json
"${native_runner[@]}" build "${bazel_options[@]}" --config=syncd-dash //tools/bazel/tests:syncd_installed_images_policy
policy=$("${native_runner[@]}" cquery "${bazel_options[@]}" --config=syncd-dash --output=files //tools/bazel/tests:syncd_installed_images_policy)
cp "$policy" artifacts/vs/syncd-installed-images-policy.json

python3 -B tools/bazel/ci/verify_p4rt_debug.py \
    --runtime-deb "$runtime_deb" --debug-deb "$debug_deb" \
    --source-root src/sonic-p4rt/sonic-pins \
    --output artifacts/vs/p4rt-debug-verification.json \
    2>&1 | tee artifacts/vs/p4rt-debug-verification.log
