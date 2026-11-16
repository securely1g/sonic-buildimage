#!/bin/bash
set -euo pipefail

if (( $# )); then
    echo 'Usage: prewarm_vs_repositories.sh' >&2
    exit 2
fi

option_lines=$(python3 -B tools/bazel/build_helpers.py options \
    --cache-directory="${BAZEL_CONTAINER_CACHE_DIR-${BAZEL_SWSS_CACHE_DIR:-}}" \
    --arguments="${BAZEL_CONTAINER_ARGS-${BAZEL_SWSS_ARGS:-}}")
options=()
if [[ -n "$option_lines" ]]; then mapfile -t options <<< "$option_lines"; fi
contents_cache=
for option in "${options[@]}"; do
    if [[ $option == --repo_contents_cache=* ]]; then
        contents_cache=${option#*=}
    fi
done
if [[ $contents_cache != /* ]]; then
    echo 'VS repository prewarm requires an absolute --repo_contents_cache path' >&2
    exit 1
fi
mkdir -p -- "$contents_cache"
contents_cache=$(realpath -- "$contents_cache")
timing_directory=$(python3 -B tools/bazel/ci/time_vs_image.py prewarm-recipe)
source tools/bazel/private_server.sh
start_private_bazel repository-prewarm
private_bazel_shutdown_prefix=(python3 -B tools/bazel/ci/time_vs_image.py "$timing_directory" shutdown)

# Fetch the host LLVM distribution, not every repository or any build action.
repository=sonic-fips++clang+symcrypt_llvm_llvm
python3 -B tools/bazel/ci/time_vs_image.py "$timing_directory" fetch \
    "${private_bazel[@]}" fetch "${options[@]}" "--repo=@@$repository"
repository_path="$bazel_output_base/external/$repository"
if [[ ! -L "$repository_path" || $(realpath -- "$repository_path") != "$contents_cache/"* ]]; then
    echo 'LLVM repository was not shared through the repository-contents cache' >&2
    exit 1
fi
printf 'LLVM repository prepared in the shared repository-contents cache\n'
