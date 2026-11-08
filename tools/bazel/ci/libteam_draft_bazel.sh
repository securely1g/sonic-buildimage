#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=c2d8152ea91cc543547d0078c19928cd6f082417
exec "$(dirname "$0")/review_bazel.sh" "$@"
