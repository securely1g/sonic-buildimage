#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=af9c810989f0577a555933f836e80610d6eda06b
exec "$(dirname "$0")/review_bazel.sh" "$@"
