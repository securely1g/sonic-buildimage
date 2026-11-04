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
interface; the root applies the Common patch to its selected
`0.0.8-a0450924719a9f706125cc44a07dc88934935003` source.

Dependencies resolve from the canonical `securely1g/sonic-bazel-registry`
`main` catalog. Root and standalone libteam declare `libnl3 3.7.0.sonic.7`
and `rules_distroless 0.9.4.sonic.15`. Standalone libteam declares
`sonic-build-infra 0.0.25-34de7330abc72229dfc8dab88dec71b89c52aa88`;
the root dependency and override select
`0.0.26-2cfc059c2cf1dc3b2f2b2ec1bcd70400b04ecb75` for root builds.
`tools/bazel/ci/libteam_draft_bazel.sh` is only a pass-through
that loads `tools/bazel/common.bazelrc` with that canonical registry; it pins
no registry snapshot or module version. For local validation run
`tools/bazel/ci/libteam_draft_bazel.sh build @libteam//:libteam_runtime`.

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
