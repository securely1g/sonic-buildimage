#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=1a7de6cd36285bd696b3bfe2bdc19aa58e6e1bf5
exec "$(dirname "$0")/review_bazel.sh" "$@"
