#!/usr/bin/env bash
# Temporary PR validation while FRR's dependency entries remain unlanded.
# Keep the normal .bazelrc on canonical main; remove this wrapper after landing.
set -euo pipefail
if [[ $# == 1 && "$1" == --version ]]; then
  exec bazel --version
fi
startup_options=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch|--output_base=*|--output_user_root=*) startup_options+=("$1"); shift ;;
    *) break ;;
  esac
done
bazel_command="${1:?expected a Bazel command}"
shift
case "$bazel_command" in
  aquery|build|cquery|fetch|info|mod|query|test) ;;
  *) printf 'Unsupported Draft CI Bazel command: %s\n' "$bazel_command" >&2; exit 2 ;;
esac
# The cache-reuse verifier runs this wrapper from fresh source snapshots too.
exec bazel "${startup_options[@]}" --nosystem_rc --nohome_rc --noworkspace_rc \
  --bazelrc="$PWD/tools/bazel/common.bazelrc" \
  "$bazel_command" \
  --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/04a71b7883384e995a5ae089e2b6f6a0601164de \
  --registry=https://bcr.bazel.build --lockfile_mode=update "$@"
