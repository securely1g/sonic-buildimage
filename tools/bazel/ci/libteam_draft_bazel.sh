#!/usr/bin/env bash
# Pin the landed registry snapshot for reproducible libteam validation.
# Keep the normal .bazelrc on canonical main; this wrapper only selects CI inputs.
set -euo pipefail
if [[ $# == 1 && "$1" == --version ]]; then
  exec bazel --version
fi
startup_options=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch|--output_base=*|--output_user_root=*|--host_jvm_args=*) startup_options+=("$1"); shift ;;
    *) break ;;
  esac
done
bazel_command="${1:?expected a Bazel command}"
shift
case "$bazel_command" in
  aquery|build|cquery|fetch|info|mod|query|shutdown|test) ;;
  *) printf 'Unsupported Draft CI Bazel command: %s\n' "$bazel_command" >&2; exit 2 ;;
esac
# The worker owns these files: resource/sandbox settings and operator cache
# configuration. Keep them when isolating the workspace's registry selection.
worker_rc=()
case "${SONIC_CI_EXECUTION_MODE:-}" in
  native|qemu-aarch64)
    worker_rc=(--bazelrc=/etc/bazel.bazelrc --bazelrc="$HOME/.bazelrc") ;;
esac
# Shutdown accepts startup settings, but not registry/build options.
if [[ "$bazel_command" == shutdown ]]; then
  exec bazel "${startup_options[@]}" --nosystem_rc --nohome_rc --noworkspace_rc \
    "${worker_rc[@]}" shutdown "$@"
fi
# The cache-reuse verifier runs this wrapper from fresh source snapshots too.
exec bazel "${startup_options[@]}" --nosystem_rc --nohome_rc --noworkspace_rc \
  "${worker_rc[@]}" \
  --bazelrc="$PWD/tools/bazel/common.bazelrc" \
  "$bazel_command" \
  --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/a1334b9a9604733a2faf6cc96536d289da09e8d8 \
  --registry=https://bcr.bazel.build --lockfile_mode=update "$@"
