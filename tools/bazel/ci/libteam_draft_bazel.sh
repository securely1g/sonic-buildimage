#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=927a08c2f0985719d1ba1b1cbeab5eb44cdbb69d
exec "$(dirname "$0")/review_bazel.sh" "$@"
