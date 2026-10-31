# Build the VS installer in a SONiC slave runner

The `VS installer in SONiC slave (AMD64)` job in `bazel-oci.yml` runs the
Actions worker inside the Trixie build container. It uses the
`sonic-slave-container` runner label and invokes the native build directly:

```bash
make -f slave.mk target/sonic-vs.bin \
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
The Monit producer fetches the pinned Debian source release and packaging from
`deb.debian.org` with SHA256 verification, avoiding a Salsa Git clone. It keeps
the SONiC StGit patches, Debian quilt patches, and runtime/debug package outputs.
The native libyang CLI tests also run with noninteractive CI streams. Their
harness reports test-file errors and zero executed tests as failures, while
retaining the existing expected skips.

The remaining native gbsyncd image still depends on PI, BMv2 and P4C. The runner
checks nine pinned source files in `/dpkg_cache/source-archives/p4lang` before
building and exports that directory as `P4LANG_SOURCE_DIR`. These are source
inputs; the native recipes still compile and package them. Populate this
directory when provisioning a new host, using the checksums and source details
in `src/p4lang`. Preparation rejects missing or altered inputs instead of
waiting for an unavailable source server late in the build.

P4C's source archive matches the existing SONiC download pin. Its original
descriptor names a different archive, so the local-source path updates only
the archive checksum and size rows in a private copy of that descriptor.
All original inputs are verified before this repair. Equivalence to the
different archive named by the original descriptor has not been established.

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

Native image cache keys include the actual Bazel base archive. Missing inactive
debug-package metadata is omitted from runtime keys; enabled debug and runtime
dependencies remain required. Hash failures stop cache lookup or publication,
preventing reuse under an incomplete dependency key.

GitHub supplies `BAZEL_REMOTE_AUTH` only to cache/build steps. The installed
credential helper sends it only to the configured cache origin. If the cache
cannot be reached, the job records an explicit local-only fallback. Invalid
credentials or authorization failures still fail setup. The cache receipt does
not claim a remote cache hit merely because HTTPS connectivity succeeds.

The former `Make VS with Bazel SWSS (AMD64)` job is disabled in this workflow.
During bring-up, this PR branch skips other CI jobs until the native installer
workflow succeeds; hosted regression, dependency-resolution, and AMD64/ARM64
archive checks will then be restored. After the new native build, CI verifies
the installer container
selection and both runtime/debug archive pairs, runs the P4/debug checks in the
same slave, and publishes `sonic-vs-slave-amd64` with the installer, matching
container archives, and SHA256 checksums. `sonic-vs-slave-evidence-amd64` retains
the configuration and validation receipts.
