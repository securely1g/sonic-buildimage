#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=accd59217ebf93a984fd7688d7508b48c3a735c4
exec "$(dirname "$0")/review_bazel.sh" "$@"
