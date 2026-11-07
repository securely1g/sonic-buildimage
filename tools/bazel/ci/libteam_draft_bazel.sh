#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=02756ae8499e2325f4b652e4ff0eb02bbc38d05c
exec "$(dirname "$0")/review_bazel.sh" "$@"
