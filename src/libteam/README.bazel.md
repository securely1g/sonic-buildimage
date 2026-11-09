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
`252a506ae5a94e36a4e330b736a3781a9f7bcf9b` (with `tools/bazel/0001-sonic-swss-libteam.patch`
until registered `sonic-swss` is selected) and Common
`0.0.9-334389eae54afd1f4f5ee69f89c3a7ad2062ee61`. Libnl3 `3.7.0.sonic.7` and
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

The native and OCI workflows use the [merged registry configuration](https://github.com/securely1g/sonic-bazel-registry/blob/97cc502556b7ec3435d2d67b105315bcc18bb47e/ci/source_mirrors.cfg)
at `97cc5025` and verify the same SHA256 before running Bazel, while root `.bazelrc`
keeps `tools/bazel/debian_mirrors.cfg` and canonical `sonic-bazel-registry/main`.
Pure caller tests verify script resolution and flag forwarding. The native
workflow retains generated module-resolution evidence for both architectures,
plus installed-output and four-phase probe receipts for AMD64, in its artifacts.
