# Native libteam source validation

`src/libteam` is a standalone Bazel module (`libteam`). The root
`sonic-buildimage` module selects it with `local_path_override`, and the SWSS
integration patch adds `bazel_dep(name = "libteam")`, so the root image, SWSS
consumers and Make all build the same in-tree Debian 1.31-1 source and
`patch/series`. Root targets are `@libteam//:<name>`; from `src/libteam`,
`bazel build //:libteam_runtime` builds the component on its own (standalone
builds use rules_gzip's default APE gzip toolchain from cosmo.zip; the root
registers its reproducible `//tools/bazel/gzip:toolchain` instead).

The `Bazel libteam and SWSS consumers` workflow builds libteam's runtime,
development and matching debug TARs, producer metadata, `teamsyncd` and
`tlm_teamd` on native AMD64 and ARM64 Trixie runners. It executes
`@libteam//:consumer_test` with the declared runtime libraries. The SWSS
integration patches select this source-built libteam and the per-library ZeroMQ
interface; the Common patch applies to the current landed `f7fda40` source.

Before registry PRs #146–#148 land, CI selects the single frozen registry
`e0bf141cea82764b18d15d7d2c8516cbf3e99290`; its `DRAFT_VALIDATION_INPUTS.json`
records the composition. Infrastructure is
`0.0.25-17a1e555291c64be1a6ff008f33352bffc4f8e5c`, Distroless is `.sonic.13`,
and libnl3 is `3.7.0.sonic.6`. Both retain their published versions and source
archives, with the reviewed native changes applied as registry patches. The normal `.bazelrc` still uses maintained `main`.
For local candidate validation, run
`tools/bazel/ci/libteam_draft_bazel.sh build @libteam//:libteam_runtime`.
This wrapper loads `common.bazelrc` without the default registry endpoint.
The inherited base-image, OCI, eventd, Rust-listener and image-root YANG checks
use this same reviewed catalog; standalone YANG validation stays on `main`.
The dedicated libteam matrix retains its existing explicit registry flags.
Do not append both endpoints.
Replace the temporary CI endpoint with maintained `main` and verify landed
source revisions before review readiness.

CI retains checkout inputs, the generated resolution lock, build/test events,
named outputs and SHA-256 checksums. New acceptance is AMD64-only; the existing
ARM64 source job remains unchanged. On AMD64 CI also compiles the public C consumer
against the installed SDK/pkg-config interface and verifies all five detached
symbol pairs, including isolated positive/negative GDB source lookup. A config
edit probe verifies that the declared input changes the utils TAR and restoring
the exact source reproduces the original TAR. Its unchanged-input rebuild is a
same-output-base determinism check, not a fresh-cache or remote-hit claim.
These tests cover the component and its
build consumers. They do not establish daemon business flows, a full image or
successful inherited image workflows.

The shared OCI workflow applies no PR- or branch-specific exclusions: the
full-VS installer job and the DEB-generating kernel fixture suites run for this
change as they do for any other pull request.
