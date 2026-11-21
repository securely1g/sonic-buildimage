# Installing the isolated VS cleanup supervisor

This is an opt-in implementation for the [one-host pilot](https://github.com/securely1g/sonic-buildimage/blob/9ddf0ab06cd196c541a8d5316ef99a8348e55c68/tools/ci/runner/vs-cleanup-supervisor-handoff.md),
not a fleet deployment. Production routing and cleanup are unchanged. There is no
claimed installer speedup until the same-host A/B/C benchmark is complete.

The root-owned supervisor registers one ephemeral runner with only
`vs-cleanup-canary`, waits for its listener and worker to exit, checks the final
GitHub **job** result, performs guarded cleanup, then admits a successor. The
completed-job hook is not used. A synchronous socket acknowledgment follows an
fsynced host journal. If enrollment fails or deferral is disabled, existing
workflow cleanup remains active.

## Preconditions

- Protect the selected VMSS instance from scale-in and scale-set actions. Exclude
  it from instance-targeted provisioning, guest maintenance and runner restart
  scripts. Do not rerun the normal VMSS setup script on it during this pilot:
  that script can restart the original listener and reconfigure Docker.
- Drain production routing, finish the active job and cleanup, and record the
  original service, labels, account, work directory, hooks and restart policy.
  Stop and disable that exact service; inhibit competing startup paths.
- Reserve the Docker daemon. No other GitHub/Azure DevOps listener, worker or
  container may remain, including stopped unrelated containers. Attribute them
  before any operator removal; the supervisor does not delete unrelated objects.
- Use trusted source only. Docker-group access is root-equivalent: this host
  arrangement is **not** a security boundary against malicious workflow code.
  Do not route fork code or unrelated jobs to this label.
- Have Python 3.11+, Docker, systemd, `runuser` and `sudo` available. Keep the
  runner's existing non-root account and its required Docker/cache permissions.
  Use the dedicated mounted build disk, not `/opt` or the temporary OS disk,
  for the Actions work root.

## Prepare paths and credentials

On the VM, preserve the original runner installation. The separate canary runner
package may remain in `/opt/actions-runner-vs-canary`, owned by its non-root
account. The examples below use `azureuser`; substitute the verified account.

```bash
sudo install -d -o azureuser -g azureuser -m 0700 \
  /mnt/build/actions-runner-vs-canary-work
sudo install -d -m 0700 /etc/sonic-vs-cleanup /var/lib/sonic-vs-cleanup
```

If you already configured the new canary with `--work _work` under `/opt`,
remove **only that canary registration** using its `config.sh remove` and a
fresh removal token from the matching registration scope. Never edit `.runner`
by hand or remove the original runner. The supervisor will configure a fresh
registration with the correct absolute work root. Alternatively, an already
configured canary is accepted if its name, ephemeral setting, work root and
single label match the host configuration.

Provision `/etc/sonic-vs-cleanup/github-token` through your private credential
mechanism, root-owned mode `0600`. Do not commit the token, put it in a job
environment, copy workstation GitHub authentication, or run credential commands
with shell tracing enabled. The credential must permit:

- Managing runners at the selected repository or organization scope.
- Reading Actions jobs and artifacts in `securely1g/sonic-buildimage`.

The host exchanges this credential for a fresh short-lived registration token
before each registration. It is not a Docker/ACR credential. Rotate the host
credential according to your provisioning policy.

Create root-owned `/etc/sonic-vs-cleanup/config.json`, mode `0600`:

```json
{
  "repository": "securely1g/sonic-buildimage",
  "registration_scope": "repo",
  "token_file": "/etc/sonic-vs-cleanup/github-token",
  "runner_user": "azureuser",
  "runner_dir": "/opt/actions-runner-vs-canary",
  "runner_name": "YOUR_VERIFIED_CANARY_NAME",
  "original_service": "YOUR_ORIGINAL_actions.runner.NAME.service",
  "work_root": "/mnt/build/actions-runner-vs-canary-work",
  "build_mount": "/mnt/build",
  "nfs_package_cache": "/nfs/dpkg_cache/vs",
  "min_free_bytes": 274877906944,
  "job_name": "VS installer in SONiC slave (AMD64)",
  "completion_timeout": 300,
  "defer_cleanup": false,
  "reclaim_caches": false
}
```

Replace both uppercase placeholders with the recorded names. For an
organization-scoped runner, set `registration_scope` to `org`; the repository
still identifies the job/artifact API. Set the free-disk threshold to the
approved VS preflight requirement. Do not weaken admission simply to start a job.

## Install the reviewed revision

From a reviewed checkout of this PR, install only the following files. The
supervisor and helpers must remain root-owned outside Actions workspaces.
Do not execute the supervisor directly from a job-writable checkout.

```bash
sudo install -d -m 0755 /opt/sonic-vs-cleanup/tools/ci/runner \
  /opt/sonic-vs-cleanup/tools/bazel/ci
sudo install -m 0644 tools/ci/runner/vs_cleanup_supervisor.py \
  tools/ci/runner/vs_cleanup_client.py /opt/sonic-vs-cleanup/tools/ci/runner/
sudo install -m 0644 tools/bazel/ci/cleanup_vs_workspace.py \
  tools/bazel/ci/cleanup_runner_bazel_cache.py /opt/sonic-vs-cleanup/tools/bazel/ci/
sudo install -m 0644 tools/ci/runner/sonic-vs-cleanup.service \
  /etc/systemd/system/sonic-vs-cleanup.service
sudo systemctl daemon-reload
```

Keep it disabled at boot until the pilot passes. The service deliberately has
`Restart=no` and `KillMode=control-group`: a failure does not silently reopen
admission, and stopping the service stops its owned runner processes.

## First controlled job: retain workflow cleanup

Keep `defer_cleanup=false` and `reclaim_caches=false`. Start the service manually
on the selected VM:

```bash
sudo systemctl start sonic-vs-cleanup.service
sudo journalctl -u sonic-vs-cleanup.service --follow
```

Wait for the new native `Listening for Jobs` line. On your admin workstation,
dispatch the existing workflow at the reviewed implementation branch:

```bash
gh workflow run bazel-oci.yml --repo securely1g/sonic-buildimage \
  --ref ci/vs-cleanup-supervisor \
  -f skip_vs=false -f vs_cleanup_canary=true
```

Only the native `vs_container` job changes routing. Its producer and validation
jobs retain their normal runners. Keep those jobs available. Workflow dispatch
of this existing workflow requires it to exist on the default branch.

The supervisor acknowledges enrollment but reports deferral disabled, so the
job retains its existing cache, builder and checkout cleanup. Verify the job
ran under the expected account, host preflight/Docker/cache checks passed,
uploads and registry logout completed, and the listener exited. The service
admits a new ephemeral registration only after finishing the previous record.
No separate probe workflow is required.

## Enable deferred cleanup only after validation

Drain the canary, stop its supervisor when idle, and set `defer_cleanup=true`
and `reclaim_caches=true` in the root-owned config. Restart and dispatch a
controlled build. Do not make these settings production defaults.

Before the native build, the workflow enrolls the exact run and attempt plus
the checkout's device/inode with the host. It skips workflow reclamation only
after successful durable enrollment and explicit host permission to defer.
Artifact uploads, registry logout and private Docker credential erasure remain
inside the job. Checkout eligibility is separately authorized after the required
uploads and successful steps. The supervisor independently checks the final
job result (including Action post steps), runner ID, exact job name and attempt,
and published artifact ownership before deleting a successful checkout.

After listener/worker exit, cleanup removes only the exact repository/run/
attempt/VS-labeled containers. It does not remove volumes or unrelated
containers. Checkout cleanup reuses mount/Docker-reference guards and additionally
checks the enrolled directory identity. Failed, cancelled, unknown-result or
unauthorized checkouts remain. Selected local caches are reclaimed only after
NFS validation, and unused Docker images/build cache are pruned only on the
exclusive daemon. Shared NFS caches and runner diagnostic logs are retained.

## Recovery and acceptance

Private records live under `/var/lib/sonic-vs-cleanup`. `pending.json` is
atomically replaced and fsynced; records include listener exit when observed,
cleanup start and completion time. Preserve these records and runner `_diag` logs for the pilot.
Forward logs using the provisioning owner's trusted mechanism.

Any failure leaves the host out of service. Check `journalctl`, the private
record and the host state, fix the underlying problem, then restart the service.
It recovers pending cleanup **before** registration/admission. To recover without
starting a new listener, first stop the service, verify all listeners/workers are
gone, create the runtime directory if systemd removed it, and run:

```bash
sudo install -d -m 0755 /run/sonic-vs-cleanup
sudo python3 -B /opt/sonic-vs-cleanup/tools/ci/runner/vs_cleanup_supervisor.py \
  --recover-only
```

Unknown job outcomes, an interrupted session without enrollment, changed
checkout identity or an unidentifiable job require operator investigation.
Never delete the pending record just to reopen admission. For an unenrolled
session, verify the final job outcome and existing workflow cleanup, preserve
all uncertain evidence, and archive the record before an explicitly approved
reset. No automated broad cleanup or cache-outage fallback is provided.

Before enabling automatic startup or proceeding beyond the canary, exercise
success, failed upload, failed Action post step, cancellation, termination/reboot
with pending cleanup, NFS failure, mounted/replaced checkout and a second job
queued during cleanup. A queued successor must remain unaccepted until cleanup
and admission pass. Record GitHub job completion, cleanup start/end and the
successor's fresh readiness event separately. Measure the same-host A/B/C arms
from the handoff; validate the installer artifact and reclaimed disk.

For rollback, drain and stop the canary, recover pending work, remove only its
registration, and restore the original service, hooks, labels and restart policy.
Verify a production probe before restoring the VMSS instance's original
protection settings and provisioning participation.
