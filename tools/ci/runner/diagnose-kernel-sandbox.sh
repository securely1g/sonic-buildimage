#!/usr/bin/env bash
# One-off diagnostic, on an isolated branch; leaves host policy unchanged.
set -euo pipefail
mkdir -p artifacts/kernel-sandbox
exec > >(tee artifacts/kernel-sandbox/probe.log) 2>&1
image=sonic-slave-trixie:4b8b34e9bb1
old_workspace=/data/sonic-runner/attempts/20261025T201632Z-e97777a5/_work/sonic-buildimage/sonic-buildimage/vs-40255619811-1
test -d "$old_workspace/target/bazel-kernel-state"
docker image inspect "$image" --format 'image_id={{.Id}}'
printf 'Host: '; uname -a
id
docker ps --format '{{.ID}} {{.Image}} {{.Status}}'
docker run --rm --privileged --init \
  --mount "type=bind,src=$old_workspace,dst=/sonic" \
  --mount type=bind,src=/data/sonic-runner/cache/sonic-buildimage/bazel,dst=/bazel_cache \
  --mount "type=bind,src=$PWD/tools/ci/runner/probe-kernel-sandbox.sh,dst=/probe.sh,readonly" \
  --workdir /sonic "$image" bash /probe.sh
