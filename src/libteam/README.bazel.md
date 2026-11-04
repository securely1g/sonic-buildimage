# Native libteam source validation

The `Bazel libteam and SWSS consumers` workflow builds libteam's runtime,
development and matching debug TARs, producer metadata, `teamsyncd` and
`tlm_teamd` on native AMD64 and ARM64 Trixie runners. It executes
`//src/libteam:consumer_test` with the declared runtime libraries. The SWSS
integration patches select this source-built libteam and the per-library ZeroMQ
interface; the Common patch applies to the current landed `f7fda40` source.

CI uses one complete review catalog based on maintained registry
`5f43269552ff4cff2020f898d2545ff353744083`, with the exact pending libnl3
entry from registry #181 and cumulative infrastructure entry from #218 (including #209). The catalog is
`51a3b267cfe4ca177c0ecfd6b47ed9bcd1b05c2d`.

The native interfaces are declared explicitly: libnl3 is `3.7.0.sonic.7`,
infrastructure is `0.0.26-2a6fc39c55be05e849b0e3a2d1808a4a59f123a6`, and
Distroless is the published `0.9.4.sonic.16`. Every maintained registry version
is preserved. The old catalog's native overlays on published libnl3 `.6`,
Distroless `.13`, and infrastructure `17a1e555` are no longer selected.
The libteam sources, 16 SONiC patches, native tests, SWSS consumer patch,
Common `f7fda40` ZeroMQ patch, and FIPS `d1a2c8f` selection are unchanged.
The normal `.bazelrc` still uses maintained `main`. Keep this PR Draft while
#181, #209, and #218 remain unlanded, and retain the earlier `681be7c5` acceptance
as evidence for its original catalog rather than claiming new-graph acceptance.

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

For PRs #51 and #61, inherited full-VS CI is explicitly skipped because these
source-only tasks exclude DEB generation. Three existing hosted kernel-fixture
suites that generate DEBs are also omitted for these PRs; other hosted checks
still run. These omissions are not image validation evidence.
