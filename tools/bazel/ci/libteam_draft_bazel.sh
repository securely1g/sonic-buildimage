#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=08480c85afb70cdcbfc790ce970903d5d60e91d9
exec "$(dirname "$0")/review_bazel.sh" "$@"
