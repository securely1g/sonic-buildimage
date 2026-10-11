# Syncd VS OCI image

Syncd uses SWSS (`docker-orchagent`) as its image-build template. Make builds
ordinary prerequisites and the rest of the VS installer; Bazel assembles the
runtime and debug OCI images and exports the Docker-save archives Make consumes.
There is no container-specific Make preparation script or payload handoff.

The image owner is `platform/vs/docker-syncd-vs/`, beside the original
[`Dockerfile.j2`](../Dockerfile.j2), [`start.sh`](../start.sh),
[`supervisord.conf`](../supervisord.conf) and
[`critical_processes`](../critical_processes). Its Bazel targets, APT inputs and
configuration declarations live here too. The Make selection remains in
[`platform/vs/docker-syncd-vs.mk`](../../docker-syncd-vs.mk).

## File mapping to SWSS

Both containers have these five files under `bazel/`:

| File | Purpose |
| --- | --- |
| `BUILD.bazel` | Export the checked APT inputs to the owner BUILD file. |
| `README.md` | Explain the supported configuration, inputs and validation. |
| `apt-resolve.MODULE.bazel` | Declare the intended direct APT roots for a deliberate lock refresh. |
| `apt.lock.json` | Pin the reviewed dependency closure and archive contents. |
| `apt_inputs.MODULE.bazel` | Export the locked candidate packages to Bazel. |

Image assembly lives in [`../BUILD.bazel`](../BUILD.bazel), which exports the
original startup inputs directly for assembly and tests.
[`../config/BUILD.bazel`](../config/BUILD.bazel) packages those inputs and the
generated labels and declares the reviewed installation state. There is no
second Syncd owner under `dockers/` and no `legacy/` symlink bridge.

Tests remain in [`tools/bazel/tests`](../../../../tools/bazel/tests/README.md)
and its `integration` subdirectory. Full-image validators and package-contract
logic remain in [`tools/bazel/oci`](../../../../tools/bazel/oci/README.md), shared
with other containers. The owner BUILD generates `:package_policy` from its
existing APT policy and explicit validation expectations; no owner Python
validator is needed.

## Build flow

The shared rules are the same as SWSS: `oci_base_layout`, `apt_layer`,
`sonic_layer`, `oci_image`, `debug_symbols_layer` and `sonic_docker_archive`.
The owner BUILD directly declares native `genrule` actions using `tar.bzl`'s
public `@bsd_tar_toolchains//:resolved_toolchain` and `$(BSDTAR_BIN)`, followed
by the shared `normalize_layer` rule. There is no custom package-import rule.

Shared path normalization and imported-symbol matching live in
`tools/bazel/oci`, alongside the existing OCI layout reader, inventory and APT
selection. They share that layout reader and the existing archive-validation
utilities. Source-owned runtime/debug packaging remains in `sonic-build-infra`.

1. Make builds the config-engine base, service manifests and the explicitly
   declared `.deb` prerequisites. Root `BUILD.bazel` exports those exact input
   filenames, just as it exports SWSS's Make-built Scapy wheel.
2. The declared BSD tar actions expose each package's original control/data
   archives and compose the ordered data archives as PAX. The separate read-only
   `tools/bazel/oci/package_receipts.py` validates package identities, source/control
   hashes, architecture, member order, ownership, links and extended metadata,
   then writes the retained-package manifest. Extraction creates no DEBs and
   executes no maintainer scripts. A failed validation fails the whole action.
3. Locked APT selection checks dependencies against the actual base, imported
   package controls and source-package metadata. Runtime FIPS OpenSSH is retained.
4. The runtime image adds selected APT, native packages, installation state and
   startup files to the config-engine OCI base.
5. The debug image extends that exact runtime with tools and matching symbols.
   Both archives retain the names and tags expected by Make and the VS installer.

There is no `inputs.mk`, `prepare_packages.py`, or pre-Bazel aggregate TAR and
manifest generation step. A changed `.deb` invalidates its Bazel extraction
action. Runtime and debug share extraction of identical declared package files.
Distroless remains the APT repository adapter. Its `flatten` rule emits GNU TAR,
so these local packages use BSD tar's PAX composition to retain extended metadata.

## Required differences from SWSS

Every difference needs a container-specific reason:

| Difference | Reason |
| --- | --- |
| Existing Make `.deb` inputs | PI, BMv2, p4c, DASH SAI, VS SAI and DASH-enabled Syncd do not yet have equivalent source targets in this image graph. Importing these packages is distinct from migrating their producers. |
| Source dependency receipt | Imported packages depend on Common/sairedis package identities. The shared `tools/bazel/oci/package_contract.py` binds those dependency declarations to the selected owner modules and actual TARs before APT selection. It does not pretend the source TARs are installed DEBs. |
| [`config/source_packages.json`](../config/source_packages.json) | Records the three reused owner targets, their dependency controls, required install paths and explicit data-file modes. It retains the actual base's `libsonicdbcli` identity rather than pinning one base build. |
| [`config/runtime_package_state.json`](../config/runtime_package_state.json) | These imported packages need alternatives, Syncd service links and provider copyright aliases that extraction alone cannot install. The shared `package_state.py` checks the selected package controls/scripts and link targets before emitting the reviewed state. SWSS does not install this package set. |
| Imported base symbols | The base retains a Make-built `libsonicdbcli` DSO. The shared matcher selects its companion and DWZ supplement against the final runtime, while excluding old Common/swssloglevel symbols replaced by source builds. |
| Additional APT snapshot | P4/compiler dependencies need six development packages from the pinned October snapshot to match inherited runtime versions. For example, `libc6-dev=2.41-12+deb13u4` requires exactly that `libc6` version from the base. SWSS does not select these development packages. |
| Narrower feature selector | This implementation preserves the reviewed DASH/FIPS profile. Other profiles continue to use the legacy Dockerfile. |

The source-owned libraries are exactly the targets used by SWSS:

- `@sonic_swss_common//:libswsscommon_pkg`
- `@sonic_sairedis//lib:libsairedis_pkg`
- `@sonic_sairedis//meta:libsaimetadata_pkg`

`SYNCD_VS_BAZEL_SOURCE_DEBS` removes these libraries' Make packages and their
debug packages from the image's direct prerequisites. The owner BUILD separately
declares which Make packages enter the image. Dependencies are expanded before
filtering so required libraries such as libyang and libnl remain available;
Make's remaining package producers can still need the excluded packages
transitively. SWSS has no equivalent filter because its native payloads already
come from source targets and it declares its image prerequisites directly.

Their original target edges remain in the layer graph so the shared symbol
collector can traverse their `flatten` and deployment rules. The source runtime
receipt does not depend on debug archives; a runtime build never requests debug
imports or symbol collection.

`base_debug_symbols.json` is unnecessary. The shared
`imported_debug_symbols_layer` reads the final runtime ELF build IDs and GNU
debuglink checksums, retains matching symbols and checks any DWZ supplement.
An explicit required path makes missing `libsonicdbcli` symbols fail the build.
SWSS currently does not provide this inherited-base pair; that coverage gap is
not a reason to remove the coverage Syncd already has.

## Supported configuration

Native Linux AMD64, Trixie, VS, `INCLUDE_VS_DASH_SAI=y`, `INCLUDE_FIPS=y`,
`ENABLE_ASAN=n`, `ENABLE_SYNCD_RPC=n`, and the standard `dbg` image suffix.
Cross builds, other architectures/distributions, unsupported feature settings,
custom package directories, and aggregate `docker-sonic-vs` requests keep their
legacy producer. Bazel's source configuration constraints mirror Make's selector.

FIPS is a runtime feature: the selected FIPS OpenSSH package is installed in
runtime and inherited unchanged by debug. Debug inputs cannot replace it with a
public Debian build. A rebuilt package may change `Installed-Size`; all other
reviewed controls and maintainer-script hashes remain checked. Runtime OpenSSH
also retains its explicitly reviewed archive/control identities.

## Build and inspect

Use the normal configured Make build:

```sh
make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y target/docker-syncd-vs.gz
make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y target/docker-syncd-vs-dbg.gz
make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y target/sonic-vs.img.gz
```

After Make has built the declared base, manifests and DEBs, the direct targets are:

```sh
bazel build //platform/vs/docker-syncd-vs:docker-syncd-vs.gz \
            //platform/vs/docker-syncd-vs:docker-syncd-vs-dbg.gz
bazel cquery --output=files //platform/vs/docker-syncd-vs:runtime_import_manifest
bazel cquery --output=files //platform/vs/docker-syncd-vs:debug_import_manifest
bazel cquery --output=files //platform/vs/docker-syncd-vs:source_package_receipt
bazel cquery --output=files //platform/vs/docker-syncd-vs:base_debug_symbols_receipt
```

The import receipts record the source labels, DEB hashes, original decompressed
control/data archive hashes, all control fields and debug/runtime binding.
The aggregate payload hash identifies the actual PAX output; its headers may
be normalized by the upstream tool while filesystem metadata stays unchanged.
The source receipt records module versions and actual source TAR inventories.
The base-symbol receipt records the actual runtime digest and selected pairs.
These are generated outputs, not checked-in captures of one machine's build.

To refresh APT deliberately, use `apt-resolve.MODULE.bazel` in an isolated
preparation checkout, review the new lock, then regenerate the candidate include
with `sonic-build-infra/apt/export_inputs.py`. Normal builds use the checked lock.

## Validation

The common `.github/workflows/bazel-oci.yml` runs SWSS source checks, Syncd
contract tests, native AMD64/ARM64 archive checks and the approved full VS build.
The Syncd tests check FIPS inheritance, package dependencies, source ownership,
installation state, metadata, imported/native symbols and complete OCI fixtures.
Normalization and symbol-matching tests live with the image helpers in
`tools/bazel/oci/tests` and run on native AMD64 and ARM64. The consumer's
`oci_package_receipts_test` uses checked-in
existing DEBs to exercise direct BSD tar extraction, repeated PAX composition,
real `rules_oci` consumption, original control hashes and policy rejection.
The selected action graph is inspected before execution to ensure these tests
do not create DEBs.

Both containers declare `ci_tests` and `ci_artifacts` in their owning BUILD
files. The shared runner discovers those declarations; no `ci_config.py` is
needed. Both suites use the same parameterized manifest test and shared OCI
inventory checks. Syncd retains checks for its package adapter, SAI/service
installation state and FIPS inputs. Its CI artifact group is empty because this
hosted job validates contracts without building production package prerequisites.

From a clean native AMD64 Trixie checkout without `MODULE.bazel.lock`, prepare
inputs through the existing Make metadata and run the common entry point:

```sh
make -f tools/bazel/prepare_manifests.mk \
  MANIFEST_METADATA=platform/vs/docker-syncd-vs.mk DEBS_PATH=target/debs/trixie \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs CONFIGURED_ARCH=amd64 \
  INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y ENABLE_ASAN=n ENABLE_SYNCD_RPC=n DBG_IMAGE_MARK=dbg \
  PLATFORM_PATH=platform/vs DOCKERS_PATH=dockers
python3 -B tools/bazel/ci/container.py \
  --container //platform/vs/docker-syncd-vs --source-file platform/vs/docker-syncd-vs.mk --bazel bazel --artifacts artifacts/syncd-vs/bazel
```

The shared runner audits the expanded test suite and artifact group, retains
each configured test's logs/XML, checks unchanged declared source hashes and
records dependency resolution. The artifact directory must be empty.

A complete image validation must additionally inspect both actual OCI images and
archives, verify runtime ancestry and labels, compare installed bytes/modes/owners/
links with the reviewed baseline, and exercise native loader/consumer behavior.
Retain the receipts and dependency resolution with that evidence. Existing
PI/BMv2/p4c, DASH SAI and libnl binaries without matching upstream debug packages
remain explicit coverage gaps; building a VS installer does not prove boot or
packet forwarding.
