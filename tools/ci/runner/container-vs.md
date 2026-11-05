# Build the VS installer in a SONiC slave runner

The `VS installer in SONiC slave (AMD64)` job in `bazel-oci.yml` runs the
Actions worker inside the Trixie build container. It uses the
`sonic-slave-container` runner label and invokes the native build directly:

```bash
PREFER_DEBIAN_REPO=y make -f slave.mk target/sonic-vs.bin \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y \
  INCLUDE_VS_DASH_SAI=y \
  INCLUDE_FIPS=y \
  ENABLE_ASAN=n \
  ENABLE_SYNCD_RPC=n
```

This produces the ONIE installer. It does not create or boot BIOS/UEFI VM disks.
The registered Bazel container pairs also produce matching debug archives;
the normal installer continues to select the runtime archives. Packages without
Bazel support retain their existing Make producers.
CI explicitly sets `PREFER_DEBIAN_REPO=y` for Monit and rasdaemon to fetch their
pinned Debian source archives and packaging with SHA256 verification. The
default `n` preserves the existing Git source recipes. Both paths keep the
SONiC patches, Debian quilt patches, and runtime/debug package outputs.
Libyang3 retains its upstream test harness; its headless test and error-reporting
improvements are tracked separately in
[PR #83](https://github.com/securely1g/sonic-buildimage/pull/83).
DHCP relay retains the upstream submodule revision and existing test behavior.
Its test recipe suppresses failures, so installer success does not establish
that the DHCP6 tests passed. The isolated Redis fixture and failure-reporting
fixes remain a separate component change in
[sonic-dhcp-relay #1](https://github.com/securely1g/sonic-dhcp-relay/pull/1).

The native P4 dependencies still use their existing Make/Debian producers.
The lgh4 worker cannot currently connect to the OpenSUSE source endpoint, so
the OCI checks download the nine published source files pinned in
[`src/p4lang/source-inputs.json`](../../../src/p4lang/source-inputs.json).
They upload a per-run artifact; the native job selects its exact artifact ID
and independently checks every file's SHA256, size and descriptor identity.
It then passes `P4LANG_SOURCE_BASE_URL=file:/sonic/artifacts/vs/p4-source-inputs/sources`
to Make. Staging is separate from `src/p4lang`, where `dget` replaces its working
descriptor. Ordinary Make invocations keep the original OpenSUSE URL.

OpenSUSE currently publishes an inconsistent P4C descriptor: its unchanged
bytes identify an orig archive with SHA256 `61251b9c...` and size 173,765,465,
while both its mirror and direct content server supply `a41d616a...` and
173,758,549 bytes. The supplied archive matches SONiC's existing `versions-web`
record and the prior passing native build. The input manifest binds this exact
known discrepancy to the descriptor and archive hashes; receipts report it
explicitly, and any new discrepancy fails verification. This does not establish
equivalence with the archive named by the descriptor. The original descriptor
and existing `dget -u` behavior are preserved; no checksum rows are rewritten.
PI and BMv2 must match their descriptors normally.

This source handoff uses a fresh CI artifact. It adds no persistent P4 source
cache and changes no shared Make-cache logic. The tracked source pins are
already covered by the P4 recipes' existing input-file hashing.

The worker manager is maintained in
[agi-projects/sonic-slave-wrk-mgr](https://github.com/securely1g/agi-projects/tree/codex/sonic-slave-wrk-mgr/sonic-slave-wrk-mgr).
The native profile requires a privileged container, private PID/cgroup namespaces,
bridge networking, and passwordless sudo for its UID 1000 build user. Each job
starts its own Docker daemon; the host Docker socket is not mounted. The Actions
work volume is mounted at `/actions`, allowing the checkout to be bind-mounted
at `/sonic` for existing build hooks without hiding the runner's own files.

On lgh4, the manager selects the `sonic-slave-build` AppArmor profile to permit
the build user's namespaces required by Bazel's Linux sandbox. The profile is
installed on the host and assigned only to these privileged workers. Preparation
runs the pinned kernel Bazel sandbox as UID 1000 before building packages. Node-based Actions
use the image's existing system CA bundle for verified HTTPS connections.
The manager also declares `SONIC_BAZEL_JAVA_TRUST_STORE` in the image. The
isolated kernel adapter reads that default and records the selected trust
store's digest; the Make rule contains no runner-specific certificate path.

`prepare_vs_slave.sh` checks the image identity and native capabilities, prepares
the build hooks and configuration normally supplied by the outer Make wrapper,
and configures VS through `slave.mk`. The workflow supplies `SONIC_BUILD_GIT_NAME`
and `SONIC_BUILD_GIT_EMAIL` for package recipes that create temporary Git commits;
preparation sets this identity only in the ephemeral worker's user configuration.
The VS selector is passed to `configure` and saved in `.platform`; it is not
exported as a runtime hardware identity to native package tests.
It checks that the selected installer has
no Bookworm container prerequisites. A future configuration needing those
prerequisites must add an explicit compatible preparation stage.

The lgh4 profile has four ephemeral runner slots, each capped at six CPUs and
20 GiB. Workflow concurrency permits one full VS build at a time. Make runs one
package build at a time with four compilation jobs; Bazel is also limited to
four jobs. The manager registers a replacement container after each job.

Both cache mounts survive worker replacement:

| Mount | Use |
| --- | --- |
| `/dpkg_cache` | Existing Make package cache in `rwcache` mode |
| `/bazel_cache` | Existing Bazel disk, repository, and Bazelisk caches |

The shared APT policy allows eight retries per transient acquisition failure
(nine attempts), with exponential delays capped at 30 seconds and 20-second
HTTP/HTTPS/FTP timeouts. Each index or package download has its own retry
budget, including dependencies fetched while repairing a local-DEB install.
APT retains its normal error classification and TLS, signature and hash checks.
Index refreshes fail if any index cannot be downloaded, so an incomplete
refresh cannot become a successful cached Docker layer. These settings bound
individual attempts and delays, not the total duration of an APT command.

The native Docker CI job sets `SONIC_DOCKER_APT_RETRIES=0` to avoid APT 3.0.3's
unbounded delayed-retry warning queue ([Debian bug 1117797](https://bugs.debian.org/1117797)).
The Dockerfile generator installs a temporary APT fragment with a late-sorting
name and verifies that APT's effective retry count is zero before each independent
stage's hooks and first build command, including builder and runtime stages.
It refuses a pre-existing override path and removes only its
own unchanged fragment before the stage can be copied, flattened or exported.
The resulting image keeps the original eight-retry runtime policy. Unset or
empty leaves ordinary Make behavior unchanged; other values are rejected.

These native Docker builds use the existing `run_with_retry` helper, allowing
two retries with a ten-second delay. A failed Docker build still fails the job
after three attempts, and later attempts can reuse completed layers. Strict
index refresh errors, timeouts, and TLS, signature and hash checks stay enabled.
This CI-only mitigation does not change rootfs or native-package APT behavior,
repair a persistent transport failure, or force APT to refresh DNS.

GitHub supplies `BAZEL_REMOTE_AUTH` only to cache/build steps. The installed
credential helper sends it only to the configured cache origin. If the cache
cannot be reached, the job records an explicit local-only fallback. Invalid
credentials or authorization failures still fail setup. The cache receipt does
not claim a remote cache hit merely because HTTPS connectivity succeeds.

The former `Make VS with Bazel SWSS (AMD64)` job is disabled in this workflow.
After the new native build, CI verifies the installer container
selection and both runtime/debug archive pairs, runs the P4/debug checks in the
same slave, and publishes `sonic-vs-slave-amd64` with the installer, matching
container archives, and SHA256 checksums. `sonic-vs-slave-evidence-amd64` retains
the configuration and validation receipts.
