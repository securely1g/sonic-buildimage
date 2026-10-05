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

These targets first produce their Make inputs. With those inputs already
available in `target/`, the equivalent Bazel targets are
`//dockers/docker-orchagent:docker-orchagent.gz` and
`//dockers/docker-orchagent:docker-orchagent-dbg.gz`. The Make wrapper publishes
the completed archive to `target/` only after the Bazel build succeeds.

## BuildBuddy remote cache

Both CI jobs use BuildBuddy when the repository Actions secret
`BUILDBUDDY_API_KEY` is configured with a key from your BuildBuddy account.
Set the optional Actions variable `BUILDBUDDY_CACHE_ENDPOINT` to the cache
endpoint for that account; it defaults to `grpcs://remote.buildbuddy.io`.
See the [BuildBuddy authentication guide](https://www.buildbuddy.io/docs/guide-auth/)
for creating a key with cache access.

CI writes the endpoint and a host-scoped Bazel credential helper to the ignored
`.bazelrc.user`. The helper sends credentials only to that secure endpoint.
The setup step saves the secret to a temporary file
outside the checkout with mode `0600`; build steps pass only that file's path to
the helper. This keeps the key out of Bazel's recorded client environment, build
events, command arguments and configuration. Make mounts the file read-only in
its slave and forwards the file path and endpoint by environment variable name.
CI removes the file after the build, including on failure.
This enables remote caching; compilation still runs on the CI
runner. No remote execution or BuildBuddy build-event upload is enabled.

Fork pull requests do not receive the secret. When the key is unavailable, CI
continues using local caches and explicitly reports that BuildBuddy is not
configured in its job summary.

## Validation

The `SWSS source layers (AMD64)` PR check runs in native Debian Trixie. It checks
the Make handoff, archive conversion, configuration rendering and package
validator, then builds SWSS, its runtime dependencies, configuration and matching
debug-symbol tar layers. It also runs SWSS's Common Rust API and Serde consumer
test and checks the Debian path-filter rules. It verifies the source install
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
and at least 300 GiB free for the workspace plus room for Docker storage.
The [runner setup and recovery guide](runner/README.md) provides checked
provisioning, host preflight and one-job rearming, including PR #9 examples.
When no matching runner is available, this job remains queued; the
source-layer check alone does not validate the complete image.

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
