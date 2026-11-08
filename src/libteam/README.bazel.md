# Consume libteam from its shared owner

The image root uses `@libteam` version `1.31.0.sonic.1`. That registry module
owns the pinned upstream 1.31 source, the existing 16 SONiC patches, native
libraries and programs, runtime/debug TARs, development TAR, package metadata
and native consumer test. `src/libteam/BUILD.bazel` only forwards compatibility
labels used by existing image recipes. SWSS uses the owner's public providers
directly.

The existing Make recipe, Debian control files and source patches are unchanged.
These Bazel targets produce TARs and metadata, not DEBs. All three runtime
packages remain available: `libteam5`, `libteamdctl0` and `libteam-utils`.
The registry owner also exposes library-only metadata for SWSS's separate TARs.

The registered SWSS source `88d9cc2bcda39b0ebac16c95de9601e62021e3eb` still needs
`tools/bazel/0001-sonic-swss-libteam.patch` until equivalent libteam integration
lands in that source owner. The patch replaces stock libteam APT inputs with
`@libteam` library providers and `@libteam//:library_runtime_packages.json`, so
the installed library TARs and control-generated metadata have the same owner.
The root [MODULE.bazel](../../MODULE.bazel) records the selected dependency
versions and exact recipe overrides. Bazel's resolved graph must still be
verified in CI; matching declarations are not runtime proof.

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

The native and OCI workflows use `tools/bazel/ci/source_mirrors.py` to verify the
[shared registry configuration](https://github.com/securely1g/sonic-bazel-registry/blob/9697f3db62fc58a7a70b0d876abe0a849133a00d/ci/source_mirrors.cfg)
and preserve local Debian mirrors ahead of its source fallbacks. This includes
the verified pigz release without changing source checksums. Root `.bazelrc`
keeps `tools/bazel/debian_mirrors.cfg` and canonical `sonic-bazel-registry/main`.
Pure caller tests verify script resolution and flag forwarding. The native
workflow retains generated module-resolution evidence for both architectures,
plus installed-output and four-phase probe receipts for AMD64, in its artifacts.
