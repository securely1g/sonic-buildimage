# Canary plan for VS runner cleanup after GitHub job completion

This needs the maintainer who can change the VS runner's host service or provisioning configuration. Start with one dedicated runner and keep the rest of the pool on its existing service.

The workflow-only PR retains the final Trixie `sonic-slave` container with `--rm=false`. The existing workflow removes it after artifact uploads, inside the job. The host-supervised alternative remains untested on a VS runner. [Workflow-only PR #283](https://github.com/securely1g/sonic-buildimage/pull/283)

## What runs today

| When | Current operation | Canary handling |
| --- | --- | --- |
| At job start | Validate shared NFS, prune unused Docker images and build cache, remove selected local Bazel and package caches, check disk/build prerequisites. | Keep admission checks. On the dedicated canary, reclaim eligible local disk after the preceding job. Validate NFS again before deleting the local package cache. |
| During the build | Make starts the final installer builder on the host Docker daemon. Other Make invocations retain their existing removal policy. | Limit deferred removal to the final Trixie VS installer builder. |
| After publication, inside the job | Remove containers with the exact repository/run/attempt/VS label, log out of the slave registry and erase the job’s private Docker config. Delete this run’s checkout only when artifact and job conditions permit. | Hand off labeled container and eligible checkout cleanup. Keep artifact publication, registry logout and credential erasure inside the job in every mode. |

The sampled host had one GitHub Actions systemd unit and successive builds reused a registration. The probe did not inspect its service command or configured hook values. The operator should verify both in the provisioning configuration. GitHub’s completed-job hook runs synchronously before the job completes. A new service boundary is needed to measure cleanup *after* job completion. [Workflow startup](https://github.com/securely1g/sonic-buildimage/blob/3bdfbe135e242832e04d499325211b55ded362de/.github/workflows/bazel-oci.yml#L796-L810) · [Workflow cleanup](https://github.com/securely1g/sonic-buildimage/blob/3bdfbe135e242832e04d499325211b55ded362de/.github/workflows/bazel-oci.yml#L1223-L1277) · [GitHub hook lifecycle](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts#triggering-the-scripts)

## Operator handoff

1. Obtain authorized access to the runner’s actual provisioning service. Drain and isolate one dedicated host, reserve its Docker daemon for the canary, and record the original unit, account, labels, work directory, pre/post hooks and restart policy. Route only the benchmark jobs there. Install the supervisor and cleanup helpers from a reviewed revision under administrator-owned paths outside Actions workspaces. Keep provisioning and registration credentials outside the job environment.
2. Start a single-job listener under that account. Before deferring removal, the job must durably enlist its repository, job, run ID and attempt with the host. Use the existing workflow cleanup if handoff is unavailable. Once enrolled, publish artifacts and authorize checkout deletion only after the current upload and success conditions hold.
3. Wait for the listener and worker to exit. Remove containers by their exact job label, then verify none remain. Delete only the authorized checkout after verifying a successful final job result, directory identity, mount safety and Docker references. Keep the checkout on failure, cancellation or unknown outcome. Reclaim selected caches and unused images only on the exclusive host. Preserve shared caches, named volumes and unrelated containers. Check whether automatic removal previously reclaimed any anonymous volumes.
4. Persist pending work across service crashes and host reboots. Complete or retry it before the next listener can accept a job. A failed cleanup leaves the host out of service with a recoverable record. Ensure the stock service or provisioning agent cannot start a competing listener. Test this with a second queued job.

Reuse the existing guarded checkout and local cache helpers from a trusted installation. Capture the job label, builder ID, checkout identity, artifact ID and cleanup results in the operator's log. [Checkout guard](https://github.com/securely1g/sonic-buildimage/blob/3bdfbe135e242832e04d499325211b55ded362de/tools/bazel/ci/cleanup_vs_workspace.py) · [Local cache guard](https://github.com/securely1g/sonic-buildimage/blob/3bdfbe135e242832e04d499325211b55ded362de/tools/bazel/ci/cleanup_runner_bazel_cache.py)

A persistent registration with `Runner.Listener run --once` can isolate a short experiment if the installed version supports it. Upstream warns that this flag will be deprecated. For a supported one-job registration, use `--ephemeral` or just-in-time provisioning. The stock systemd `runsvc.sh` launcher starts its own listener, so verify how the service passes a one-job setting through before running the trial. With ephemeral mode, obtain each successor registration through the provisioning service and forward runner logs before recycling the instance. [Listener implementation](https://github.com/actions/runner/blob/main/src/Runner.Listener/Runner.cs) · [GitHub guidance for ephemeral runners](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#ephemeral-runners-for-autoscaling)

## Same-host benchmark

| Arm | Final builder removal | Runner lifecycle |
| --- | --- | --- |
| A | Docker auto-removes it at Make exit. | Current service. |
| B | Existing workflow removes it after uploads. | Current service, workflow-only change. |
| C | Supervisor removes it after the listener exits. | Admit the next job after disk cleanup completes. |

Reuse the A/B results from PR #283 when the host, revision and inputs match. Otherwise rerun the control. Use identical source, build and upload settings, runner image, cache access and starting disk state. Record a host/boot fingerprint privately so a reused runner name cannot hide a replacement. Compare repeated runs in alternating order. Use job start as time zero and report separately:

- Final builder exit, installer artifact publication, GitHub job completion, cleanup start/end and readiness for another job. Verify the installer download/validation and record its size and digest.
- Measure readiness from the successor’s fresh native `Listening for Jobs` event or acceptance of a queued probe. Match its registration and session to the preceding cleanup. A stale `last-ready.json` describes a prior session. For A/B, a continuously connected listener may emit no new readiness line. A probe then gives an upper bound. Report queue delay separately.
- Record peak and final free disk, leftover labeled container IDs and checkout disposition. Inspect the final job result from the runner or GitHub because an upload can succeed before a later Action post step fails.

Complete success, failed upload, failed Action post step, cancellation, supervisor termination/restart, NFS failure, mount/checkout replacement and queued-successor tests. Advance only if the measured publication or job-time benefit justifies the measured time to next availability, with expected artifact, retention and disk results. For rollback, drain the canary, finish its pending cleanup with the same guards, restore the original service and routing, and verify it accepts a probe job.
