# Native libteam source validation

The `Bazel libteam and SWSS consumers` workflow builds libteam's runtime,
development and matching debug TARs, producer metadata, `teamsyncd` and
`tlm_teamd` on native AMD64 and ARM64 Trixie runners. It executes
`//src/libteam:consumer_test` with the declared runtime libraries. The SWSS
integration patches select this source-built libteam and the per-library ZeroMQ
interface; the Common patch applies to the landed `a045092` source.

Local builds and CI use the root `.bazelrc`: the maintained SONiC registry
`main` followed by BCR. Infrastructure
`0.0.25-3ee15ec9b4d99d7f8eae3e324cead79b203b451e` supplies the published native
library interfaces; `rules_distroless@0.9.4.sonic.15` exports the pinned Debian
headers and libraries, and `libnl3@3.7.0.sonic.7` provides the native libnl
interfaces and SDK. The root selects the libnl recipe's supported `rules_cc`
`0.2.22` and `tar.bzl` `0.10.5.sonic.1` versions consistently.

Initialize the root workspace's pinned gNOI submodule before local validation:

```sh
git submodule update --init -- src/sonic-sysmgr/gnoi
bazel build //src/libteam:libteam_runtime //src/libteam:libteam_dev
bazel test //src/libteam:consumer_test
```

These commands default to AMD64 Trixie. Native ARM64 callers pass both
`--platforms=@sonic_build_infra//platforms:aarch64_trixie` and
`--host_platform=@sonic_build_infra//platforms:aarch64_trixie`.

CI retains checkout inputs, the generated resolution lock, build/test events,
named outputs and SHA-256 checksums. On both native architectures CI also
compiles the public C consumer against the installed SDK/pkg-config interface
and verifies all five detached
symbol pairs, including isolated positive/negative GDB source lookup. The
development TAR sets every member's timestamp to zero using the standard mtree
time default. Generated pkg-config files and the compressed changelog therefore
cannot add build-time timestamps. Installed SDK validation rejects nonzero
timestamps, including PAX overrides; run
`python3 tools/bazel/tests/libteam_sdk_timestamp_test.py` for its regression tests.
A config
edit probe verifies that the declared input changes the utils TAR and restoring
the exact source reproduces the original TAR. Its unchanged-input rebuild is a
same-output-base determinism check, not a fresh-cache or remote-hit claim.
These tests cover the component and its build consumers. They do not establish
daemon business flows, a full image or
successful inherited image workflows.

The normal hosted regression suite includes the existing kernel package
fixtures. Full-VS jobs follow the repository's normal event and fork policies;
there are no branch- or PR-specific exclusions for libteam. Source-package
validation does not by itself establish a successful installer or guest boot.
