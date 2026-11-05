#!/bin/bash
# A private daemon for runtime checks in one disposable Debian runner.
set -euo pipefail

die() { printf 'Private Docker: %s\n' "$*" >&2; exit 1; }
runner_temp=$(realpath "${RUNNER_TEMP:?}")

case "${1:-}" in
    start)
        [[ $# == 1 && -f /.dockerenv && $(id -u) != 0 ]] ||
            die 'Start requires a non-root user in a disposable runner container.'
        [[ -z ${DOCKER_HOST:-}${DOCKER_CONTEXT:-}${DOCKER_TLS_VERIFY:-}${DOCKER_CERT_PATH:-} ]] ||
            die 'Do not inherit a Docker endpoint.'
        [[ ! -e /var/run/docker.sock ]] || die 'An existing Docker socket is not allowed.'
        sudo -n true
        # Package maintainer scripts must not launch a second, default daemon.
        policy_created=n
        restore_policy() {
            if [[ $policy_created == y ]]; then sudo -n rm /usr/sbin/policy-rc.d; fi
        }
        trap restore_policy EXIT
        if ! sudo -n test -e /usr/sbin/policy-rc.d; then
            printf '#!/bin/sh\nexit 101\n' | sudo -n tee /usr/sbin/policy-rc.d >/dev/null
            sudo -n chmod 755 /usr/sbin/policy-rc.d
            policy_created=y
        fi
        sudo -n apt-get update
        sudo -n apt-get install -y --no-install-recommends docker.io docker-cli docker-buildx
        restore_policy
        trap - EXIT
        sudo -n python3 -B "$(dirname "$0")/prepare_nested_docker.py"
        state=$(mktemp -d "$runner_temp/sonic-docker.XXXXXX")
        # Export early so the always() cleanup step can stop a failed startup.
        printf 'SONIC_PRIVATE_DOCKER_STATE=%s\nDOCKER_HOST=unix://%s/docker.sock\nDOCKER_CONFIG=%s/client\n' \
            "$state" "$state" "$state" >> "${GITHUB_ENV:?}"
        mkdir -m 700 "$state/client"
        printf '{}\n' > "$state/daemon.json"
        export DOCKER_HOST="unix://$state/docker.sock" DOCKER_CONFIG="$state/client"
        # The daemon and its containerd have private state. No host socket or
        # inherited Docker configuration/credentials are used.
        sudo -n env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
            RUNNER_TRACKING_ID="${RUNNER_TRACKING_ID:-}" \
            "$(command -v dockerd)" --config-file "$state/daemon.json" \
            --host "$DOCKER_HOST" --group "$(id -g)" \
            --data-root "$state/data" --exec-root "$state/exec" --pidfile "$state/daemon.pid" \
            --storage-driver overlay2 --exec-opt native.cgroupdriver=cgroupfs \
            > "$state/daemon.log" 2>&1 &
        deadline=$((SECONDS + 120))
        until timeout 3s docker info >/dev/null 2>&1; do
            if (( SECONDS >= deadline )); then
                tail -n 40 "$state/daemon.log" >&2
                die 'Daemon did not become ready within 120 seconds.'
            fi
            sleep 1
        done
        [[ $(docker info --format '{{.DockerRootDir}}') == "$state/data" ]] ||
            die 'Unexpected daemon data root.'
        docker version
        ;;
    stop)
        [[ $# == 1 ]] || die 'Unexpected arguments.'
        state=${SONIC_PRIVATE_DOCKER_STATE:-}
        [[ -n $state ]] || exit 0
        [[ $(dirname "$state") == "$runner_temp" && $(basename "$state") =~ ^sonic-docker\.[a-zA-Z0-9]{6}$ && ! -L $state ]] ||
            die 'Invalid state path.'
        if [[ -s $state/daemon.pid ]]; then
            # Confirm this PID still belongs to the daemon this job started.
            sudo -n python3 - "$state" <<'PY'
import os
from pathlib import Path
import signal
import sys
import time
state = Path(sys.argv[1])
pid = int((state / 'daemon.pid').read_text())
if pid <= 1:
    raise SystemExit('Invalid daemon PID')
process = Path('/proc') / str(pid)
def stopped():
    try:
        return (process / 'stat').read_text().split()[2] == 'Z'
    except FileNotFoundError:
        return True

if stopped():
    raise SystemExit(0)
try:
    command = (process / 'cmdline').read_bytes().split(b'\0')
except FileNotFoundError:
    raise SystemExit(0)
if command[0]:
    if (Path(os.fsdecode(command[0])).name != 'dockerd'
            or os.fsencode(str(state / 'data')) not in command):
        raise SystemExit('Refusing to stop an unrelated process')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise SystemExit(0)
# An exiting daemon can have an empty cmdline; wait without signaling it.
for _ in range(60):
    if stopped():
        break
    time.sleep(1)
else:
    raise SystemExit('Private Docker did not stop within 60 seconds')
PY
        fi
        # Keep diagnostics, but do not retain pulled images in the worker volume.
        sudo -n rm -rf --one-file-system -- "$state/data" "$state/exec"
        ;;
    *) die 'Usage: private_docker.sh start|stop' ;;
esac
