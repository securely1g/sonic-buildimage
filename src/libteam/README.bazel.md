# Native libteam source validation

The `Bazel libteam and SWSS consumers` workflow builds libteam's runtime,
development and matching debug TARs, producer metadata, `teamsyncd` and
`tlm_teamd` on native AMD64 and ARM64 Trixie runners. It executes
`//src/libteam:consumer_test` with the declared runtime libraries. The SWSS
integration patches select this source-built libteam and the per-library ZeroMQ
interface; the Common patch applies to the maintained `a045092` source.

CI uses the immutable review catalog
`cfd42fb5eecf0e1a8e45da58598b107754c81ec3`. It selects libnl3
`3.7.0.sonic.7`, infrastructure
`0.0.25-3ee15ec9b4d99d7f8eae3e324cead79b203b451e`, and Distroless
`0.9.4.sonic.16`. Both root FIPS declarations select
`0.0.5-15fcb926a302059ec379f0c2fd15eb3d88bda4c9`.

The earlier ARM64 base-image check failed during Krb5 configuration with
FIPS `d1a2c8f`; its inner compiler log was not retained. This refresh uses
the current FIPS candidate from registry #221 and the infrastructure entry
published by #218, retaining its APT directory deduplication and foreign-build
header forwarding. The explicit root override keeps this validated interface
when dependencies request a different infrastructure version. The stacked review
base reuses #166's verified download mirrors and unchanged integrity checks.
Current CI must validate this selection; the earlier compiler-probe error
alone does not establish its underlying cause.

The libteam sources, SONiC patches, native tests, SWSS consumer patch and
Common ZeroMQ patch are unchanged. The current review base selects Common
`0.0.8-a0450924719a9f706125cc44a07dc88934935003`; its declared-runtime helper
accepts the maintained caller's explicit `patchelf` argument. The original
ZeroMQ patch applies unchanged to that source. No root Go SDK is changed.
The normal `.bazelrc` uses maintained registry `main`. Keep this PR Draft while the registry and review
base prerequisites remain unlanded. Earlier `681be7c5` and `fb3bc3f8`
results retain their original dependency scope.

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
