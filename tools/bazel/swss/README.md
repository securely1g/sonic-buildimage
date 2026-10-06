# Build SWSS with Bazel inside a Make VS image

`BUILD_SWSS_WITH_BAZEL=y` makes Bazel compile SWSS and assemble its runtime and
debug OCI images. Make continues to build the config-engine base, the Scapy
wheel, other containers, host packages, kernel and final `sonic-vs.img.gz` virtual-machine image.
The default Make build is unchanged.

This path supports native AMD64 Debian Trixie and `PLATFORM=vs`. The source and
toolchain revisions are declared in the root `MODULE.bazel`; they do not change
the source revisions used by Make's existing submodules. SWSS and its declared
source dependencies produce runtime tar layers and matching debug symbols.
Pinned Debian packages supply external runtime dependencies; Bazel does not
create Debian packages in this path.

## Build

Follow the repository's normal Make setup, initialize its submodules and
configure the VS platform:

```sh
make init
make configure PLATFORM=vs PLATFORM_ARCH=amd64
make target/sonic-vs.img.gz BUILD_SWSS_WITH_BAZEL=y
```

The Trixie slave already includes Bazelisk and selects the version recorded in
`.bazelversion`. Inside the Trixie slave, `BAZEL` can select another launcher
and `BAZEL_SWSS_ARGS` adds Bazel options. Local cache settings can also go in
the ignored `.bazelrc.user`. The same flag works for the ONIE installer target
`target/sonic-vs.bin`.

Make selects Bazel only for `target/docker-orchagent.gz` and
`target/docker-orchagent-dbg.gz`, preserving those Docker-save archive names for
the existing image installer. The debug image adds debugging tools and matching
symbols to the exact runtime image layers.

For an individual container, use the same Make option:

```sh
make target/docker-orchagent.gz BUILD_SWSS_WITH_BAZEL=y
make target/docker-orchagent-dbg.gz BUILD_SWSS_WITH_BAZEL=y
```

These targets first produce their Make inputs, including
`target/docker-config-engine-trixie.oci` and the Scapy wheel. Make publishes the
base's existing OCI files from its Docker-save archive without converting or
recompressing them. The original `.gz` remains available to existing consumers.
Both outputs contain the same final config-engine image.

With those inputs already available in `target/`, the equivalent Bazel targets are
`//dockers/docker-orchagent:docker-orchagent.gz` and
`//dockers/docker-orchagent:docker-orchagent-dbg.gz`. The Make wrapper publishes
the completed archive to `target/` only after the Bazel build succeeds.

If the Docker base archive and Scapy wheel already exist, prepare the layout
before invoking Bazel directly:

```sh
python3 tools/bazel/swss/prepare_oci_base.py \
  --archive target/docker-config-engine-trixie.gz \
  --output target/docker-config-engine-trixie.oci \
  --expected-platform linux/amd64
```

The helper accepts the native OCI entries emitted by the pinned Docker version.
An older Docker-only cache must be rebuilt; it is not converted. Publication is
atomic, and repeating it with unchanged content preserves the layout timestamps.
See [the OCI base guide](../oci/README.md#use-makes-oci-base).

## Reuse local build caches

With `BUILD_SWSS_WITH_BAZEL=y`, Make mounts the host's
`$HOME/.cache/sonic-buildimage/bazel` at `/bazel-cache` in each builder.
`BAZEL_SWSS_CACHE_SOURCE` selects another host directory. Bazel keeps downloaded
repositories in `repository/`, completed build results in `disk/`, and Bazelisk
keeps its downloaded Bazel binaries in `bazelisk/`. The cache survives removal of
the builder and can be reused from a fresh checkout on the same host. Each builder
keeps its own output base; concurrent builds do not share a Bazel server or its
working directory.

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
make target/sonic-vs.img.gz BUILD_SWSS_WITH_BAZEL=y \
  BAZEL_SWSS_CACHE_SOURCE="$HOME/.cache/sonic-buildimage/bazel" \
  SONIC_DPKG_CACHE_METHOD=rwcache \
  SONIC_DPKG_CACHE_SOURCE="$HOME/.cache/sonic-buildimage/packages"
```

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
tar and gzip files, hashes, test results, generated module lock and resolved
module graph. A separate cache regression builds the archive in two fresh
checkouts with separate Bazel output bases and one new local disk cache. It checks
cache hits and identical bytes, then changes a source input and requires a rebuild.
Receipts and execution logs are retained with the archive evidence. See the [archive guide](../oci/README.md#reproducible-docker-archives).

The `SWSS source layers (AMD64)` PR check runs in native Debian Trixie. It checks
the Make handoff, OCI base publication, configuration rendering and package
validator. A native Make regression also checks package-cache reuse across fresh
checkouts and invalidation when source or architecture changes. The job then builds SWSS, its runtime dependencies, configuration and matching
debug-symbol tar layers. It also runs SWSS's Common Rust API and Serde consumer
test and checks the Debian path-filter rules. The shared renderer and orchagent
adapter tests also run under Bazel to check their declared Python dependencies.
It verifies the source install
inventory, installed bytes and modes, AMD64 ELF files, build IDs, DWARF and debug links. Its artifact
includes the five tar files, hashes, command logs, build events, generated module
lock and resolved module graph. CI starts without a module lock and rejects
tracked input changes.

The `Make VS with Bazel SWSS (AMD64)` job builds the complete OCI
archives and final VS image after the source-layer check succeeds. It runs
automatically for pull requests and pushes to `master`, and on manual workflow
dispatch. It requires a disposable runner with the labels `self-hosted`, `linux`,
`x64` and `sonic-vs-source-pr-NUMBER` for a pull request, or
`sonic-vs-source-master` for push/manual runs. The host needs Docker, KVM, `j2`
and at least 100 GiB free for the workspace plus room for Docker storage.
The [runner setup and recovery guide](runner/README.md) provides checked
provisioning, host preflight and one-job rearming, including PR #9 examples.
When no matching runner is available, this job remains queued; the
source-layer check alone does not validate the complete image.

Workflow concurrency is scoped to the source revision so a new push can run its
hosted checks while an older full VS build keeps its progress. A rerun of the same
revision supersedes its earlier run; a new full VS job waits for its runner.

The Make base supplies Python Common bindings while Bazel supplies Common's C++
library; their compatibility must be checked with the complete image. Guest
boot, forwarding, ARM64 images and cross builds remain unvalidated. ASAN and
listing SWSS in `SONIC_PACKAGES_LOCAL` are rejected by this opt-in path.

To reproduce the source-layer check in native AMD64 Trixie, install the execution
tools listed in `.github/workflows/bazel-swss-oci.yml`, make the pinned Bazel
version available, and run from a clean checkout:

```sh
python3 -B -m unittest discover -s tools/bazel/swss -p '*_test.py'
PYTHONPATH=tools/bazel/swss python3 -B tools/bazel/swss/ci.py --artifacts artifacts/swss
```

## Change the build

Edit SWSS source lists, program dependencies and install declarations in the
owning `sonic-swss` repository, then update the pinned source revision here.
Container dependencies belong in `dockers/docker-orchagent/BUILD.bazel`; startup
configuration belongs in `dockers/docker-orchagent/config/BUILD.bazel`. The PR
check compares the packaged programs and data with SWSS's install declarations
and verifies that the container runtime layer preserves them.

The small `dockers/docker-orchagent/config/render.py` adapter supplies SWSS's Make
variable, service scope and non-ASAN settings. It uses the
[shared container renderer](../oci/README.md) for metadata parsing, strict Jinja
rendering and manifest labels. Other container recipes can depend on
`//tools/bazel/oci:container_config` without depending on orchagent's configuration
or runtime Python packages.
