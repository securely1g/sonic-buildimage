#!/usr/bin/env bash
# One-off diagnostic and scoped policy repair, on an isolated branch.
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
container_name=sonic-kernel-probe-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}
host_policy() {
  docker run --rm --privileged --pid=host "$image" \
    nsenter --target 1 --mount --root --wd -- /usr/bin/python3 \
    "$PWD/tools/ci/runner/apparmor_kernel.py" "$@"
}
cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
run_probe() {
timeout 180s docker run --rm --privileged --init --name "$container_name" \
  -e "PROBE_REQUIRE_SUCCESS=${1:-0}" \
  --mount "type=bind,src=$probe_workspace,dst=/sonic" \
  --mount "type=bind,src=$PWD/tools/ci/runner/probe-kernel-sandbox.sh,dst=/probe.sh,readonly" \
  --mount "type=bind,src=$PWD/tools/ci/runner/sandbox-action-probe,dst=/action-probe,readonly" \
  --workdir /sonic "$image" bash /probe.sh
}
printf '\nBEFORE durable scoped policy:\n'
run_probe
host_policy --install
host_policy
printf '\nAFTER durable scoped policy:\n'
run_probe 1
