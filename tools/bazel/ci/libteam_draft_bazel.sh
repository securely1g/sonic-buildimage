#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=ade59395fe876f7bed63d27c6330faf3c6848f72
exec "$(dirname "$0")/review_bazel.sh" "$@"
