#!/usr/bin/env bash
# Shared CI Bazel entry point using the canonical maintained registry.
# Preserves explicit --bazelrc=tools/bazel/common.bazelrc across fresh snapshots.
set -euo pipefail
if [[ $# == 1 && "$1" == --version ]]; then
  exec bazel --version
fi
startup_options=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch|--host_jvm_args=*|--output_base=*|--output_user_root=*) startup_options+=("$1"); shift ;;
    *) break ;;
  esac
done
bazel_command="${1:?expected a Bazel command}"
shift
case "$bazel_command" in
  aquery|build|cquery|fetch|info|mod|query|test) ;;
  *) printf 'Unsupported CI Bazel command: %s\n' "$bazel_command" >&2; exit 2 ;;
esac
review_options=()
registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/main
if [[ -n "${SONIC_BASE_REVIEW_REGISTRY:-}" ]]; then
  [[ "$SONIC_BASE_REVIEW_REGISTRY" =~ ^[0-9a-f]{40}$ ]] || {
    echo 'Expected a full registry commit for base-image review.' >&2
    exit 2
  }
  registry="${registry%/main}/$SONIC_BASE_REVIEW_REGISTRY"
  case "$bazel_command" in
    aquery|build|cquery|fetch|info|test) review_options+=(--remote_upload_local_results=false) ;;
  esac
  for arg in "$@"; do
    case "$arg" in
      --registry|--registry=*) echo 'Duplicate review registry selection.' >&2; exit 2 ;;
    esac
  done
fi
# The cache-reuse verifier runs this wrapper from fresh source snapshots too.
exec bazel "${startup_options[@]}" --nosystem_rc --nohome_rc --noworkspace_rc \
  --bazelrc="$PWD/tools/bazel/common.bazelrc" \
  "$bazel_command" \
  --registry="$registry" \
  --registry=https://bcr.bazel.build --lockfile_mode=update "$@" "${review_options[@]}"
