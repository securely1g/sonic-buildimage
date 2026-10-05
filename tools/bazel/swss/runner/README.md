# One-job VS build runner

These scripts provision a dedicated Linux x86_64 host and arm one GitHub Actions
runner for one full VS image job. Defaults target PR #9 in
`securely1g/sonic-buildimage`; pass `--repo owner/name --pr NUMBER` for another PR.
The workflow routes PR jobs to `sonic-vs-source-pr-NUMBER` and push/manual jobs
to `sonic-vs-source-master`. A runner gets only its selected custom label plus
GitHub's default `self-hosted`, `linux`, `x64` labels.

## Prepare a host once

Use Ubuntu 22.04 or 24.04 for the documented SONiC host baseline. The initial
host uses Ubuntu 26.04; passing preflight does not establish full compatibility.
Enable hardware virtualization and KVM, load `overlay`, and install Docker
Engine with buildx following the [Docker Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).
Also install `ca-certificates`, `curl`, `wget`, `git`, `make`, `python3`, `python3-venv`,
`kmod`, `procps`, `util-linux`, and the [GitHub CLI](https://cli.github.com/).
For Ubuntu 24.04/26.04, the distribution-package preparation command is:

```sh
sudo apt-get update
sudo apt-get install docker.io docker-buildx ca-certificates curl wget git make python3 python3-venv kmod procps util-linux liblttng-ust1t64 libicu-dev
sudo systemctl enable --now docker
sudo modprobe kvm
sudo modprobe overlay
```

If Docker CE is already installed, keep that installation instead of installing
`docker.io`; use its `docker-buildx-plugin`. Ubuntu 22.04 uses `liblttng-ust1`
instead of `liblttng-ust1t64`; use Docker's linked instructions for its buildx
installation. Install `gh` separately using its linked instructions, and verify
that `/dev/kvm` exists after enabling CPU virtualization in the host firmware.
`make init` uses host `wget` to download the build hooks' trusted signing keys.

Use a local, persistent `/data` mount with at least 300 GiB free for workspaces.
Budget another 100 GiB free for Docker; if they share a filesystem, preflight
requires 400 GiB total. The Docker figure is this tool's conservative planning
allowance, not a measured maximum or a workflow requirement; the workflow itself
requires 300 GiB workspace space. Docker's containerd image store can also use
`/var/lib/containerd`; if separately mounted, budget and monitor that filesystem
as well. The initial host keeps both Docker and containerd on the root filesystem.
Prefer at least 12 GiB RAM available; the image installer VM alone requests 10 GiB.
Avoid running multiple full builds on the same host.

From a reviewed checkout, preview and then install:

```sh
bash tools/bazel/swss/runner/bootstrap.sh --dry-run
sudo bash tools/bazel/swss/runner/bootstrap.sh
sudo -u sonic-runner /usr/bin/python3 /opt/sonic-runner-tools/runner/preflight.py
```

Bootstrap creates `sonic-runner` with a locked password, no administrator group,
and `docker,kvm` membership. It verifies the pinned runner 2.337.0 archive using
its SHA-256, installs `jinjanator==25.3.1` in a root-owned virtual environment,
and exposes `j2` to systemd builds. It installs the operator scripts in a
root-owned location accessible to the runner. Runner runtime dependencies must
also be available; if `config.sh` reports a missing library, review and run the
verified archive's `bin/installdependencies.sh`, then retry.

SONiC uses privileged Docker, chroot, device nodes, loop mounts and a KVM VM.
A `/data` mount with `nodev,nosuid` can let checkout and compilation succeed but
break image assembly. Bootstrap adds a self-bind mount only at
`/data/sonic-runner`; `sonic-vs-workspace-permissions.service` explicitly remounts
that child with `dev,suid,exec` before the runner starts. An initial bind alone
can inherit restrictive flags. The parent `/data` flags remain unchanged.
Bootstrap backs up fstab before adding its entry and refuses a conflicting
existing entry. Preflight checks effective flags, Docker/buildx as the runner
user, workspace and Docker disk budgets, RAM, overlay, j2 rendering, KVM API,
and both the process and runner service file-creation masks.

The runner service uses `UMask=0022`, while its home and attempt directories
remain `0700`. A restrictive `0077` mask makes checked-out configuration files
`0600`; Docker copies preserve those modes while changing ownership to root.
The non-root builder then cannot read `/etc/pip.conf` or Docker APT sources,
causing misleading pip `externally-managed-environment` errors. After correcting
an existing service, use a fresh checkout and rebuild affected slave images:
changing the mask does not repair existing files, Docker layers, or containers.
Preserve evidence and remove only identified obsolete builder tags before retrying.

Some Ubuntu hosts also enforce an AppArmor profile for `/usr/bin/gs`, including
Ghostscript executed inside a privileged builder. Bash manual generation writes
under `/sonic`, outside that profile's default permitted directories. If the
standard `gs` profile is present and enabled, bootstrap preserves
`/etc/apparmor.d/local/gs`, adds one include of the root-owned
`/etc/apparmor.d/local/sonic-vs-gs` fragment, and reloads only `gs`. Its rule is
`owner /sonic/**.{ps,pdf} rw,`: access is limited to owned PS/PDF files under
the build mount. A differently structured profile fails with an instruction
to review it; AppArmor remains enabled. The `apparmor` package supplies the
parser when required. Preflight detects a loaded `gs` profile without the
managed configuration. This checks configuration files; after policy edits,
reload the profile and validate actual Ghostscript execution in the builder.

## Arm a runner for the next attempt

Run these as the normal operator who is already authenticated to GitHub with
repository runner administration access. Keep that login out of `sonic-runner`.
Inspect the queued runs and cancel obsolete attempts before arming; a PR label
selects the PR, not an individual commit or run.

```sh
gh auth status
gh run list --repo securely1g/sonic-buildimage --workflow bazel-swss-oci.yml --limit 10
python3 /opt/sonic-runner-tools/runner/rearm.py --pr 9 --dry-run
python3 /opt/sonic-runner-tools/runner/rearm.py --pr 9
```

Use `--master` for a push/manual job. Arm only after reviewing the PR code and
workflow that will run. For a failed attempt, retry the workflow with
`gh run rerun RUN_ID --failed --repo securely1g/sonic-buildimage`; workflow edits
require a new run on the updated commit, because rerunning uses the original
run's workflow. A new registration is required after each consumed job.
Pushing another commit to the PR cancels its active workflow, including a full
image build, because the workflow uses `cancel-in-progress`. Coordinate changes
before arming a long build. A cancelled job also consumes its one-job runner;
arm a new runner for the replacement run.
The workflow labels Make builder containers with its repository, run and attempt.
An `always()` cleanup step saves their container logs and removes only those
containers. Stopping the runner alone does not stop Docker containers. After a
host crash or interrupted cleanup, inspect `sudo docker ps` and the containers'
labels and workspace mounts before removing an identified orphan or rearming.

Rearm refuses a running listener/worker or an unfinished previous registration.
It rechecks the host, verifies the archive, extracts a fresh runner under
`/data/sonic-runner/attempts/TIMESTAMP-ID`, and registers with `--ephemeral`.
Only a short-lived registration token crosses to the runner, through standard
input and its environment rather than command arguments; the operator's
personal token and SSH agent are not copied. The service has `Restart=no` and
starts only when `.runner` exists. No automatic registration occurs at boot.
Rearm waits up to 90 seconds for GitHub to report that exact runner online or
busy. If it cannot verify the connection, it reports the attempt directory and
journal command; it leaves the service intact for diagnosis. Check whether the
runner already consumed a job before retrying.
GitHub runner auto-update remains enabled; update the archive version and its
verified checksum together when maintaining the bootstrap baseline.

## Observe and recover

```sh
gh run watch RUN_ID --repo securely1g/sonic-buildimage --exit-status
sudo journalctl -u sonic-vs-runner.service --since today
sudo systemctl status sonic-vs-runner.service
sudo du -h --max-depth=1 /data/sonic-runner/attempts
sudo docker system df
```

Keep `_diag` and `_work` inside the previous attempt until the failed run is
understood and uploaded artifacts/logs are saved. Rearm preserves them and uses
a fresh directory; it never recursively deletes a checkout or prunes Docker.
After a failure, inspect free space before retrying. Clean only identified,
inactive attempt directories and unused build images after saving evidence.

If the listener stopped before consuming a job, its `.runner` registration may
remain. First confirm the job is not running, then stop the service. Inspect
the repository runner list, remove only that stale runner ID from GitHub, and
clear its local registration before rearming:

```sh
sudo systemctl stop sonic-vs-runner.service
gh api repos/securely1g/sonic-buildimage/actions/runners --jq '.runners[] | {id,name,status,busy}'
# Replace RUNNER_ID only after identifying the inactive runner.
gh api --method DELETE repos/securely1g/sonic-buildimage/actions/runners/RUNNER_ID
sudo -u sonic-runner sh -c 'cd /data/sonic-runner/current && ./config.sh remove --local'
```

When adopting the initial host, stop and disable its old
`actions.runner.securely1g-sonic-buildimage.p330-sonic-vs-pr9.service` after its
job finishes. Do not leave two listeners active. The new service can coexist
with the preserved old `/data/sonic-runner/runner` directory.

## Gaps addressed and remaining limits

| Failure or gap | Repeatable protection |
| --- | --- |
| Generic label accepted an older unrelated queued job | Per-PR labels; inspect/cancel obsolete runs before arming |
| Interrupted submodule checkout left `.git/modules/.../HEAD` pointing at `.invalid` | Fresh runner directory for each registration; workflow checkout also uses run/attempt-specific paths |
| Missing host `j2` or service PATH differs from operator shell | Pinned root-owned virtual environment, explicit service PATH and render check |
| Restrictive service umask made copied pip/APT config root-only | `UMask=0022`, private runner directories, process/service umask checks, fresh checkout and rebuilt affected images |
| Root or inaccessible Docker/KVM | Dedicated account, groups, actual daemon and KVM ioctl checks |
| `/data` `nodev,nosuid` blocked device access in privileged containers | Scoped self-bind plus boot-time remount and effective mount-flag check |
| Docker consumes a different filesystem from checkout | Separate capacity checks and summed budget if shared |
| Ephemeral runner disappears after a job | Explicit one-command rearm, fresh token, no restart loop |
| Ephemeral registration leaves local state behind | Preserve diagnostics; explicit cleanup and host rebuild policy |
| Cancelled job left its build container compiling | Per-attempt container labels, saved container logs and cleanup on cancellation/failure |
| Builder cleanup deleted groff device files, breaking Bash manual generation | Preserve groff runtime data in slave images while retaining runtime-image cleanup |
| Host AppArmor blocked Ghostscript PDF output even in a privileged builder | Managed gs-only owner allowance for `/sonic/**.{ps,pdf}`, profile reload and preflight configuration check |
| Cached builder tags ignored changes to their installed build hooks | Include build-hook source content in builder tags so hook repairs rebuild cached environments |

Ephemeral registration does **not** erase the machine, Docker state, user home,
or other attempts. Docker group membership grants root-equivalent host access.
Use this machine only for reviewed, trusted code; for untrusted/public PRs use
a disposable VM or host and destroy it afterward. Repository labels are routing,
not a security boundary. No full host image/package lock, automatic cleanup,
network mirror, remote execution, or unattended runner autoscaler is provided.
Downloads still depend on GitHub, package registries and upstream repositories.
Fresh attempts have cold workspace caches; retained Docker layers
are not a complete build cache. SONiC's existing version machinery pins Debian
builder image digests and supplies versioned download mirrors and Python
constraints. This run still uses rolling APT repositories, and download/Python
fallback paths can accept different inputs. Runner provisioning does not make
those mechanisms strict or freeze the host package environment. Host checks do
not prove the full VS image builds, boots, or forwards traffic; track those
results independently.

Tool checks, requiring no sudo or GitHub token:

```sh
bash -n tools/bazel/swss/runner/bootstrap.sh
python3 -B -m unittest discover -s tools/bazel/swss/runner -p '*_test.py'
```
