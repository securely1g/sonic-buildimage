#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=6a69c2d89070d4d2ffc80b1e13626da387f7ea65
exec "$(dirname "$0")/review_bazel.sh" "$@"
