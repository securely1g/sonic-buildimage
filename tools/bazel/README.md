# Buildimage Bazel support

The component-owned build instructions describe the Python libraries, wheels,
complete source tests and installed-wheel checks:

- [config-engine](../../src/sonic-config-engine/README.bazel.md)
- [py-common](../../src/sonic-py-common/README.bazel.md)

Shared artifact publication and CI evidence helpers live in this directory.

## Registry selection

CI and local commands use the maintained SONiC registry `main` endpoint from
`.bazelrc`, alongside Bazel Central Registry. Module versions, source commits,
checksums and package snapshots remain pinned.

[Registry #44](https://github.com/securely1g/sonic-bazel-registry/pull/44)
landed `sonic-build-infra` version
`0.0.15-199a3d5aa97a6c5260cb7ca4bb5eb36cafb1a57a` on registry `main`
at `5c85252b0ebc888e53bfcbd95f07278e54450f98`. It registers the landed fix
from [build-infra #26](https://github.com/securely1g/sonic-build-infra/pull/26).
The root module override selects this version because commit suffixes do not
sort by source history; older transitive requests would otherwise win.

Use `bazel help --announce_rc` to inspect the effective rc selection. Keep one
SONiC registry endpoint; do not add another endpoint in a home or user rc.
Generated resolution receipts list registry lookup URLs from the lock; a reused
lock can also contain historical lookups.
