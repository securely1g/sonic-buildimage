#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=9f225754068d1253ca56d6d40a7c5b2bd9de35ac
exec "$(dirname "$0")/review_bazel.sh" "$@"
