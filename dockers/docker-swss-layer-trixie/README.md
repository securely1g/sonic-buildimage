# Shared SWSS image from source owners

This companion to buildimage PR183 consumes the SWSS runtime TAR registered by
[registry PR240](https://github.com/securely1g/sonic-bazel-registry/pull/240), from
[source PR10](https://github.com/securely1g/sonic-swss/pull/10). It replaces PR183's
local libteam Bazel producer and temporary SWSS/Common patches with their shared
owner targets. The Make source checkout, recipes and patch series are unchanged.

| Input | PR183 | This companion |
| --- | --- | --- |
| SWSS | Git source `252a` plus local libteam patch | Registered `0.0.2-f85dc016197a9a2c91e36cc6eb4445fabf65fa9d`, which owns those dependency changes |
| libteam | Local `1.31.sonic-buildimage` producer | `1.31.0.sonic.1` from [registry PR236](https://github.com/securely1g/sonic-bazel-registry/pull/236), with the same upstream 1.31 and 16 SONiC patch bodies |
| Common / Sairedis / DASH API | Common7 plus local ZeroMQ patch / Sairedis3 / DASH4 | Common9 / Sairedis4 / DASH5, required by the selected SWSS source |
| Infrastructure / Distroless | Infrastructure25 / Distroless15 | Infrastructure26 / Distroless16, matching the owner interfaces |
| Common Rust test | Image wrapper around the old source binary | The source owner's real runtime test; Rust-listener wrappers use Common's declared `patchelf` argument |

The default `.bazelrc` continues to select maintained registry `main`. Draft CI
uses the single immutable complete catalog `2a849bbcaa254113a61ca0aeb972aeeccb8494f5`.
It requires PR240 and its PR236/238 stack. Source-base, native-libteam and
OCI archive CI verify the shared [registry PR228](https://github.com/securely1g/sonic-bazel-registry/pull/228)
configuration at `a32c87f0658e20f5fecd6459ef006caed96d905f`
(SHA256 `354c7d6ea5931c8ecb1c4a96fafa1269802b1547bde3b3b64dfc55a89a35cb46`). It retains the earlier libssl3t64,
libcap-dev and Python development-package routes and adds verified exact routes
for the observed Krb5/PCRE, rsyslog, JQ and ss-dev inputs while preserving the
collaborator FreeType routes. The selected package hashes remain enforced. The
reviewed merge is available on `codex/review-downloader-a32c87`; registry PR228
remains unlanded. Downloads do not establish native acceptance or fix general
Snapshot availability.
OCI regression checks retain `264002ba05e75df2f7c90dea2d78cf2bf46a4d4a`
(SHA256 `bb3a1a98` prefix). Eventd, Rust and YANG retain
`6e305126f13965fd01b6b3aa06e98d4595832d39` (SHA256 `e9f004bf` prefix).
Every context verifies the full pinned configuration hash. Locked package
checksums, declared source versions, existing bootstrap commands and forwarded
setup-bazel repository/disk cache options remain unchanged.

Socat uses `1.8.0.2.sonic.6` from [registry PR242](https://github.com/securely1g/sonic-bazel-registry/pull/242),
with the same upstream 1.8.0.2 and Debian
1.8.0.2-1 archive hashes. Its explicit `tar.xz` archive type supports the
verified content URL, whose path has no archive extension. This upgrade
from `.3` also includes the published compiler-path normalization and
procan namespace-string termination fixes from `.4` and `.5`; executable
and debug bytes may change. Configured features, tools, installed paths
and runtime/debug TAR interfaces remain as declared. Fresh resolution
and native acceptance for this selected version are still required.

The image keeps direct `@libteam` runtime TAR inputs. SWSS's reexport filegroups
are not substituted for those inputs because the debug aspect follows the direct
provider edges. The APT lock already excludes `libteam5` and `libteamdctl0`; it is
unchanged. Package metadata now binds those two libraries to the selected libteam
module and verifies their installed native bytes. Other source TARs retain the
existing package-accounting scope; no new dpkg records are invented.

The manual reviewed-source mode retains the legacy checkout's cache helper,
probe and test labels when its new source interfaces are absent. Normal companion
CI uses the new owner interfaces, and missing selected helpers fail closed.

The existing native archive, debug-pair, parent-payload, dependency, loader,
installed-container and cache checks remain. The incremental probe edits the
selected owner's exported `fpmsyncd/bgp_eoiu_marker.py`, preserves the source fence,
checks initial/no-op/edit/restore phases and restores its exact bytes and mode.
The artifact collector reuses the accepted 128MiB lock bound while retaining the
64MiB limit for other dependency JSON and every secret/schema check.

This is a Draft integration. Native AMD64 image, installed-runtime, actual module
resolution and four-phase acceptance must pass for this exact companion revision.
Historical PR183/191 results do not establish those results. Normal inherited
architecture matrices remain; the exact owned-branch Draft guards exclude DEB
fixtures and full VS CI. Remove the temporary catalog and branch guards and pass
normal required CI before marking it Ready.
