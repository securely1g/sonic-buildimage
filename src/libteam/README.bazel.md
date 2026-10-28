# Native libteam source validation

The `Bazel libteam and SWSS consumers` workflow builds libteam's runtime,
development and matching debug TARs, producer metadata, `teamsyncd` and
`tlm_teamd` on native AMD64 and ARM64 Trixie runners. It executes
`//src/libteam:consumer_test` with the declared runtime libraries. The SWSS
integration patches select this source-built libteam and the per-library ZeroMQ
interface; the Common patch applies to the current landed `f7fda40` source.

Before registry PRs #146–#148 land, CI selects the single frozen registry
`d25ab9d2a37ab2caf9b052cc3aaa4e35aee1a74d`; its `DRAFT_VALIDATION_INPUTS.json`
records the composition. Infrastructure is
`0.0.25-1341eaf4a7759ef0121ae7c17edcd0000d063038`, Distroless is `.sonic.13`,
and libnl3 is `3.7.0.sonic.7`. The normal `.bazelrc` still uses maintained `main`.
For local candidate validation, run
`tools/bazel/ci/libteam_draft_bazel.sh build //src/libteam:libteam_runtime`.
This wrapper loads `common.bazelrc` without the default registry endpoint.
The inherited base-image, OCI, eventd, Rust-listener and image-root YANG checks
use this same reviewed catalog; standalone YANG validation stays on `main`.
The dedicated libteam matrix retains its existing explicit registry flags.
Do not append both endpoints.
Replace the temporary CI endpoint with maintained `main` and verify landed
source revisions before review readiness.

CI retains checkout inputs, the generated resolution lock, build/test events,
named outputs and SHA-256 checksums. These tests cover the component and its
build consumers. They do not establish daemon business flows, a full image or
successful inherited image workflows.
