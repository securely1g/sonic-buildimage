# Native AMD64 Teamd images

`//dockers/docker-teamd:docker-teamd` and `:docker-teamd-dbg` compose the release
and debug images from the shared source-built SWSS parent. Their `.gz` targets
preserve the existing `docker-teamd:latest` and `docker-teamd-dbg:latest` archive
handoff. The debug image extends the exact release layers.

The only new project payload is the existing libteam owner's
`@libteam//:libteam_utils_runtime`: `teamd`, `teamnl`, `teamdctl`, their manual
pages, configuration and documentation. The same owner's patched `libteam5`
and `libteamdctl0`, plus SWSS's `teammgrd`, `teamsyncd` and `tlm_teamd`, already
come from `//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie`.
The image never compiles a second copy of these libraries.

The utility ELF files require `libdaemon0`, `libjansson4`, `libdbus-1-3` and
`libzmq5` in addition to their source libraries and libc. The shared APT selector
checks these four roots against the actual parent and retains its matching
packages. The runtime check rejects an unexpected additional external package.
All project runtime and matching symbol TARs retain their source owners.

The checked libteam package metadata records the three actual owner outputs,
including the inherited libraries. The image adds no runtime account. The
debug image adds the inherited debugger/editor tools, the source FIPS OpenSSH
client and its `_ssh` group contract, and symbols matching the complete native
source chain. Every installed package-state file is checked against its
generated input; no maintainer script or package installation runs in assembly.

The Make bridge selects native AMD64, VS, Trixie, FIPS enabled, ASAN disabled,
and the standard `dbg` suffix. Other profiles keep the existing producer.
The original `INCLUDE_TEAMD`, host `teamdctl` wrapper, shutdown ordering,
capabilities, mounts and service metadata remain owned by `rules/docker-teamd.mk`.
The selected Bazel archive edge needs manifests and owner TARs, not DEB files or
Make's package cache.

Build and validation are separate phases:

```sh
python3 tools/bazel/ci/teamd_image.py build --artifacts artifacts/teamd
python3 tools/bazel/ci/teamd_image.py validate --artifacts artifacts/teamd
python3 tools/bazel/ci/teamd_image.py incremental --artifacts artifacts/teamd
```

These commands use the shared CI entry point `tools/bazel/ci/libteam_draft_bazel.sh`
with the canonical registry main, the same catalog `.bazelrc` uses.
The build selects explicit runtime/debug TAR and OCI targets and audits their
action graph for DEB-producing outputs and wrappers. Validation checks source
bytes, modes, numeric ownership, inherited files, Make labels, entrypoints,
tagged archive contents and every source ELF/debug pair. A template edit must
change both images and archives while preserving source TARs and the parent;
restoring it must reproduce the original hashes. Timings describe the actual
same-output-base no-op/edit builds, not an inferred cache-hit count.

The installed-image check uses a private Redis helper and an isolated network
namespace with `NET_ADMIN` and `NET_RAW` added. It renders the shipped supervisor template
against that database, runs the startup script, creates a no-port LACP team,
queries its real Unix socket through `teamdctl`, checks the same kernel mode
through `teamnl`, and verifies `teamsyncd` publishes
a kernel MTU change and `tlm_teamd` reads the live daemon state. The debug variant
also exercises source/line/type lookup for all three libteam tools with an empty
symbol-directory negative control. Kernel team support is required; an absent
driver is a test failure. The fixture never uses the host network or physical
interfaces. Forwarding, the complete supervisor boot and full VS installer
acceptance remain separate checks under issue #130.

## CI

The AMD64 archive job in [Bazel OCI](../../../.github/workflows/bazel-oci.yml)
builds and validates both Teamd images alongside their shared SWSS parent. It
reuses the checkout, execution tools, Bazel setup, pinned compression source and
persistent cache. The template edit/restore check and matching debug-symbol
checks remain required. The ARM64 archive leg keeps its existing checks; it does
not build the AMD64-only Teamd profile.

A dependent job in the same workflow downloads `teamd-image-amd64`, verifies
its tested revision and hashes, and runs the installed runtime/debug fixture
above. Its results are retained as `teamd-runtime-amd64`. These checks run for
pull requests, pushes to `master`, and manual Bazel OCI runs.

## Refresh external inputs

The lock preserves exact previously reviewed Debian snapshot package records
and extracted-content hashes; independent resolution reproduces all 124
package records without changes. `refresh/MODULE.bazel` independently
resolves the four runtime roots, inherited debug tools and a test-only Redis
helper against the same Trixie, updates and security snapshots. With Bazel
8.5.1, resolve `//:resolve` from that preparation module using the selected
SONiC registry and BCR, then run `refresh/export_lock.py`. Review all package,
archive and extracted-content hashes before accepting a refresh. The Redis
helper is mounted as test data; it is never included in either image.
