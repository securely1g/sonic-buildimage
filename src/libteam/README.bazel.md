# Native libteam source validation

The `Bazel libteam and SWSS consumers` workflow builds libteam's runtime,
development and matching debug TARs, producer metadata, `teamsyncd` and
`tlm_teamd` on native AMD64 and ARM64 Trixie runners. It executes
`//src/libteam:consumer_test` with the declared runtime libraries. The SWSS
integration patches select this source-built libteam and the per-library ZeroMQ
interface; the Common patch applies to the current landed `f7fda40` source.

Before registry PRs #146–#148 land, CI selects the single frozen registry
`41d98ae02d50c83a5575e76992294d24db70028d`; its `DRAFT_VALIDATION_INPUTS.json`
records the composition. Infrastructure is
`0.0.25-6f6658865b7f687fa5e78d1675c0db8a5765b2ce`, Distroless is `.sonic.13`,
and libnl3 is `3.7.0.sonic.7`. The normal `.bazelrc` still uses maintained `main`.
For local candidate validation, use the explicit flags in
`.github/workflows/bazel-libteam.yml`, which load `common.bazelrc` without
loading the default registry endpoint. Do not append both endpoints.
Replace the temporary CI endpoint with maintained `main` and verify landed
source revisions before review readiness.

CI retains checkout inputs, the generated resolution lock, build/test events,
named outputs and SHA-256 checksums. These tests cover the component and its
build consumers. They do not establish daemon business flows, a full image or
successful inherited image workflows.
