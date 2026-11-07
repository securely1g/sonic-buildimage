#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=04a71b7883384e995a5ae089e2b6f6a0601164de
exec "$(dirname "$0")/review_bazel.sh" "$@"
