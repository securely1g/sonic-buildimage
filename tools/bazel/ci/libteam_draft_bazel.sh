#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=ddf0e11162d5a19df88338b4b67a4c5235700590
exec "$(dirname "$0")/review_bazel.sh" "$@"
