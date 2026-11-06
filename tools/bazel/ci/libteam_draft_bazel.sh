#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=27d4f97bec8aa2c59527ec39eade76eca061a967
exec "$(dirname "$0")/review_bazel.sh" "$@"
