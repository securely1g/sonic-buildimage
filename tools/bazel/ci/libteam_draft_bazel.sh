#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=f87ec1d97b0493d895e34733ea4aae92a90cf75d
exec "$(dirname "$0")/review_bazel.sh" "$@"
