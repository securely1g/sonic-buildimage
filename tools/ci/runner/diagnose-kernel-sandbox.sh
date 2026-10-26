#!/usr/bin/env bash
# One-off diagnostic, on an isolated branch; leaves host policy unchanged.
set -euo pipefail
mkdir -p artifacts/kernel-sandbox
exec > >(tee artifacts/kernel-sandbox/probe.log) 2>&1
image=sonic-slave-trixie:4b8b34e9bb1
probe_workspace=$PWD/sandbox-workspace
mkdir -p "$probe_workspace"
docker image inspect "$image" --format 'image_id={{.Id}}'
printf 'Host: '; uname -a
id
docker ps --format '{{.ID}} {{.Image}} {{.Status}}'
docker run --rm --privileged --init \
  --mount "type=bind,src=$probe_workspace,dst=/sonic" \
  --mount "type=bind,src=$PWD/tools/ci/runner/probe-kernel-sandbox.sh,dst=/probe.sh,readonly" \
  --workdir /sonic "$image" bash /probe.sh
