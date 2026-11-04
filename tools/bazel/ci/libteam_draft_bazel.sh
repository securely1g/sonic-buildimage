#!/usr/bin/env bash
# Temporary PR validation while libteam's dependency entries remain unlanded.
# Keep the normal .bazelrc on canonical main; remove this wrapper after landing.
set -euo pipefail
# Preserve this image's exact catalog while using the maintained launcher for
# worker-selected JVM/resources and the existing local cache configuration.
export SONIC_BAZEL_REGISTRY_REVISION=e0bf141cea82764b18d15d7d2c8516cbf3e99290
exec "$(dirname "$0")/review_bazel.sh" "$@"
