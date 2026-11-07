# Consume libteam from its shared owner

The image root uses `@libteam` version `1.31.0.sonic.1`. That registry module
owns the pinned upstream 1.31 source, the existing 16 SONiC patches, native
libraries and programs, runtime/debug TARs, development TAR, package metadata
and native consumer test. `src/libteam/BUILD.bazel` only forwards compatibility
labels used by existing image recipes and the pinned SWSS source patch.

The existing Make recipe, Debian control files and source patches are unchanged.
These Bazel targets produce TARs and metadata, not DEBs. All three runtime
packages remain available: `libteam5`, `libteamdctl0` and `libteam-utils`.
The registry owner also exposes library-only metadata for SWSS's separate TARs.

The image retains master's FIPS `0.0.4-ef423363e8fd50358f0aa2a8a20d3e08d4450a56`,
infrastructure `0.0.26-51c838da6ff57295fde6e0bc3807841cb31a8568`, SWSS
`252a506ae5a94e36a4e330b736a3781a9f7bcf9b` and Common
`0.0.8-a0450924719a9f706125cc44a07dc88934935003`. The existing SWSS libteam and
Common ZeroMQ patches retain their behavior. Libnl3 `3.7.0.sonic.7` and
Distroless `0.9.4.sonic.16` satisfy the shared libteam contract. Bazel's resolved
graph must still be verified in CI; matching declarations are not runtime proof.

## Validation

The native workflow retains its AMD64/ARM64 matrix, 11 selected output targets,
uncached owner consumer test and AMD64 installed-SDK/debug checks. The installed
check verifies all five ELF/debug pairs, SONAME and SDK links, public headers,
pkg-config flags and zero development-TAR timestamps. The shared owner's
incremental probe performs baseline, no-op, config-edit and restoration builds
using the same Bazel entry point, flags, caches and execution root. The consumer
resolves the owner's script through its public source label and does not edit a
second local configuration file.
For pinned Bazel 8.5.1 with Bzlmod, the lookup reads `info output_base` and
validates its `execroot/_main` directory before resolving the owner script. This
avoids platform configuration during the path lookup while keeping the same
wrapper and all build/query options; empty, malformed or missing roots fail.

The historical source producer and incremental evidence on PR #162 at `233bde2`
remain evidence for that revision. They do not establish this new alias-based
consumer's acceptance. The inherited source-base, Eventd, OCI, Rust and YANG
checks retain their accepted cache forwarding. Each source-mirror bootstrap
uses the checksum-verified URL and digest declared in its workflow. Reproduce
the native and OCI paths with their respective URL and SHA-256 values from
[registry #228](https://github.com/securely1g/sonic-bazel-registry/pull/228).
Temporary exact-branch exclusions keep DEB fixtures and the full VS build out of
this Draft validation; remove those exclusions before normal readiness.

## Temporary registry selection

The committed `.bazelrc` keeps the maintained registry `main`. Draft CI uses the
explicit immutable catalog selected by `tools/bazel/ci/libteam_draft_bazel.sh`
and the native workflow, with configuration and generated locks retained in
artifacts. [Registry PR #236](https://github.com/securely1g/sonic-bazel-registry/pull/236)
is a direct prerequisite; #228 is the shared download-configuration prerequisite.
Remove the temporary selection after the owner lands.

Pure caller tests verify script resolution and flag forwarding. The native
workflow retains generated module-resolution evidence for both architectures,
plus installed-output and four-phase probe receipts for AMD64, in its artifacts.
Assess those receipts against the head, base and selected catalog under review.
