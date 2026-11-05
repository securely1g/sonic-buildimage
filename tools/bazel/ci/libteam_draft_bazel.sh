#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=99ea0d52c0e780b340ff210aaa055f6cc1a03289
exec "$(dirname "$0")/review_bazel.sh" "$@"
