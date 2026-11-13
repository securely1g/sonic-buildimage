#!/usr/bin/env bash
# Prepare one disposable SONiC-slave runner for the native VS installer build.
# The caller owns daemon shutdown and runs the build in this same container.
set -euo pipefail

die() { printf 'VS slave preparation: %s\n' "$*" >&2; exit 1; }
(( $# == 0 )) || die 'This script takes no arguments.'
reuse_only=${SONIC_VS_REUSE_ONLY:-n}
[[ $reuse_only == n || $reuse_only == y ]] || die 'Unknown VS preparation mode.'
supervised_phase=${SONIC_VS_SUPERVISED_PHASE:-}
if [[ $reuse_only == y ]]; then
    [[ $supervised_phase == before-daemon || $supervised_phase == after-daemon ]] ||
        die 'Reuse-only preparation requires the owned supervisor phase.'
else
    [[ -z $supervised_phase ]] || die 'Legacy preparation cannot select a supervisor phase.'
fi
controller_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
if [[ $reuse_only == y ]]; then
    [[ ${WORKER_POOL:-} == sonic-slave-container-k3s &&
       ${SONIC_DEB_BUILD_POLICY:-} == reuse-only &&
       ${SONIC_BAZEL_KERNEL_PREPARE:-} == verify &&
       ${SONIC_BAZEL_VS_ARCHIVE_PROFILE:-} == final23 &&
       ${SONIC_DPKG_CACHE_METHOD:-} == none &&
       ${SONIC_DPKG_CACHE_METHOD_OVERRIDE:-} == none &&
       ${SONIC_BAZEL_CACHE_POLICY:-} == remote-only ]] || die 'Reuse-only VS policy is incomplete.'
    # Read/hash original inputs before any package, daemon or Make preparation.
    python3 -I -S -B "$controller_root/tools/bazel/ci/vs_reuse_plan.py" inputs \
        --workspace "${GITHUB_WORKSPACE:?}" --manifest "${VS_REUSE_INPUTS:?}" \
        --manifest-sha256 "${VS_REUSE_INPUTS_SHA256:?}" --merge "${VS_SOURCE_MERGE:?}" \
        --source-head "${VS_SOURCE_HEAD:?}" --source-tree "${VS_SOURCE_TREE:?}" \
        --require-environment
fi
[[ $(id -u):$(id -g) == 1000:1000 ]] || die 'Expected runner UID/GID 1000:1000.'
workspace=$(realpath "${GITHUB_WORKSPACE:?}")
runner_temp=$(realpath "${RUNNER_TEMP:?}")
[[ $workspace == /actions/_work/* && $runner_temp == /actions/_work/* ]] ||
    die 'Checkout and runner temporary files must be below /actions/_work.'
[[ -f $workspace/slave.mk && -f /.dockerenv ]] || die 'Expected a SONiC checkout inside a container.'
[[ $(lsb_release -cs):$(uname -m) == trixie:x86_64 ]] || die 'Expected native AMD64 Debian Trixie.'
finish() {
    local status=$?
    trap - EXIT
    python3 - "$artifacts/slave-preparation.json" "$stage" "$status" <<'PY'
import json
from pathlib import Path
import sys
path, stage, status = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
report = json.loads(path.read_text()) if path.exists() else {"schema": 1}
report.update(status="prepared" if status == 0 else "failed", stage=stage, exit_code=status)
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
    if (( status )); then
        printf 'VS slave preparation failed during %s; inspect artifacts/vs/.\n' "$stage" >&2
    fi
    exit "$status"
}
if [[ $supervised_phase != after-daemon ]]; then
[[ -z ${DOCKER_HOST:-}${DOCKER_CONTEXT:-}${DOCKER_TLS_VERIFY:-}${DOCKER_CERT_PATH:-} ]] ||
    die 'An existing Docker endpoint must not be inherited by this job.'
[[ ! -e /var/run/docker.sock ]] || die 'An existing Docker socket is not allowed.'
sudo -n true || die 'This build requires the privileged runner profile and passwordless sudo.'
for command in docker dockerd make git python3 dpkg-deb mount findmnt ip timeout unshare; do
    command -v "$command" >/dev/null || die "Missing required tool: $command"
done
mountpoint -q /actions || die '/actions must be a worker-private Docker volume.'
vs_dpkg_cache=/dpkg_cache
vs_bazel_cache=/bazel_cache
if [[ $reuse_only == y ]]; then
    mkdir -p "$workspace/artifacts/vs"
    vs_work="$runner_temp/vs-working-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}-${RUNNER_NAME:?}"
    native_root=${SONIC_VS_NATIVE_CONTROLLER_ROOT:?A fresh immutable native-controller checkout is required.}
    python3 -I -S -B - "$native_root" "$workspace/artifacts/vs/k3s-storage.json" \
        "$runner_temp" "$vs_work" "$controller_root" <<'PY_VS_WORKING'
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile

NATIVE_REVISION = "6ba39f0b058e41eb2db7cc7cf01d62a2d7088bba"
NATIVE_FILES = {'tools/bazel/ci/source_image_worker.py': '3c8f2a5695d866d6c9a368dadf35308ba5aef81f752c7775046a85e71591ee75', 'tools/bazel/ci/source_worker_preflight.py': '9ffde2a7a08e5cb86bf9d91a05c6b462537fe9586c0576ca912f6983e49d9e9f', 'tools/bazel/cache/credentials.py': '9e44e83bccea514eaeea0a162d67295bb00d9be1cca764681b967380c7e0f5b0', 'tools/bazel/ci/image_worker_receipt.py': 'eae654c75ff20d3dbc18a0e8e32bf065fb9b5b136d232548b1cbef5804d9b5db'}

def require(value, message):
    if not value:
        raise ValueError(message)




def load_native(root, workspace, temporary):
    require(root.is_absolute() and root.resolve() == root and
            any(root.is_relative_to(parent) and root != parent for parent in (workspace, temporary)),
            "Native checkout is outside the selected job paths")
    # Bind the actual published native controller; cleanup keeps its separate historical source.
    revision = subprocess.check_output(["git", "--no-optional-locks", "-C", str(root),
                                        "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=10)
    require(revision == (NATIVE_REVISION + "\n").encode(), "Native controller revision differs")
    require(subprocess.run(["git", "--no-optional-locks", "-C", str(root), "diff", "--quiet",
                            "--no-ext-diff", "--no-textconv", "HEAD", "--", "tools"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10).returncode == 0,
            "Native tracked source differs from the selected controller")
    # No exclude-standard: ignored bytecode is also an untrusted import input.
    untracked = subprocess.check_output(["git", "--no-optional-locks", "-C", str(root),
                                        "ls-files", "--others", "--", "tools"],
                                       stderr=subprocess.DEVNULL, timeout=10)
    require(not untracked, "Untracked native import inputs must not be adopted")
    for name, expected in NATIVE_FILES.items():
        path = root / name
        info = path.lstat()
        require(path.resolve() == path and stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 512 * 1024,
                "Native import is not a bounded regular source")
        with path.open("rb") as stream:
            body = stream.read(512 * 1024 + 1)
        require(len(body) == info.st_size and hashlib.sha256(body).hexdigest() == expected,
                "Native import source binding differs")
    sys.path.insert(0, str(root))
    from tools.bazel.ci import source_image_worker as native
    from tools.bazel.ci import source_worker_preflight as resources
    for module, name in ((native, "source_image_worker.py"), (resources, "source_worker_preflight.py")):
        require(Path(module.__file__).resolve() == root / "tools/bazel/ci" / name,
                "Native module imported from another checkout")
    require(native.ROOT == root, "Native module root differs")
    return native, resources




def validate_policy(environment):
    expected = {"WORKER_POOL": "sonic-slave-container-k3s", "SONIC_VS_REUSE_ONLY": "y",
                "SONIC_DEB_BUILD_POLICY": "reuse-only", "SONIC_BAZEL_KERNEL_PREPARE": "verify",
                "SONIC_BAZEL_VS_ARCHIVE_PROFILE": "final23", "SONIC_DPKG_CACHE_METHOD": "none",
                "SONIC_DPKG_CACHE_METHOD_OVERRIDE": "none", "SONIC_BAZEL_CACHE_POLICY": "remote-only"}
    require(all(environment.get(key) == value for key, value in expected.items()),
            "Reuse-only remote-cache policy differs")


def scope_name(run, attempt, worker):
    require(all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value)
                for value in (run, attempt)), "Invalid run-attempt working identity")
    require(isinstance(worker, str) and worker_matches_pool(worker, "sonic-slave-container-k3s"),
            "Invalid K3s working identity")
    return "vs-working-" + run + "-" + attempt + "-" + worker


def fresh_scope(parent, run, attempt, worker, work_root, owner):
    name = scope_name(run, attempt, worker)
    require(parent.is_absolute() and parent.resolve() == parent and
            parent.is_relative_to(work_root) and parent != work_root,
            "Working parent is outside the canonical runner namespace")
    info = parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and (info.st_uid, info.st_gid) == owner
            and not stat.S_IMODE(info.st_mode) & 0o022,
            "Working parent ownership or permissions differ")
    root = parent / name
    root.mkdir(mode=0o700)
    for relative in ("packages", "packages/vcache", "bazel", "bazel/repository_cache",
                     "bazel/bazelisk", "bazel/output-base", "tmp"):
        (root / relative).mkdir(mode=0o700)
    for path in (root, *[root / relative for relative in
                        ("packages", "packages/vcache", "bazel", "bazel/repository_cache",
                         "bazel/bazelisk", "bazel/output-base", "tmp")]):
        current = path.lstat()
        require(path.resolve() == path and stat.S_ISDIR(current.st_mode)
                and (current.st_uid, current.st_gid) == owner
                and stat.S_IMODE(current.st_mode) == 0o700 and current.st_dev == info.st_dev,
                "Fresh working scope identity differs")
    return root


def writable_scope(root):
    with tempfile.TemporaryFile(mode="w+b", dir=root) as probe:
        probe.write(b"vs-run-attempt-working-probe\n")
        probe.flush()
        os.fsync(probe.fileno())
        probe.seek(0)
        require(probe.read(64) == b"vs-run-attempt-working-probe\n", "Working probe differs")




validate_policy(os.environ)
workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
native, resources = load_native(Path(sys.argv[1]), workspace, Path(sys.argv[3]))
# Check actual assembly source before the canonical native verifier imports
# any code from that checkout. Use the existing native API and stage guard.
import runpy
controller = Path(sys.argv[5])
planner = runpy.run_path(str(controller / "tools/bazel/ci/vs_reuse_plan.py"))
stages = runpy.run_path(str(controller / "tools/bazel/ci/vs_reuse_stages.py"))
inventory_path = Path(os.environ["VS_REUSE_INPUTS"])
inventory = planner["data"](inventory_path, os.environ["VS_REUSE_INPUTS_SHA256"])
assembly = planner["assembly_domain"](inventory,
    tested_merge=os.environ["VS_SOURCE_MERGE"], tested_tree=os.environ["VS_SOURCE_TREE"])
allowed = [name for name in inventory["files"] if name.startswith(("target/", "artifacts/"))]
if inventory_path.is_relative_to(workspace):
    allowed.append(str(inventory_path.relative_to(workspace)))
checkout = stages["verify_assembly_checkout"](
    native, workspace, revision=assembly["commit"], tree=assembly["tree"],
    allowed_untracked=sorted(set(allowed)))
admission = subprocess.run(
    stages["planner_command"]("admission", controller=controller, workspace=workspace,
                             manifest=inventory_path,
                             manifest_sha256=os.environ["VS_REUSE_INPUTS_SHA256"],
                             merge=os.environ["VS_SOURCE_MERGE"],
                             source_head=os.environ["VS_SOURCE_HEAD"],
                             source_tree=os.environ["VS_SOURCE_TREE"]),
    # The supervisor already owns this private stderr log; retain nested
    # canonical-verifier traceback facts there instead of discarding them.
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=sys.stderr,
    timeout=900, check=True)
require(len(admission.stdout) <= 2 * 1024**2, "Oversized native admission result")
native_admission = planner["strict_json"](admission.stdout)
require(native_admission.get("status") == "NATIVE_INPUTS_VERIFIED_BEFORE_PREPARATION_ONLY"
        and native_admission["assembly"] == assembly, "Canonical native inputs did not pass")
worker_matches_pool = resources.worker_matches_pool
marker_identity = resources.marker_identity
cpu_limit = resources.cpu_limit
select_build_profile = resources.select_build_profile
require(os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 1000,
        "Expected worker UID/GID1000")
require(platform.machine() == "x86_64" and platform.freedesktop_os_release().get("ID") == "debian"
        and platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
        "Expected native AMD64 Debian Trixie")
worker, run, attempt = (os.environ[key] for key in ("RUNNER_NAME", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
identity = marker_identity(Path("/etc/sonic-slave-github-runner.json"))
cgroups = Path("/sys/fs/cgroup")
require(Path("/proc/self/cgroup").read_text().strip() == "0::/"
        and (cgroups / "cgroup.type").read_text().strip() == "domain", "Private cgroup v2 differs")
require({"cpu", "memory", "pids"} <= set((cgroups / "cgroup.controllers").read_text().split()),
        "Missing delegated cgroup controllers")
memory = int((cgroups / "memory.max").read_text().strip())
cpus = cpu_limit((cgroups / "cpu.max").read_text().strip())
require(memory == 20 * 1024**3 and cpus == 6, "K3s allocation differs")
profile = select_build_profile(memory, cpus)
for name in ("bazel", "readelf", "mountpoint"):
    require(shutil.which(name) is not None, "Missing native preflight tool: " + name)
for command in (["unshare", "--user", "--map-root-user", "--mount", "true"],
                ["mountpoint", "-q", "/actions"],
                ["sudo", "-n", "test", "-w", "/sys/fs/cgroup/cgroup.procs"],
                ["sudo", "-n", "test", "-w", "/sys/fs/cgroup/cgroup.subtree_control"]):
    require(subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10).returncode == 0,
            "Private namespace or delegation check failed")
for name in ("/var/run/docker.sock", "/actions/sonic-native-docker", "/run/sonic-native-docker"):
    require(not os.path.lexists(name), "Existing private Docker state must not be adopted")
parent = Path(sys.argv[3])
filesystem = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", str(parent)],
                                     text=True, timeout=10).strip()
mount = subprocess.check_output(["findmnt", "-n", "-o", "TARGET", "-T", str(parent)],
                               text=True, timeout=10).strip()
require(filesystem in ("ext4", "xfs") and mount == "/actions"
        and parent.stat().st_dev == Path("/actions").stat().st_dev,
        "Temporary working state must use the private actions volume")
free = shutil.disk_usage(parent).free
require(free >= 90 * 1024**3, "VS workspace has less than90GiB free after staging")
root = fresh_scope(parent, run, attempt, worker, Path("/actions/_work"), (1000, 1000))
require(str(root) == sys.argv[4], "Shell working scope differs")
writable_scope(root)
# This reuse-only route does not create a Bazel launcher or consult cache settings.
output = Path(sys.argv[2])
require(output.parent.resolve() == output.parent, "Working receipt parent is not canonical")
with output.open("x") as stream:
    json.dump({"schema": 1, "status": "passed", "scope": "fresh_run_attempt_working_preflight_only",
               "worker": worker, "run": run, "attempt": attempt, "slave": identity,
               "native_controller_revision": NATIVE_REVISION, "native_source_bindings": NATIVE_FILES,
               "assembly_checkout": checkout, "native_admission": native_admission,
               "build_profile": profile, "memory_limit_bytes": memory, "cpu_limit": cpus,
               "workspace_storage": {"filesystem": filesystem, "free_bytes": free,
                                     "shared_with_working_state": True},
               "working_root": str(root), "working_probe_closed": True,
               "persistent_cache_dependency": False, "cache_HTTP_probe": False,
               "Bazel_executed": False, "package_generation_executed": False},
              stream, sort_keys=True, indent=2)
    stream.write("\n")
PY_VS_WORKING
    vs_dpkg_cache="$vs_work/packages"
    vs_bazel_cache="$vs_work/bazel"
    export TMPDIR="$vs_work/tmp"
    # Match the passed kernel probe; install the dormant build launcher afterwards.
    export BAZEL=bazel
    unset BAZEL_REAL
else
    for cache in /dpkg_cache /bazel_cache; do
        mountpoint -q "$cache" && [[ -w $cache ]] || die "Required writable cache mount is missing: $cache"
    done
fi

artifacts="$workspace/artifacts/vs"
mkdir -p "$artifacts"
stage=preflight
trap finish EXIT

python3 - "$artifacts/slave-preparation.json" <<'PY'
import grp
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

def require(condition, message):
    if not condition:
        raise SystemExit(message)

marker_path = Path("/etc/sonic-slave-github-runner.json")
info = marker_path.lstat()
require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o222 == 0,
        "Runner image marker must be a root-owned, read-only regular file")
marker = json.loads(marker_path.read_text())
for key, expected in {"uid": 1000, "gid": 1000, "runner_root": "/opt/actions-runner",
                      "work_root": "/actions/_work"}.items():
    require(marker.get(key) == expected, f"Unexpected runner image marker field: {key}")
require(re.fullmatch(r"sha256:[0-9a-f]{64}", marker.get("base_image", "")),
        "Runner marker must identify the immutable SONiC slave base image")
expected = os.environ.get("EXPECTED_MANAGER_REVISION", "")
require(re.fullmatch(r"[0-9a-f]{40}", expected) and marker.get("manager_revision") == expected,
        "Runner manager revision differs from the workflow pin")
require(grp.getgrnam("docker").gr_gid in os.getgroups(), "Runner process lacks the Docker group")
require(Path("/proc/self/cgroup").read_text().strip() == "0::/",
        "Expected a fresh, private cgroup v2 namespace")
cgroups = Path("/sys/fs/cgroup")
memory = (cgroups / "memory.max").read_text().strip()
require(memory == str(20 * 1024**3), "Native runner must have a 20 GiB memory limit")
quota, period = (cgroups / "cpu.max").read_text().split()
require(quota != "max" and 0 < int(quota) <= 6 * int(period),
        "Native runner must have a CPU quota of at most six cores")
filesystem = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", "/actions"], text=True).strip()
require(filesystem in {"ext4", "xfs"}, "Native Docker storage requires an ext4 or XFS worker volume")
bridge = ipaddress.ip_network("192.168.240.0/24")
pool = ipaddress.ip_network("192.168.248.0/21")
for route in json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show"], text=True)):
    destination = route.get("dst", "default")
    if destination != "default":
        network = ipaddress.ip_network(destination, strict=False)
        require(not network.overlaps(bridge) and not network.overlaps(pool),
                "Private Docker bridge/address pool overlaps an existing worker route")
report = {"schema": 1, "image": marker, "uid": os.getuid(), "gid": os.getgid(),
          "work_root": "/actions/_work", "build_root": "/sonic",
          "memory_limit_bytes": int(memory), "cpu_quota": int(quota), "cpu_period": int(period),
          "docker_storage_filesystem": filesystem, "docker_bridge": str(bridge),
          "make_jobs": 1, "package_jobs": 2 if os.environ.get("SONIC_VS_REUSE_ONLY") == "y" else 4}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY

stage=kernel-sandbox
# Exercise the producer's pinned helper as the build user, including its
# mounts: namespace creation alone does not establish sandbox readiness.
export BAZELISK_HOME="$vs_bazel_cache/bazelisk"
if timeout --kill-after=5s 300s python3 -B "$workspace/tools/bazel/ci/check_kernel_sandbox.py" \
    > "$artifacts/kernel-sandbox.log" 2>&1; then
    python3 - "$artifacts/slave-preparation.json" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
report = json.loads(path.read_text())
report["kernel_sandbox_probe"] = {"uid": 1000, "status": "passed"}
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY
else
    probe_status=$?
    tail -n 20 "$artifacts/kernel-sandbox.log" >&2
    die "The pinned kernel linux-sandbox failed for UID 1000 (probe exit $probe_status). Check the runner-scoped AppArmor profile sonic-slave-build and its user-namespace permission."
fi

stage=checkout
cd "$workspace"
# Checkout already initialized the exact recorded revisions. Do not fetch or
# update submodules here, especially after the build revision has been selected.
git submodule status --recursive > "$artifacts/submodule-revisions.txt"
if [[ ! -s $artifacts/submodule-revisions.txt ]] ||
   LC_ALL=C grep -q '^[+U-]' "$artifacts/submodule-revisions.txt"; then
    die 'Every submodule must already be initialized at its recorded revision.'
fi
if mountpoint -q /sonic; then
    [[ $(stat -c '%d:%i' /sonic) == "$(stat -c '%d:%i' "$workspace")" ]] ||
        die '/sonic is already mounted from another directory.'
else
    [[ -d /sonic && ! -L /sonic && -z $(find /sonic -mindepth 1 -maxdepth 1 -print -quit) ]] ||
        die '/sonic must be an empty image directory before binding the checkout.'
    sudo -n mount --bind "$workspace" /sonic
fi
cd /sonic
[[ -w /sonic && $(stat -c '%d:%i' /sonic) == "$(stat -c '%d:%i' "$workspace")" ]] ||
    die 'The writable /sonic checkout binding could not be verified.'
git rev-parse HEAD > "$artifacts/preparation-revision.txt"
ulimit -Sn 524288

if [[ $reuse_only == y ]]; then
    # The same owned supervisor retains this proof, starts its private daemon,
    # and supplies the exact hash to the second bounded user subprocess.
    stage=before-daemon
    exit 0
fi
stage=docker
sudo -n python3 tools/bazel/ci/prepare_nested_docker.py > "$artifacts/private-cgroup.json"
docker_root=/actions/sonic-native-docker
[[ ! -e $docker_root && ! -e $runner_temp/sonic-dockerd.pid ]] ||
    die 'Private Docker state already exists; this preparation requires a fresh worker.'
sudo -n install -d -m 0711 "$docker_root" /run/sonic-native-docker
export DOCKER_HOST=unix:///var/run/docker.sock
export DOCKER_CONFIG="$runner_temp/sonic-docker-client"
install -d -m 0700 "$DOCKER_CONFIG"
printf '{}\n' > "$runner_temp/sonic-dockerd.json"
chmod 0600 "$runner_temp/sonic-dockerd.json"
# No inherited credentials or host daemon configuration enter this daemon.
# Its root is on the worker volume, avoiding overlay-on-overlay storage.
sudo -n /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    "$(command -v dockerd)" --config-file "$runner_temp/sonic-dockerd.json" \
    --host "$DOCKER_HOST" --group docker --data-root "$docker_root" \
    --exec-root /run/sonic-native-docker --pidfile "$runner_temp/sonic-dockerd.pid" \
    --storage-driver overlay2 --exec-opt native.cgroupdriver=cgroupfs \
    --bip 192.168.240.1/24 --default-address-pool base=192.168.248.0/21,size=24 \
    > "$artifacts/dockerd.log" 2>&1 &
ready=n
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
    if timeout 3s docker info >/dev/null 2>&1; then ready=y; break; fi
    sleep 1
done
if [[ $ready != y || ! -s $runner_temp/sonic-dockerd.pid ]]; then
    tail -n 40 "$artifacts/dockerd.log" >&2
    die 'Private Docker did not become ready within 120 seconds.'
fi
[[ $(docker info --format '{{.DockerRootDir}}') == "$docker_root" ]] || die 'Docker is using an unexpected data root.'
[[ $(docker info --format '{{.Driver}}') == overlay2 ]] || die 'Private Docker is not using overlay2.'
docker version --format '{{json .Server}}' > "$artifacts/private-docker-version.json"

else
    # No preparation producer or second daemon is entered in this phase. The
    # supervisor binds the first phase's exact records before calling us.
    artifacts="$workspace/artifacts/vs"
    vs_work="$runner_temp/vs-working-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}-${RUNNER_NAME:?}"
    vs_dpkg_cache="$vs_work/packages"
    vs_bazel_cache="$vs_work/bazel"
    export TMPDIR="$vs_work/tmp"
    [[ ${DOCKER_HOST:-} == unix:///var/run/docker.sock && -n ${DOCKER_CONFIG:-} &&
       -z ${DOCKER_CONTEXT:-}${DOCKER_TLS_VERIFY:-}${DOCKER_CERT_PATH:-} ]] ||
        die 'Expected only the owned supervisor Docker endpoint.'
    python3 -I -S -B - "$artifacts" "$vs_work" \
        "${SONIC_VS_PREPARATION_SHA256:?}" "${SONIC_VS_STORAGE_SHA256:?}" <<'PY_VS_RESUME'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

artifacts, working = Path(sys.argv[1]), Path(sys.argv[2])
records = {}
for name, digest in zip(("slave-preparation.json", "k3s-storage.json"), sys.argv[3:]):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit("Invalid supervised preparation digest")
    path = artifacts / name
    if path.resolve() != path:
        raise SystemExit("Preparation record path is not canonical")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != 1000 or not 0 < before.st_size <= 262144:
            raise SystemExit("Preparation record is not bounded worker-owned data")
        body = stream.read(262145)
        after = os.fstat(stream.fileno())
        stable = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                               info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stable(before) != stable(after) or len(body) != before.st_size or hashlib.sha256(body).hexdigest() != digest:
            raise SystemExit("Supervised preparation record changed")
        records[name] = json.loads(body)
preparation, storage = records["slave-preparation.json"], records["k3s-storage.json"]
if preparation.get("status") != "prepared" or preparation.get("stage") != "before-daemon":
    raise SystemExit("First supervised preparation phase did not pass")
if storage.get("status") != "passed" or storage.get("working_root") != str(working):
    raise SystemExit("Fresh working-state proof differs")
for key, variable in (("worker", "RUNNER_NAME"), ("run", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT")):
    if storage.get(key) != os.environ[variable]:
        raise SystemExit("First phase worker/run/attempt differs")
for path in (working, working / "packages", working / "bazel", working / "tmp"):
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 or info.st_gid != 1000:
        raise SystemExit("Fresh working directory identity differs")
PY_VS_RESUME
    mountpoint -q /sonic && [[ $(stat -c '%d:%i' /sonic) == "$(stat -c '%d:%i' "$workspace")" ]] ||
        die 'The supervised /sonic binding differs from the frozen checkout.'
    cd /sonic
    ulimit -Sn 524288
    stage=configuration
    trap finish EXIT
fi

stage=configuration
# Makefile.work normally supplies this identity. Native package recipes such
# as ifupdown2 create temporary Git repositories and need it in the build user
# configuration, which lives only in this ephemeral worker.
git config --global user.name "${SONIC_BUILD_GIT_NAME:?Set the build Git name}"
git config --global user.email "${SONIC_BUILD_GIT_EMAIL:?Set the build Git email}"
git var GIT_AUTHOR_IDENT >/dev/null
git var GIT_COMMITTER_IDENT >/dev/null
# Configure selects VS below and records it in .platform. Runtime Python code
# interprets a nonempty PLATFORM as a hardware identity, so do not leak the
# build selector into package tests (the normal configured wrapper leaves it
# empty as well).
unset PLATFORM
export PLATFORM_ARCH=amd64 TARGET_BOOTLOADER=grub
export BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y
export ENABLE_ASAN=n ENABLE_SYNCD_RPC=n CROSS_BUILD_ENVIRON=n MULTIARCH_QEMU_ENVIRON=n
export PREFER_DEBIAN_REPO="${PREFER_DEBIAN_REPO:-n}"
export DOCKER_USERNAME=sonic DOCKER_USERTAG=latest DOCKER_LOCKDIR=/tmp/docklock
export SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=y SONIC_CONFIG_USE_DOCKER_CACHE=y
export SONIC_DPKG_CACHE_SOURCE="$vs_dpkg_cache"
if [[ $reuse_only == y ]]; then
    # The planner exported both none policies before this preparation. Keep them.
    [[ $SONIC_DPKG_CACHE_METHOD == none && $SONIC_DPKG_CACHE_METHOD_OVERRIDE == none &&
       $SONIC_BAZEL_CACHE_POLICY == remote-only ]] || die 'Early cache policy changed.'
    # The admitted assembly has no Bazel action. Refuse an unexpected routed invocation.
    # This is a backstop; the independent configured-input/admission checks remain required.
    export BAZEL=/usr/bin/false BAZEL_REAL=/usr/bin/false
    export BAZEL_CONTAINER_CACHE_DIR="$vs_bazel_cache" BAZEL_SWSS_CACHE_DIR="$vs_bazel_cache"
else
    export SONIC_DPKG_CACHE_METHOD=rwcache
fi
export SONIC_BAZEL_CACHE_SOURCE="$vs_bazel_cache" BAZELISK_HOME="$vs_bazel_cache/bazelisk"
export SONIC_DEFAULT_CONTAINER_REGISTRY='' DEFAULT_CONTAINER_REGISTRY=''
# The minimal installer rootfs installs CA certificates after its first apt
# update. Keep Make's HTTP bootstrap transport and signed APT metadata checks,
# using the public Debian mirrors instead of the default Traffic Manager mirror.
export MIRROR_URLS=http://deb.debian.org/debian/
export MIRROR_SECURITY_URLS=http://deb.debian.org/debian-security/
export SONIC_BUILD_JOBS=1 SONIC_BUILD_MEMORY=20g
# rules/config assigns these with '='; MAKEFLAGS gives the bounded values the
# same precedence as the outer wrapper's command-line assignments.
export MAKEFLAGS='SONIC_CONFIG_BUILD_JOBS=1 SONIC_CONFIG_MAKE_JOBS=4'
if [[ $reuse_only == y ]]; then
    export MAKEFLAGS='SONIC_CONFIG_BUILD_JOBS=1 SONIC_CONFIG_MAKE_JOBS=2'
    : "${BUILD_NUMBER:?}" "${BUILD_TIMESTAMP:?}" "${SOURCE_DATE_EPOCH:?}" "${BUILD_HOSTNAME:?}" "${SONIC_IMAGE_VERSION:?}"
else
    export BUILD_NUMBER="${GITHUB_RUN_NUMBER:?}" BUILD_TIMESTAMP="$(date -u +%Y%m%d.%H%M%S)"
    export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)" BUILD_HOSTNAME="$(hostname)"
fi
export SLAVE_DIR=sonic-slave-trixie DISTRO=trixie IMAGENAME=sonic-slave-trixie
export SONIC_BUILD_TARGET=target/sonic-vs.bin
export SLAVE_TAG="$(python3 -c 'import json; print(json.load(open("/etc/sonic-slave-github-runner.json"))["base_image"].split(":")[1])')"
mkdir -p "$DOCKER_LOCKDIR" "$vs_dpkg_cache/vcache" "$vs_bazel_cache/bazelisk"
# The root build hook creates its package-cache path before Make's configure
# recipe runs. Keep the checkout's output directories owned by the build user.
mkdir -p "target/vcache/$IMAGENAME/deb" target/versions dpkg
profile=(BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y ENABLE_ASAN=n ENABLE_SYNCD_RPC=n)

# Resolve the repository's defaults without entering either outer Makefile.
# The wrapper normally exports these feature settings through command-line
# assignments; native package submakes need the same resolved values.
settings=$(make --no-print-directory -s -f rules/config -f - .vs-preparation-defaults <<'MAKE'
-include rules/config.organization
-include rules/config.user
$(info VS_DEFAULT:TRUSTED_GPG_URLS=$(TRUSTED_GPG_URLS))
$(info VS_DEFAULT:SONIC_VERSION_CONTROL_COMPONENTS=$(SONIC_VERSION_CONTROL_COMPONENTS))
$(info VS_DEFAULT:SONIC_VERSION_CACHE=$(filter-out none,$(SONIC_VERSION_CACHE_METHOD)))
$(info VS_DEFAULT:SONIC_VERSION_CACHE_SOURCE=$(SONIC_VERSION_CACHE_SOURCE))
$(info VS_DEFAULT:BUILD_PACKAGES_URL=$(BUILD_PACKAGES_URL))
$(info VS_DEFAULT:BUILD_PUBLIC_URL=$(BUILD_PUBLIC_URL))
$(info VS_DEFAULT:BUILD_SNAPSHOT_URL=$(BUILD_SNAPSHOT_URL))
$(info VS_DEFAULT:MIRROR_SNAPSHOT=$(MIRROR_SNAPSHOT))
$(info VS_DEFAULT:PIP_HTTP_TIMEOUT=$(PIP_HTTP_TIMEOUT))
$(info VS_DEFAULT:ENABLE_TRANSLIB_WRITE=$(ENABLE_TRANSLIB_WRITE))
$(info VS_DEFAULT:ENABLE_NATIVE_WRITE=$(ENABLE_NATIVE_WRITE))
$(info VS_DEFAULT:ENABLE_HOST_SERVICE_ON_START=$(ENABLE_HOST_SERVICE_ON_START))
.PHONY: .vs-preparation-defaults
.vs-preparation-defaults: ; @:
MAKE
)
while IFS= read -r line; do
    [[ $line == VS_DEFAULT:* ]] || continue
    setting=${line#VS_DEFAULT:}
    export "$setting"
done <<< "$settings"
bash scripts/generate_buildinfo_config.sh
if [[ $reuse_only == y ]]; then
    # Existing canonical verify checks actual worker copy and source freshness.
    # It never enters the DEB constructor or creates synthetic cache metadata.
    make --no-print-directory -C src/sonic-build-hooks verify 2>&1 | tee "$artifacts/build-hooks.log"
else
    make --no-print-directory -C src/sonic-build-hooks all 2>&1 | tee "$artifacts/build-hooks.log"
fi
# slave.mk refreshes its runtime buildinfo from this directory on every parse.
mkdir -p "$SLAVE_DIR/buildinfo"
cp -a src/sonic-build-hooks/buildinfo/. "$SLAVE_DIR/buildinfo/"
sudo -n install -d /usr/local/share/buildinfo/config
sudo -n cp src/sonic-build-hooks/buildinfo/sonic-build-hooks_1.0_all.deb /usr/local/share/buildinfo/
sudo -n cp src/sonic-build-hooks/buildinfo/config/buildinfo.config /usr/local/share/buildinfo/config/
sudo -n dpkg -i /usr/local/share/buildinfo/sonic-build-hooks_1.0_all.deb >> "$artifacts/build-hooks.log" 2>&1
scripts/versions_manager.py generate -t "$runner_temp/native-buildinfo-versions" \
    -n build-sonic-slave-trixie -d trixie -a amd64
sudo -n rm -rf /usr/local/share/buildinfo/versions
sudo -n cp -a "$runner_temp/native-buildinfo-versions" /usr/local/share/buildinfo/versions
sudo -n touch /usr/local/share/buildinfo/versions/versions-deb /usr/local/share/buildinfo/versions/versions-web
sudo -n env IMAGENAME=sonic-slave-trixie DISTRO=trixie pre_run_buildinfo sonic-slave-trixie \
    >> "$artifacts/build-hooks.log" 2>&1
make -f slave.mk configure PLATFORM=vs PLATFORM_ARCH=amd64 "${profile[@]}" \
    2>&1 | tee "$artifacts/configure.log"

stage=profile
make --no-print-directory -s -f slave.mk -f - .vs-preparation-profile "${profile[@]}" \
    > "$runner_temp/native-make-profile.txt" <<'MAKE'
$(info VS_PROFILE:BLDENV=$(BLDENV))
$(info VS_PROFILE:PLATFORM=$(CONFIGURED_PLATFORM))
$(info VS_PROFILE:ARCH=$(CONFIGURED_ARCH))
$(info VS_PROFILE:MAKE_JOBS=$(SONIC_BUILD_JOBS))
$(info VS_PROFILE:PACKAGE_JOBS=$(SONIC_CONFIG_MAKE_JOBS))
$(info VS_PROFILE:PREFER_DEBIAN_REPO=$(PREFER_DEBIAN_REPO))
$(info VS_PROFILE:BOOKWORM=$(filter $(SONIC_BOOKWORM_DOCKERS) $(SONIC_BOOKWORM_DBG_DOCKERS),$(DOCKER_IMAGES_FOR_INSTALLERS) $(EXTRA_DOCKER_TARGETS) $(SONIC_PACKAGES_LOCAL)))
$(info VS_PROFILE:INSTALLER_DOCKERS=$(DOCKER_IMAGES_FOR_INSTALLERS))
$(info VS_PROFILE:BAZEL_DOCKERS=$(SONIC_BAZEL_DOCKER_IMAGES) $(SONIC_BAZEL_DBG_DOCKER_IMAGES))
.PHONY: .vs-preparation-profile
.vs-preparation-profile:
	@printf 'VS_PROFILE:RUNTIME_PLATFORM=%s\n' "$${PLATFORM:-}"
MAKE
python3 - "$runner_temp/native-make-profile.txt" "$artifacts/slave-preparation.json" <<'PY'
import json
import os
from pathlib import Path
import sys
settings = dict(line.removeprefix("VS_PROFILE:").split("=", 1)
                for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith("VS_PROFILE:"))
for key, expected in {"BLDENV": "trixie", "PLATFORM": "vs", "ARCH": "amd64",
                      "MAKE_JOBS": "1", "PACKAGE_JOBS": "2" if os.environ.get("SONIC_VS_REUSE_ONLY") == "y" else "4", "BOOKWORM": "",
                      "PREFER_DEBIAN_REPO": os.environ["PREFER_DEBIAN_REPO"],
                      "RUNTIME_PLATFORM": ""}.items():
    if settings.get(key, "<missing>").strip() != expected:
        raise SystemExit(f"Unexpected native Make profile {key}: {settings.get(key)!r}")
selected = set(settings["BAZEL_DOCKERS"].split())
required = {f"docker-{image}{debug}.gz" for image in ("orchagent", "syncd-vs") for debug in ("", "-dbg")}
if not required <= selected:
    raise SystemExit("The requested VS profile must select both Bazel runtime/debug image pairs")
path = Path(sys.argv[2])
report = json.loads(path.read_text())
report["make_profile"] = settings
report["source_revision"] = Path("artifacts/vs/preparation-revision.txt").read_text().strip()
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
PY

# This file contains only the named build settings, never a dump of the runner
# environment. Workflow steps source it before entering /sonic.
stage=environment
environment_file="$runner_temp/sonic-vs-slave.env"
(umask 077; printf 'unset PLATFORM\n' > "$environment_file")
environment_names=()
for variable in PLATFORM_ARCH TARGET_BOOTLOADER BUILD_WITH_BAZEL_WHEN_AVAILABLE \
    INCLUDE_VS_DASH_SAI INCLUDE_FIPS ENABLE_ASAN ENABLE_SYNCD_RPC CROSS_BUILD_ENVIRON \
    PREFER_DEBIAN_REPO \
    MULTIARCH_QEMU_ENVIRON DOCKER_HOST DOCKER_CONFIG DOCKER_USERNAME DOCKER_USERTAG DOCKER_LOCKDIR \
    SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD SONIC_CONFIG_USE_DOCKER_CACHE \
    SONIC_DPKG_CACHE_SOURCE SONIC_DPKG_CACHE_METHOD SONIC_BAZEL_CACHE_SOURCE BAZELISK_HOME \
    SONIC_DEFAULT_CONTAINER_REGISTRY DEFAULT_CONTAINER_REGISTRY MIRROR_URLS MIRROR_SECURITY_URLS \
    SONIC_BUILD_JOBS SONIC_BUILD_MEMORY MAKEFLAGS BUILD_NUMBER BUILD_TIMESTAMP SOURCE_DATE_EPOCH BUILD_HOSTNAME \
    SLAVE_DIR SLAVE_TAG DISTRO IMAGENAME SONIC_BUILD_TARGET TRUSTED_GPG_URLS \
    SONIC_VERSION_CONTROL_COMPONENTS SONIC_VERSION_CACHE SONIC_VERSION_CACHE_SOURCE \
    BUILD_PACKAGES_URL BUILD_PUBLIC_URL BUILD_SNAPSHOT_URL MIRROR_SNAPSHOT PIP_HTTP_TIMEOUT \
    ENABLE_TRANSLIB_WRITE ENABLE_NATIVE_WRITE ENABLE_HOST_SERVICE_ON_START; do
    export "$variable"
    environment_names+=("$variable")
    printf 'export %s=%q\n' "$variable" "${!variable}" >> "$environment_file"
done
if [[ $reuse_only == y ]]; then
    for variable in SONIC_DEB_BUILD_POLICY SONIC_BAZEL_KERNEL_PREPARE SONIC_BAZEL_VS_ARCHIVE_PROFILE SONIC_IMAGE_VERSION \
        SONIC_DPKG_CACHE_METHOD_OVERRIDE SONIC_BAZEL_CACHE_POLICY BAZEL BAZEL_REAL TMPDIR \
        BAZEL_CONTAINER_CACHE_DIR BAZEL_SWSS_CACHE_DIR; do
        export "$variable"
        environment_names+=("$variable")
        printf 'export %s=%q\n' "$variable" "${!variable}" >> "$environment_file"
    done
fi
# The owned supervisor consumes data, without evaluating this shell file.
python3 -I -S -B - "$environment_file.json" "${environment_names[@]}" <<'PY_ENVIRONMENT'
import json
import os
import sys
names = sys.argv[2:]
if len(names) != len(set(names)):
    raise SystemExit("Duplicate VS environment setting")
record = {"schema": 1, "unset": ["PLATFORM"],
          "environment": {name: os.environ[name] for name in names}}
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump(record, stream, sort_keys=True)
    stream.write("\n")
PY_ENVIRONMENT
printf 'ulimit -Sn 524288\n' >> "$environment_file"
stage=complete
printf 'Prepared native AMD64 Trixie VS build at /sonic; source %s before Make.\n' "$environment_file"
