#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=28bd77d92d8d28f100ca328489a49f3b025e1a65
exec "$(dirname "$0")/review_bazel.sh" "$@"
