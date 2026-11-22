#!/usr/bin/env bash
# Export the VS TAR selected by the same Bazel configuration as the installer.
set -euo pipefail
output=$1
bazel=${BAZEL:-bazel}
label=//src/sonic-device-data:vs_runtime_tar
helper=tools/bazel/build_helpers.py
option_lines=$(python3 -B "$helper" options \
    --cache-directory="${BAZEL_CONTAINER_CACHE_DIR:-${BAZEL_SWSS_CACHE_DIR:-}}" \
    --arguments="${BAZEL_CONTAINER_ARGS:-${BAZEL_SWSS_ARGS:-}}")
options=()
if [[ -n "$option_lines" ]]; then mapfile -t options <<< "$option_lines"; fi
platform=@sonic_build_infra//platforms:x86_64_trixie
options+=("--platforms=$platform" "--host_platform=$platform")
# The installer must materialize this top-level TAR even in minimal-download configurations.
"$bazel" build "${options[@]}" --remote_download_outputs=toplevel "$label"
artifact=$("$bazel" cquery "${options[@]}" --output=files "$label")
exec_root=$("$bazel" info execution_root)
python3 -B "$helper" export --execution-root="$exec_root" --query-output="$artifact" --output="$output"
