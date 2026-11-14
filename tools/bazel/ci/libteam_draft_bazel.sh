#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=e6ef022c94dd2fdeeac364dfd591f9701cd37c29
exec "$(dirname "$0")/review_bazel.sh" "$@"
