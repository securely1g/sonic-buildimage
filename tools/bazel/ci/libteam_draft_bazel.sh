#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=7b20b91ef97c45c30b83db8aadc605137e6d40a3
exec "$(dirname "$0")/review_bazel.sh" "$@"
