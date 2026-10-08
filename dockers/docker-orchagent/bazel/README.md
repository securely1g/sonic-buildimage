# Build SWSS with Bazel inside a Make VS image

`BUILD_WITH_BAZEL_WHEN_AVAILABLE` is the shared Make switch and defaults to `n`.
With `y`, containers that register Bazel targets for the selected build
configuration use Bazel. Other containers keep their existing Make build.
With `n`, all containers use Make.

For SWSS on native AMD64
Debian Trixie with `PLATFORM=vs` and ASAN disabled, the switch makes Bazel compile
SWSS and assemble its runtime and debug OCI images. Other SWSS configurations
continue to use Make, even with the switch set to `y`.
Make continues to generate the container manifests and build
the config-engine base, the Scapy wheel, other containers, host packages, kernel
and final `sonic-vs.img.gz` virtual-machine image.

The source and toolchain revisions are declared in the root `MODULE.bazel`; they do not change
the source revisions used by Make's existing submodules. SWSS and its declared
source dependencies produce runtime tar layers and matching debug symbols.
Pinned Debian packages supply external runtime dependencies; Bazel does not
create Debian packages in this path.

Make selects the builder before starting the build. Missing or invalid metadata
for a selected Bazel target, a missing Bazel executable, or a Bazel build failure
fails the build; it does not trigger a retry with Make.

## Checked APT layers

Runtime and debug package files use the shared
`@sonic_build_infra//apt:apt_layer.bzl` rule. The component runtime targets and
startup files remain unchanged. The runtime roots are the existing
`orchagent_debian` list; debug roots remain `gdb`, `gdbserver` and `strace` in
`orchagent_debug_debian`, using the same dated Debian repositories.

`apt.lock.json` is the canonical Distroless v2 lock with reviewed source DEB
identities and exact data/control hashes and sizes. `apt.from_lock` imports the
checked sets directly; normal image builds do not resolve them again.
Distroless owns dependency closure, package imports, the public
`@orchagent_debian//:package_set` provider, and tar assembly. The shared SONiC
rule passes that provider to the owner selector, then invokes upstream `flatten`
with its ordered selected-input manifest. Generated package-key lists and
private repository-name adapters are unnecessary.

The owner adapter preserves packages already installed in the base and rejects
changes to its executable/shared-library files, including source-built runtime
libraries when adding debug tools. Identical packages from multiple repositories
are included once. Non-binary file replacements are recorded for review. It
rejects package paths traversing inherited directory symlinks.

Orchagent has no Make-produced native DEB handoff: its component payloads come
from source-owned Bazel targets. `apt_policy.json` declares that supported image
profile and empty retained-package list. Runtime APT selection reads the checked
config-engine base; debug selection reads the exact completed orchagent runtime.
The native payload and its `DebugSymbolsInfo` remain in the normal source graph.

The `runtime_apt_selection` and `debug_apt_selection` targets expose JSON reports
with the base digest, lock/policy hashes, selected packages, retained versions,
duplicates and changed non-binary paths. Both variants still use `sonic_layer`
for Make's file filtering and `sonic_docker_archive` for the existing archive
outputs. The Make owner also exposes each complete OCI target for consumers
that need an image directory.

Retention does not solve new dependencies against the retained versions.
Verify native library loading and service behavior for the actual base/image
pair. This action assembles existing package files: it does not run maintainer
scripts, update the dpkg database or regenerate loader/Python caches. Source
symbols still require the existing exact-runtime validation.

The shared rule is pending infrastructure PR #27 and registry PR #45. This Draft
consumer uses their exact source revision via a temporary `git_override`, while
`.bazelrc` continues to use the registry's `main` URL. Remove the override and
verify normal resolution after publication before marking this PR ready.

Infrastructure owns the patches in `third_party/rules_distroless/patches`.
The root `archive_override` downloads upstream Distroless 0.9.4 and applies
those patches by immutable infrastructure commit and integrity hash. This
retains the protobuf header-fragment fix and adds checked APT lock imports,
package metadata and selected tar assembly. No Distroless fork or additional
registry version is required.

Bazel 8.5.1 applies dependency overrides only from the root module and rejects
patch labels from another module. The root therefore names the infrastructure
patch URLs, without keeping another copy of their content. Refresh those URLs
with the infrastructure pin. Both local builds and CI use ordinary `bazel` and
the maintained registry `main` URL.

For native AMD64/ARM64 test and source-layer CI without a full VS build, dispatch
`Bazel SWSS OCI` with `skip_vs=true`. This leaves the existing full-build default
unchanged. Each selected Bazel scope is checked for DEB-producing actions before
execution.

To refresh packages, use a separate clean checkout and artifact directory:

1. Replace this owner's two `apt.from_lock` declarations temporarily with the
   checked `apt.install` declarations in `apt-resolve.MODULE.bazel`. Adjust only
   the intended package roots or dated sources, then let Distroless resolve them.
2. Export the public hub lock, without reading private extension state:

   ```sh
   mkdir -p artifacts/apt-candidate
   bazel query @orchagent_debian//:lock.json
   bazel cquery @orchagent_debian//:lock.json --output=files > artifacts/apt-candidate/lock-path.txt
   cp "$(cat artifacts/apt-candidate/lock-path.txt)" artifacts/apt-candidate/apt.lock.json
   ```

3. Review the canonical dependency sets, source URLs, versions, checksums and
   closure. Keep only this owner's runtime/debug sets and their complete package
   closure. Preserve existing reviewed data/control hashes only when their source
   package identity is unchanged. For changed imports, audit the selected action
   graph before extracting the public provider's data/control files, then review
   and record their actual SHA-256 values and sizes in the canonical packages.
   Do not run DEB-producing actions as part of this refresh.
4. Publish the reviewed canonical lock and restore `apt.from_lock`. Rebuild both
   selected layers and validate their receipts against the intended base images.
   Re-run owner policy tests and the installed loader/runtime checks for any
   changed package or retained-version relationship.

CI runs selector regressions on native AMD64 and ARM64 with tar/OCI fixtures.
Those tests do not establish ARM64 image support; the complete image profile
remains native AMD64. Canonical lock parsing and package-set provider behavior
are validated by the upstream Distroless tests and actual AMD64 image actions.

## Startup configuration

Bazel builds the existing `sonic-cfggen` command from
[sonic-config-engine](../../../src/sonic-config-engine/README.bazel.md), using
the source-owned [sonic-py-common](../../../src/sonic-py-common/README.bazel.md)
library and declared Common/YANG bindings. The `config:docker_init` action runs
the same `sonic-cfggen -a '{"ENABLE_ASAN":"n"}' -t docker-init.j2` operation as
the legacy Dockerfile. Bazel selects its Python interpreter and native libraries
for the execution machine through the shared `sonic_cfggen_template` macro's
execution-tool dependency.

Config-engine and py-common also expose wheel and test targets that can be
invoked directly with Bazel. The Make switch selects container builders; it
does not select those standalone wheel targets.

The action fixes `PLATFORM=sonic-bazel-build` and clears `NAMESPACE_ID` to avoid
inheriting these host environment settings. This template consumes only the
explicit `ENABLE_ASAN` build input. The resulting shell script retains its live
CONFIG_DB, ASIC vendor, namespace, constants and chassis reads. It renders the
remaining configuration templates when the container starts.

Make's existing `generate_manifest` handles SWSS metadata, service discovery
and manifest overlays. SWSS registers separate runtime and debug JSON outputs in
its owning Make rules. `config/BUILD.bazel` declares these files as inputs to the
shared `manifest_labels` actions, which produce the OCI labels.
Native AMD64 and ARM64 CI prepare the same Make metadata, run the real
config-engine command, check failure exits and verify
the generated startup script. This tool coverage does not extend the complete
SWSS image's AMD64-only support boundary.

## Build

CI and local builds use the maintained registry `main` through the
[shared registry configuration](../../../tools/bazel/README.md#registry-selection).

Follow the repository's normal Make setup, initialize its submodules and
configure the VS platform:

```sh
make init
make configure PLATFORM=vs PLATFORM_ARCH=amd64
make target/sonic-vs.img.gz BUILD_WITH_BAZEL_WHEN_AVAILABLE=y
```

The Trixie slave already includes Bazelisk and selects the version recorded in
`.bazelversion`. Inside the Trixie slave, `BAZEL` can select another launcher
and `BAZEL_CONTAINER_ARGS` adds shell-quoted, single-line Bazel options.
`BAZEL_SWSS_ARGS` remains a fallback when the generic variable is unset. Local
cache settings can also go in the ignored `.bazelrc.user`. The same flag works for the ONIE installer target
`target/sonic-vs.bin`.

Make selects Bazel only for `target/docker-orchagent.gz` and
`target/docker-orchagent-dbg.gz`, preserving those Docker-save archive names for
the existing image installer. The debug image adds debugging tools and matching
symbols to the exact runtime image layers.

For an individual container, use the same Make option:

```sh
make target/docker-orchagent.gz BUILD_WITH_BAZEL_WHEN_AVAILABLE=y
make target/docker-orchagent-dbg.gz BUILD_WITH_BAZEL_WHEN_AVAILABLE=y
```

These targets first produce their Make inputs, including
`target/docker-config-engine-trixie.oci`, the Scapy wheel, and
`target/bazel-manifests/docker-orchagent/manifest.json`. The debug target also
prepares `target/bazel-manifests/docker-orchagent-dbg/manifest.json`, since its
image extends the runtime image. SWSS registers the
base in `SONIC_BAZEL_OCI_BASES`, selecting its Make archive with `_OCI_ARCHIVE`
and `linux/amd64` with `_OCI_PLATFORM`. The shared rule in
`tools/bazel/docker.mk` publishes the base's existing OCI files without converting or
recompressing them. The original `.gz` remains available to existing consumers.
Both outputs contain the same final config-engine image. Preparation also runs
after a Make package-cache hit and preserves unchanged layout timestamps.

With those inputs already available in `target/`, the equivalent Bazel targets are
`//dockers/docker-orchagent:docker-orchagent.gz` and
`//dockers/docker-orchagent:docker-orchagent-dbg.gz`. SWSS declares these explicit
labels and its Make prerequisites in its owning rules. The
[shared Make bridge](../../../tools/bazel/docker.mk) calls Bazel directly to build
the selected target and query its output path.
The shared archive helper publishes the completed archive to `target/` only
after both commands succeed. It preserves the previous archive on failure and
keeps its timestamp when the new bytes are unchanged.

If the Docker base archive and Scapy wheel already exist, prepare the layout
and manifests before invoking Bazel directly:

```sh
python3 tools/bazel/oci/prepare_oci_base.py \
  --archive target/docker-config-engine-trixie.gz \
  --output target/docker-config-engine-trixie.oci \
  --expected-platform linux/amd64
make -f tools/bazel/prepare_manifests.mk \
  MANIFEST_METADATA=rules/docker-orchagent.mk \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs \
  CONFIGURED_ARCH=amd64 ENABLE_ASAN=n DBG_IMAGE_MARK=dbg DOCKERS_PATH=dockers
```

The helper accepts the native OCI entries emitted by the pinned Docker version.
An older Docker-only cache must be rebuilt; it is not converted. Publication is
atomic, and repeating it with unchanged content preserves the layout timestamps.
See [the OCI base guide](../../../tools/bazel/oci/README.md#use-makes-oci-base).

## Reuse local build caches

Make defaults `SONIC_BAZEL_CACHE_SOURCE` to
`$(SONIC_DPKG_CACHE_SOURCE)/bazel`, regardless of whether
`BUILD_WITH_BAZEL_WHEN_AVAILABLE` is `y` or `n`. Override it with another
persistent host directory writable by the builder user. Make creates the
directory and checks writability before starting Docker.

Make mounts that directory at `/bazel_cache` and
[`tools/bazel/slave.bazelrc`](../../../tools/bazel/slave.bazelrc) at
`/etc/bazel.bazelrc`. The system configuration sets the repository cache to
`/bazel_cache/repository_cache` and the disk cache to `/bazel_cache/disk_cache`.
Ordinary Bazel commands inside the builder, including P4RT, inherit these
settings automatically. Bazelisk keeps downloaded Bazel binaries in
`/bazel_cache/bazelisk`. The shared cache survives removal of the builder and can
be reused from a fresh checkout on the same host. Each builder retains its own
output directories, working files and Bazel server; `output_user_root` is not
shared.

Set `SONIC_BAZEL_CACHE_SOURCE=` to omit both this cache mount and the mounted
system configuration. Bazel then runs with its usual local configuration.
For a container-bridge command that needs different cache paths,
`BAZEL_CONTAINER_CACHE_DIR` remains an optional override using the same
`repository_cache/` and `disk_cache/` subdirectories. Make does not export this
override automatically. `BAZEL_SWSS_CACHE_DIR` remains a fallback when the
generic variable is unset. Setting the generic variable to an empty value
suppresses that fallback; it does not disable caches configured in an rc file.

The source CI job also retains the five GitLab module source downloads selected
by gzip: `rules_gzip`, `toolchain_utils`, `ape`, `download_utils`, and
`rules_coreutils`. The small `bazel-repository-inputs` artifact contains about
572 KB. The VS job verifies each BCR SHA512 again before the long Make build and
supplies separate directories through Bazel's standard repeated `--distdir`
option; the upstream archives all have the same filename. This avoids downloading
these sources again from GitLab on the VS host. Bazel still selects each
published module and checks its integrity; no module override or prebuilt
component is introduced.
`tools/bazel/gzip/source_archive.py` rejects a changed resolved module version
until its expected source integrity is refreshed.

The full VS CI job also enables Make's package cache with
`SONIC_DPKG_CACHE_METHOD=rwcache`. Make restores a matching package or container
archive and writes a newly built result on a cache miss. The SWSS Bazel targets
still invoke Bazel to check their declared inputs. The agent stores both caches
outside the per-run checkout:

| Cache | Host path in VS CI |
| --- | --- |
| Bazel downloads and results | `/data/sonic-runner/cache/sonic-buildimage/bazel` |
| Make packages and archives | `/data/sonic-runner/cache/sonic-buildimage/packages` |

To use both caches for a local build, choose persistent, writable directories:

```sh
mkdir -p "$HOME/.cache/sonic-buildimage/packages"
make target/sonic-vs.img.gz BUILD_WITH_BAZEL_WHEN_AVAILABLE=y \
  SONIC_BAZEL_CACHE_SOURCE="$HOME/.cache/sonic-buildimage/bazel" \
  SONIC_DPKG_CACHE_METHOD=rwcache \
  SONIC_DPKG_CACHE_SOURCE="$HOME/.cache/sonic-buildimage/packages"
```

Omit the `SONIC_BAZEL_CACHE_SOURCE` override to use the default `bazel/`
subdirectory under the selected Make package-cache directory instead.

Bazel uses its declared action inputs to select cached results. Make uses its
existing source, dependency and configuration cache keys. Neither cache freezes
rolling upstream package repositories. CI retains fresh source checkouts and
checks available disk space; it does not delete these caches when a job ends.
Outside the VS workflow, Make's package cache remains opt-in.

## Validation

The independent `Container archive (AMD64)` and `Container archive (ARM64)` checks
exercise the Make OCI producer, Bazel base consumer and archive macro with tiny
images. They verify unchanged base bytes, platform checks, reproducible gzip
headers and bytes, Docker-save contents and the expected image tag without
building Debian packages or needing Make outputs. Artifacts retain the fixture
tar and gzip files, hashes, summarized test results, generated module lock and
resolved module graph. A separate cache regression builds the archive in two
fresh checkouts with separate Bazel output bases and one new local disk cache. It
checks cache hits and identical bytes, then changes a source input and requires
a rebuild. Its public result retains action names, cache status and archive
hashes. See the [archive guide](../../../tools/bazel/oci/README.md#reproducible-docker-archives).

The `SWSS source layers (AMD64)` PR check runs in native Debian Trixie. It checks
the Make handoff, OCI base publication, configuration rendering and package
validator. A native Make regression also checks package-cache reuse across fresh
checkouts and invalidation when source or architecture changes. The job then builds SWSS, its runtime dependencies, configuration and matching
debug-symbol tar layers. It also runs SWSS's Common Rust API and Serde consumer
test and checks the Debian path-filter rules. CI prepares manifests with Make
before running the shared label actions and orchagent contract tests under Bazel.
It verifies the source install
inventory, installed bytes and modes, AMD64 ELF files, build IDs, DWARF and debug links. Its artifact
includes the five tar files, hashes, summarized test and debug validation,
generated module lock and resolved module graph. CI starts without a module lock
and rejects tracked input changes.

The shared [`public_artifacts.py`](../../../tools/bazel/ci/public_artifacts.py)
step prepares each public upload. Bazel event records can include client
environment values, so uploads use extracted status fields and explicit output
paths. Raw command, event, execution and test logs/XML stay in the job workspace.
Successful jobs retain validated outputs and dependency records. A failed job
may retain only a partial status summary. Artifact preparation fails if a required
successful output is missing or a selected record fails validation. The VS
summary retains P4RT package/debug hashes, lookup counts and output sizes; the
separate VS image and SWSS archive upload keeps its existing files.

The `Make VS with Bazel SWSS (AMD64)` job builds the complete OCI
archives and final VS image after the source-layer check succeeds. It runs
automatically for pull requests and pushes to `master`, and on manual workflow
dispatch. It requires a disposable runner with the labels `self-hosted`, `linux`,
`x64` and `sonic-vs-source-pr-NUMBER` for a pull request, or
`sonic-vs-source-master` for push/manual runs. The host needs Docker, KVM, `j2`
and at least 100 GiB free for the workspace plus room for Docker storage.
The [runner setup and recovery guide](../../../tools/ci/runner/README.md) provides checked
provisioning, host preflight and one-job rearming, including PR #9 examples.
When no matching runner is available, this job remains queued; the
source-layer check alone does not validate the complete image.

Workflow concurrency is scoped to the source revision so a new push can run its
hosted checks while an older full VS build keeps its progress. A rerun of the same
revision supersedes its earlier run; a new full VS job waits for its runner.

The Make base supplies Python Common bindings while Bazel supplies Common's C++
library; their compatibility must be checked with the complete image. Guest
boot, forwarding, ARM64 images and cross builds remain unvalidated. ASAN uses
the existing Make path. Listing SWSS in `SONIC_PACKAGES_LOCAL` is rejected when
its Bazel path is selected.

To reproduce the source-layer check in native AMD64 Trixie, install the execution
tools listed in `.github/workflows/bazel-swss-oci.yml`, make the pinned Bazel
version available, and run from a clean checkout:

```sh
for tests in tools/bazel/tests tools/ci/tests tools/ci/runner; do
  python3 -B -m unittest discover -s "$tests" -p '*_test.py' || exit 1
done
python3 -B dockers/docker-orchagent/bazel/ci.py --bazel bazel --artifacts artifacts/swss
```

## Change the build

This directory keeps SWSS's CI target selection (`ci.py`) and package expectations
(`package_contract.py`); their tests live under `tools/bazel/tests`.
SWSS owns its opt-in guards, archive
labels and Make prerequisites. It registers both runtime and debug archives in
`SONIC_BAZEL_SWITCHABLE_IMAGES` even when the selector is off; the shared Make
rules track and invalidate each archive when its builder changes. The shared Make
handoff is in [tools/bazel/docker.mk](../../../tools/bazel/docker.mk); `slave.mk`
integrates it with the normal Docker and installer target lists. The same include
prepares registered OCI bases and manifests from each owner's declarations.
The container image and debug-symbol targets live in
[the container BUILD file](../BUILD.bazel).
Shared cache/publication helpers, Cargo configuration and validation
code are described in the [Bazel support guide](../../../tools/bazel/README.md). OCI preparation
lives under `tools/bazel/oci`; runner setup and native Make cache tests live under
`tools/ci`.

Another container can opt in through `SONIC_BAZEL_DOCKER_IMAGES` and
`SONIC_BAZEL_DBG_DOCKER_IMAGES` with its own `_BAZEL_TARGET`, `_PATH` and optional
`_BAZEL_DEPENDS` declarations. Register Make archive bases through
`SONIC_BAZEL_OCI_BASES` and per-base `_OCI_ARCHIVE`/`_OCI_PLATFORM` values. Register
manifests using `SONIC_BAZEL_MANIFESTS` and each key's
`_MANIFEST_IMAGE`/`_MANIFEST_SUFFIX` values, adding the generated JSON paths to
`_BAZEL_DEPENDS`. See the
[shared bridge example](../../../tools/bazel/README.md#opt-a-container-into-the-make-bridge)
and preserve that container's legacy selectors and metadata. The archive export
recipe, OCI preparation and cache settings are shared; SWSS's package expectations
and supported platform checks remain here.

Edit SWSS source lists, program dependencies and install declarations in the
owning `sonic-swss` repository, then update the pinned source revision here.
Container dependencies belong in `dockers/docker-orchagent/BUILD.bazel`; startup
configuration belongs in `dockers/docker-orchagent/config/BUILD.bazel`. The PR
check compares the packaged programs and data with SWSS's install declarations
and verifies that the container runtime layer preserves them.

`dockers/docker-orchagent/config/BUILD.bazel` supplies SWSS's generated JSON to
the [shared label macro](../../../tools/bazel/oci/README.md) and its non-ASAN
settings to the source-owned `sonic_cfggen_template` macro. Manifest metadata
continues to live in `rules/docker-orchagent.mk`. Other containers reuse the same
Make generator and Bazel label action with their own declarations.
