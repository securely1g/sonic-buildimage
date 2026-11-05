#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# Keep one complete immutable catalog and the shared cache-preserving implementation.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=3491db65989fe2dac17e09aae03a00c31d21ca9a
exec "$(dirname "$0")/review_bazel.sh" "$@"
