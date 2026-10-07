#!/bin/sh
# Temporary Draft entry point for the unpublished APT module versions.
set -eu
if [ "$#" -eq 1 ] && [ "$1" = --version ]; then
    exec bazel --version
fi
draft_dir=$(dirname "$(readlink -f "$0")")
# Preserve the reviewed Make cache settings when the builder mounts its cache.
if [ -d /bazel_cache ]; then
    set -- --bazelrc="$draft_dir/../slave.bazelrc" "$@"
fi
exec bazel --nosystem_rc --nohome_rc --noworkspace_rc \
    --bazelrc="$draft_dir/draft-registry.bazelrc" "$@"
