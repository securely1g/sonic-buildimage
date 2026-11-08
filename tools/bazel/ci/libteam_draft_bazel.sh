#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=6822771900004be658ba994a6b48e5a882a0d92b
exec "$(dirname "$0")/review_bazel.sh" "$@"
