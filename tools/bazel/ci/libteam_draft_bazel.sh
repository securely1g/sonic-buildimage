#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=d732010ffb76ee857b4a906f3648bf51e7aa57ae
exec "$(dirname "$0")/review_bazel.sh" "$@"
