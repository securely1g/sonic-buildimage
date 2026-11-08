#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=618c7279e814b858f803fd6b4891e99eedcb2400
exec "$(dirname "$0")/review_bazel.sh" "$@"
