#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=0118eb7b838805085c7e6bc23244f5995934e701
exec "$(dirname "$0")/review_bazel.sh" "$@"
