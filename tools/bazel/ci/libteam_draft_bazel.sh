#!/usr/bin/env bash
# Pin the landed registry snapshot for reproducible libteam validation.
# Keep the normal .bazelrc on canonical main; this wrapper only selects CI inputs.
set -euo pipefail
if [[ $# == 1 && "$1" == --version ]]; then
  exec bazel --version
fi
startup_options=()
if [[ -n "${BAZEL_OUTPUT_USER_ROOT:-}" ]]; then
  if [[ "$BAZEL_OUTPUT_USER_ROOT" != /* ]]; then
    echo 'BAZEL_OUTPUT_USER_ROOT must be an absolute path.' >&2
    exit 2
  fi
  startup_options+=(--output_user_root="$BAZEL_OUTPUT_USER_ROOT")
fi
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
  --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/a1334b9a9604733a2faf6cc96536d289da09e8d8 \
  --registry=https://bcr.bazel.build --lockfile_mode=update "$@"
