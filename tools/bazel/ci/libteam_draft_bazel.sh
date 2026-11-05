#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=1d1e57334ad7b2ddd896f61f1bcb773c5c412c7a
exec "$(dirname "$0")/review_bazel.sh" "$@"
