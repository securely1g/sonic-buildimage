#!/usr/bin/env bash
set -euo pipefail
mkdir -p artifacts/kernel-sandbox
exec > >(tee artifacts/kernel-sandbox/permission-verification.log) 2>&1
id
root=/data/sonic-runner/cache/sonic-buildimage/bazel
for cache in repository_cache disk_cache kernel-linux-sandbox/repository_cache kernel-linux-sandbox/disk_cache; do
  path="$root/$cache"
  if test -d "$path"; then
    blocked=$(find "$path" -type d ! -writable -print -quit)
    test -z "$blocked" || { stat "$blocked"; exit 1; }
    printf 'Directory writability passed: %s\n' "$path"
  fi
done
docker run --rm --user 1001:1003 --env PYTHONPATH=/src --env HOME=/tmp \
  --mount "type=bind,src=$PWD,dst=/src,readonly" --workdir /src \
  sonic-slave-trixie:4b8b34e9bb1 /usr/bin/python3 -B -m unittest \
  tools.bazel.tests.kernel_test.KernelBundleTest.test_ensure_builds_once_for_parallel_requests_and_preserves_shared_cache -v
