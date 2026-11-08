#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=adce859db70592be89112c6af19ea0b25deef367
exec "$(dirname "$0")/review_bazel.sh" "$@"
