#!/usr/bin/env bash
# Compatibility entry point for inherited source-only image workflows.
# The shared implementation selects one complete review catalog and preserves caches.
set -euo pipefail
export SONIC_BAZEL_REGISTRY_REVISION=2c9a1bfed7c4e5db33bb7279ea10d055a17eff29
exec "$(dirname "$0")/review_bazel.sh" "$@"
