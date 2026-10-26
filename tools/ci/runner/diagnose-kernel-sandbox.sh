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
container_name=sonic-kernel-probe-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}
profile=$PWD/tools/ci/runner/sonic-vs-kernel-sandbox
profile_loaded=false
host_parser() {
  docker run --rm --privileged --pid=host "$image" \
    nsenter --target 1 --mount --root --wd -- /usr/sbin/apparmor_parser "$@" "$profile"
}
cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  if "$profile_loaded"; then host_parser -R; fi
}
trap cleanup EXIT
run_probe() {
timeout 180s docker run --rm --privileged --init --name "$container_name" \
  --mount "type=bind,src=$probe_workspace,dst=/sonic" \
  --mount "type=bind,src=$PWD/tools/ci/runner/probe-kernel-sandbox.sh,dst=/probe.sh,readonly" \
  --workdir /sonic "$image" bash /probe.sh
}
printf '\nBEFORE temporary scoped policy:\n'
run_probe
host_parser -Q -K
host_parser -r -T
profile_loaded=true
printf '\nAFTER temporary scoped policy:\n'
run_probe
