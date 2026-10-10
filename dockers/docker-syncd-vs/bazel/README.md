# Syncd VS OCI image

Syncd uses SWSS (`docker-orchagent`) as its image-build template. Make builds
ordinary prerequisites and the rest of the VS installer; Bazel assembles the
runtime and debug OCI images and exports the Docker-save archives Make consumes.
There is no container-specific Make preparation script or payload handoff.

## File mapping to SWSS

Both containers have these seven files under `bazel/`:

| File | Purpose |
| --- | --- |
| `BUILD.bazel` | Export the checked APT inputs and image contract to the owner BUILD file. |
| `README.md` | Explain the supported configuration, inputs and validation. |
| `apt-resolve.MODULE.bazel` | Declare the intended direct APT roots for a deliberate lock refresh. |
| `apt.lock.json` | Pin the reviewed dependency closure and archive contents. |
| `apt_inputs.MODULE.bazel` | Export the locked candidate packages to Bazel. |
| `ci.py` | Run the explicit image contract tests and retain their evidence. |
| `package_contract.py` | Check the image's component install and dependency contract. |

Image assembly lives in `../BUILD.bazel`; startup files, labels and installation
state live in `../config/`. Tests live in `tools/bazel/tests` and its `integration`
subdirectory, alongside the SWSS tests. Full-image validators live in
`tools/bazel/ci/syncd_image.py` and `syncd_native_packages.py`.

## Build flow

The shared rules are the same as SWSS: `oci_base_layout`, `apt_layer`,
`sonic_layer`, `oci_image`, `debug_symbols_layer` and `sonic_docker_archive`.
The extra imported-package adapter is the shared `deb_import` rule from
`sonic-build-infra`, followed by its shared `normalize_layer` rule.

1. Make builds the config-engine base, service manifests and the explicitly
   declared `.deb` prerequisites. Root `BUILD.bazel` exports those exact input
   filenames, just as it exports SWSS's Make-built Scapy wheel.
2. `deb_import` extracts and hashes the existing files as normal Bazel actions.
   It preserves the original package order, ownership, permissions and control
   metadata. It creates no DEBs and executes no maintainer scripts.
3. Locked APT selection checks dependencies against the actual base, imported
   package controls and source-package metadata. Runtime FIPS OpenSSH is retained.
4. The runtime image adds selected APT, native packages, installation state and
   startup files to the config-engine OCI base.
5. The debug image extends that exact runtime with tools and matching symbols.
   Both archives retain the names and tags expected by Make and the VS installer.

There is no `inputs.mk`, `prepare_packages.py`, or pre-Bazel aggregate TAR and
manifest generation step. A changed `.deb` invalidates its Bazel import action.

## Required differences from SWSS

Every difference needs a container-specific reason:

| Difference | Reason |
| --- | --- |
| Existing Make `.deb` inputs | PI, BMv2, p4c, DASH SAI, VS SAI and DASH-enabled Syncd do not yet have equivalent source targets in this image graph. Importing these packages is distinct from migrating their producers. |
| Source dependency receipt | Imported packages depend on Common/sairedis package identities. `package_contract.py` binds those dependency declarations to the selected owner modules and actual TARs before APT selection. It does not pretend the source TARs are installed DEBs. |
| `config/source_packages.json` | Records the three reused owner targets, their dependency controls, required install paths and explicit data-file modes. It retains the actual base's `libsonicdbcli` identity rather than pinning one base build. |
| `config/runtime_package_state.json` and `package_state_layer.py` | These imported packages need alternatives, Syncd service links and provider copyright aliases that extraction alone cannot install. The layer checks the selected package controls/scripts and link targets before emitting the reviewed state. SWSS does not install this package set. |
| Imported base symbols | The base retains a Make-built `libsonicdbcli` DSO. The shared matcher selects its companion and DWZ supplement against the final runtime, while excluding old Common/swssloglevel symbols replaced by source builds. |
| Narrower feature selector | This implementation preserves the reviewed DASH/FIPS profile. Other profiles continue to use the legacy Dockerfile. |

The source-owned libraries are exactly the targets used by SWSS:

- `@sonic_swss_common//:libswsscommon_pkg`
- `@sonic_sairedis//lib:libsairedis_pkg`
- `@sonic_sairedis//meta:libsaimetadata_pkg`

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
bazel build //dockers/docker-syncd-vs:docker-syncd-vs.gz \
            //dockers/docker-syncd-vs:docker-syncd-vs-dbg.gz
bazel cquery --output=files //dockers/docker-syncd-vs:runtime_import_manifest
bazel cquery --output=files //dockers/docker-syncd-vs:debug_import_manifest
bazel cquery --output=files //dockers/docker-syncd-vs:source_package_receipt
bazel cquery --output=files //dockers/docker-syncd-vs:base_debug_symbols_receipt
```

The import receipts record the source labels, DEB hashes, original control
archives, all control fields, ordered payload hashes and debug/runtime binding.
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
Shared import, normalization and symbol-matching tests live with their rules in
`sonic-build-infra`. The selected action graph is inspected before execution to
ensure the image-only tests do not create DEBs.

A complete image validation must additionally inspect both actual OCI images and
archives, verify runtime ancestry and labels, compare installed bytes/modes/owners/
links with the reviewed baseline, and exercise native loader/consumer behavior.
Retain the receipts and dependency resolution with that evidence. Existing
PI/BMv2/p4c, DASH SAI and libnl binaries without matching upstream debug packages
remain explicit coverage gaps; building a VS installer does not prove boot or
packet forwarding.
