# Shared native AMD64 SWSS image

`//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie` assembles the shared
SWSS parent directly from the source config-engine OCI image and existing owner
TARs. Its public outputs are:

| Target in this Docker package | Output |
| --- | --- |
| `:source_runtime` | Complete shared source runtime TAR, also used by Orchagent |
| `:source_debug_symbols` | Matching symbols for that source TAR |
| `:docker-swss-layer-trixie` | Native AMD64 OCI image |
| `:docker-swss-layer-trixie.debug_symbols` | Matching source symbols through the entire parent chain |
| `:docker-swss-layer-trixie.gz` | Reproducible Make-compatible archive tagged `docker-swss-layer-trixie:latest` |

The source TAR combines SWSS's complete 30-program runtime, Common, SAI Redis,
SAI metadata, DASH API, its shared Protobuf ABI, and SONiC's patched libteam and
libteamdctl. Their existing owners compile them once and supply matching symbols.
Common is repeated with identical bytes so source-only Orchagent tests and the
retained Make-base path can consume the same complete TAR without an OCI parent.
The shared image inherits Common's Python binding, source libnl, configuration,
database paths and accounts from config-engine. It adds no service account.

Selected SWSS source, link arguments and ELF dependencies do not use libFIB;
that decision is checked by the separate SWSS native-closure validation. FRR
retains its own header and Make runtime-content prerequisite. libteam-utils
belongs to teamd, not this shared layer.

The external APT roots are only `iputils-ping`, `libjemalloc2` and
`libyaml-cpp0.8`. The lock retains the exact already-reviewed Debian snapshot
package identities and extracted-content hashes. Its ten candidates include
their complete dependency closure; the standard selector retains actual parent
packages and installs only missing external payloads. Orchagent no longer
requests stock `libteam5` or `libteamdctl0`; it consumes the same source TAR.

`runtime_package_metadata` preserves the parent's truthful installed package
records and records the selected APT additions. It does not claim the source
TARs were installed through dpkg, or fabricate SWSS/DASH package identities.
Source file hashes, ELF dependencies and matching symbols are independently
checked against the assembled OCI inventory and the loaded image.

Make's existing archive bridge selects this target only for native AMD64 VS,
Trixie, ASAN disabled, and `BUILD_WITH_BAZEL_WHEN_AVAILABLE=y`. It preserves
legacy declarations for excluded configurations. The selected archive needs
the base/config-engine/shared-layer manifests, not project DEBs or Make's
package cache. Direct preparation and build are:

```sh
make -f tools/bazel/prepare_manifests.mk \
  'MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk' \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs \
  CONFIGURED_ARCH=amd64 INCLUDE_FIPS=y DOCKERS_PATH=dockers
bazel build //dockers/docker-swss-layer-trixie:docker-swss-layer-trixie.gz
```

While the prerequisite producer registrations remain unlanded, this Draft uses
the same reviewed registry and `tools/bazel/ci/libteam_draft_bazel.sh` as the
libteam/SWSS closure changes. Ordinary `.bazelrc` remains on canonical registry
main; remove the temporary wrapper when the required registrations land.

The targeted workflow audits the action graph for DEB generation, checks all
source files against the final OCI inventory, matches every source ELF with
its symbols, preserves inherited files, loads the exact exported archive and
checks installed hashes and the complete loader closure. A declared installed
Python edit must change the runtime TAR/archive while preserving compiled
symbols and the parent; restoring it must reproduce the original bytes.
This is same-output-base no-op/edit evidence, not a cold-cache compilation claim.

Descendant service fixtures and complete VS installer acceptance remain in their
own integration tasks. The source/OCI-only CI scope does not run Make's full
installer or any DEB-producing target. The Make bridge is registered here; full
installer execution is not claimed by these focused checks.

To refresh external packages, resolve `apt-resolve.MODULE.bazel` with Distroless
against the pinned snapshot sources in the lock. Review the resulting identities
and dependencies, preserve extracted-content hashes only for unchanged archives,
and rerun the image/loader and no-op/edit checks. Never substitute distribution
libteam packages for the owner outputs.
