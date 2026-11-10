#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=7340f9b25dff81bdad9120231b578b66e754fd03
exec "$(dirname "$0")/review_bazel.sh" "$@"
