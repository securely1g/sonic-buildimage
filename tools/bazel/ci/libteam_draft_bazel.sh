#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=04164b00ec3f28c4b2806714d3825ac8f57a9af9
exec "$(dirname "$0")/review_bazel.sh" "$@"
