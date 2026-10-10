# docker-syncd-vs OCI integration

This opt-in builds the complete runtime and debug OCI images from the managed
config-engine base, checked Debian package content, shared source-built libraries,
and a Make package handoff. Common, sairedis and SAI metadata use the exact Bazel
targets already consumed by SWSS. PI, BMv2, p4c, DASH SAI, VS SAI and the
DASH-enabled syncd executable remain Make inputs.

The graph still needs a current native/rootfs/runtime comparison before rollout.
It models package data and the checked generated files described below. Debian
package database state and generated loader/Python caches remain explicit
validation gaps.

## Supported configuration

The owner registers Bazel only for this configuration:

| Setting | Value |
| --- | --- |
| Build environment and platform | `BLDENV=trixie`, `CONFIGURED_PLATFORM=vs` |
| Architecture | Native `CONFIGURED_ARCH=amd64`; no cross or QEMU build environment |
| DASH SAI | `INCLUDE_VS_DASH_SAI=y` |
| FIPS | `INCLUDE_FIPS=y` |
| ASAN and syncd RPC | Disabled |
| Debug suffix | `DBG_IMAGE_MARK=dbg` |
| Combined container | `docker-sonic-vs` is absent from the requested targets |

`BUILD_WITH_BAZEL_WHEN_AVAILABLE` remains the shared selector and defaults to
`n`. With `y`, the supported configuration uses these targets. Other
configurations keep their existing Dockerfile build. A selected Bazel failure is
a build failure; the shared bridge does not retry it with another builder.

Container launch flags and host mounts remain in
`platform/vs/docker-syncd-vs.mk`. The OCI config supplies the image entrypoint,
environment, labels, and filesystem layers.

## Targets and final assembly interface

All labels below are in `//dockers/docker-syncd-vs`.

| Label | Output |
| --- | --- |
| `:docker-syncd-vs` | Complete single-image Linux/AMD64 OCI directory |
| `:docker-syncd-vs-dbg` | Complete debug OCI directory extending the exact runtime OCI layers |
| `:docker-syncd-vs.gz` | Gzipped Docker-save archive tagged `docker-syncd-vs:latest` |
| `:docker-syncd-vs-dbg.gz` | Gzipped Docker-save archive tagged `docker-syncd-vs-dbg:latest` |
| `:runtime_layer` | Checked APT additions layered onto the config-engine base |
| `:debug_layer` | Checked debug APT additions layered onto the runtime image |
| `:runtime_apt_selection` | JSON describing checked, retained, and selected runtime APT inputs |
| `:debug_apt_selection` | JSON describing the equivalent check against the runtime image for debug tools |

The owning Make file retains `_BAZEL_TARGET` for the archive and declares
`_BAZEL_OCI_TARGET` for the complete OCI directory. Normal Make publishes the
archives at `target/docker-syncd-vs.gz` and `target/docker-syncd-vs-dbg.gz` through
the shared `tools/bazel/docker.mk` recipe.

The runtime `_BAZEL_DEPENDS` list is:

```text
target/docker-config-engine-trixie.oci
target/bazel-inputs/docker-syncd-vs/runtime/manifest.json
target/bazel-inputs/docker-syncd-vs/runtime/payload.tar
target/bazel-manifests/docker-syncd-vs/manifest.json
```

The debug list contains all runtime prerequisites plus:

```text
target/bazel-inputs/docker-syncd-vs/debug/manifest.json
target/bazel-inputs/docker-syncd-vs/debug/payload.tar
target/bazel-manifests/docker-syncd-vs-dbg/manifest.json
```

The config-engine layout uses the existing managed OCI declaration and source
archive. The remaining paths are regular files that final assembly can record
and stage by hash. Assembly copies those files into its own checkout for direct
Bazel consumption. Normal Make owns the managed package handoff in its producer
checkout.

`platform/` is excluded as a Bazel package tree in this repository. The OCI
package therefore lives under `dockers/`. Its `legacy/` source symlinks point to
the existing Dockerfile and startup files under `platform/vs/docker-syncd-vs`.
Edit those original files; the Bazel inputs follow their contents.

## Image build structure

The top-level [`BUILD.bazel`](../BUILD.bazel) follows the same assembly stages as
orchagent: supported configuration, checked base and Make labels, runtime inputs,
runtime image and archive, debug inputs, and debug image and archive. Layer order,
entrypoints, package lists, and output names stay visible in the image owner.

[`bazel/BUILD.bazel`](BUILD.bazel) holds the Make-package validation actions,
package-state action, and focused contract tests beside their source files. The
existing image, archive, selection, intermediate-payload, tool, and contract-test labels
in `//dockers/docker-syncd-vs` remain available. CI names the actual tests in
`//dockers/docker-syncd-vs/bazel` and collects their logs from that package.

The debug tools deliberately change five root-owned Vim alternatives from
`/usr/bin/vim.tiny` to `/usr/bin/vim.basic`: `editor`, `ex`, `rview`, `vi`, and
`view`. Complete-image validation allows only these exact symlink changes in
the debug-tools layer, checks both AMD64 Vim binaries, and requires every
inherited ELF to remain unchanged. All other inherited-link, parent-path and
whiteout checks still use the shared inventory guard; APT selection has no such
exception. The image report records the approved alternative paths.

The image combines source-owned TARs with checked Make package payloads. Source
symbols are collected through the same `DebugSymbolsInfo` rules as Orchagent.
This assembly does not compile PI, BMv2, p4c, DASH SAI or syncd with Bazel.

## Reuse SWSS's compiled libraries

The source inputs are `@sonic_swss_common//:libswsscommon_pkg`,
`@sonic_sairedis//lib:libsairedis_pkg` and
`@sonic_sairedis//meta:libsaimetadata_pkg`. Matching module versions and build
settings allow Bazel to reuse the same compilation outputs as Orchagent. The
image does not copy those components' C++ build rules or include SWSS daemons.

`source_packages.json` records the reviewed package dependency contracts, owner
module versions/revisions, target labels and required installed paths.
`source_packages.py` checks the resolved owner MODULE files, normalizes the
runtime paths and root ownership, and records the actual source TAR and symbol
hashes. These records describe source outputs, not Debian archives. Changing an
owner version requires reviewing this contract and checking the remaining Make
executables and the base's Python bindings against the new libraries.

The source action produces `source_runtime.tar`, `source_debug.tar` and
`source_packages.receipt.json`. Separate runtime/debug actions combine that
receipt with each original Make manifest for APT dependency checks. Make and
source packages remain separate inventories; duplicate ownership is rejected.
Runtime does not depend on the Make debug handoff. Debug must use the same
source receipt as runtime, and complete image validation checks both TARs and
all matching symbols against the actual deployed bytes.

## Shared OCI build and test code

The producer uses the same `oci_base_layout`, `sonic_layer`, `manifest_labels`,
`oci_image`, `sonic_docker_archive`, and Make bridge as SWSS. The owner BUILD file
supplies syncd's layer order, entrypoint, labels, and supported configuration.

Both paths use `tools/bazel/oci/oci_layout.py` to validate and read OCI metadata.
`tools/bazel/oci/oci_inventory.py` supplies shared layer inventory, whiteout and
parent-symlink and inherited ELF-link checks; syncd supplies its reviewed
merged-usr path adapter.
`tools/bazel/oci/apt_layer.bzl` binds the shared `apt_selection.py` executable to
the policy declared in this container's `BUILD.bazel`. The policy identifies its
Make package manifest and supported features. It permits no debug package
replacements. Both images use the same OCI inspection, APT selection, payload
staging, receipt writing, and command-line implementation. The package rules
remain in `sonic_apt.selection`; there is no container-owned Python selector.
The shared `tools/bazel/ci/artifact_validation.py` supplies streamed file hashes,
archive metadata, ELF headers, build IDs, DWARF checks, and debug-link checksums.
Syncd adds its package ownership, overlay, SONAME, and preserved symbol-gap policy.
Its CI uses orchagent's shared `command_log` runner and module-resolution
collector, retaining command timings and failure receipts. The action-graph
audit keeps raw action environments in a temporary private file and removes it
before publication. Syncd retains its explicit contract-test targets; orchagent
retains its source-layer build targets and package checks. Its synthetic image
tests use `tools/bazel/tests/oci_base_fixture.py` alongside the SWSS archive tests.

The remaining syncd code checks the Make-produced package handoff, checks explicit
APT inputs against the base, source and Make packages, and reconstructs the reviewed
package-generated state. SWSS consumes source-built tar layers and does not need
those package contracts.

## Make package handoff

`inputs.mk` uses the existing dependency-first `expand(...,RDEPENDS)` lists. It
also includes the two libnl development packages that the Dockerfile installs
explicitly. After dependency expansion, it removes the three source-owned
library DEBs and their matching debug DEBs from the handoffs. Their transitive
dependencies remain. One filtered Common debug input is retained for the base
image's unchanged `libsonicdbcli`: only its exact build-ID companion and required
DWZ supplement survive. `base_debug_symbols.json` pins the original DEB/control
and payload identities plus both selected files. The receipt records the original
and filtered payload hashes; obsolete source-library symbols are rejected.
For the supported `INCLUDE_FIPS=y` profile, the OCI runtime also
includes Make's `FIPS_OPENSSH_CLIENT`; the debug handoff omits that package and
inherits it from runtime. This corrects the legacy selection gap where P4C's
transitive MPI dependency installed public Debian OpenSSH in runtime and only
the debug image received FIPS OpenSSH. The legacy Dockerfile and Make package
lists remain unchanged.

`prepare_packages.py` reads existing DEBs, checks package identity
and architecture, records source and control hashes, and extracts their data
with `dpkg-deb`. GNU tar concatenates the uncompressed data tars in the same
package order.

Each variant publishes an immutable generation containing only:

- `manifest.json`: configuration, ordered package identities, source/control/data
  hashes, dependency fields, script hashes, aggregate hash and member count.
- `payload.tar`: the ordered aggregate installed payload.

A managed symlink selects the complete generation. A failed preparation keeps
the previous generation. Identical inputs retain the symlink and file timestamps.
The debug manifest records the runtime manifest hash and rejects different bytes
for any package shared with runtime. Preparation and payload validation require
runtime OpenSSH's `+fips` version to match its Debian control identity. They reject
stale runtime handoffs without FIPS OpenSSH and any debug OpenSSH package, even
if a supplied manifest omits OpenSSH from its required-package list.

The Bazel validation action checks the two declared files, configuration,
aggregate hash, and package segment counts. It accepts the managed producer
paths and the regular files staged by final assembly. Bazel does not invoke Make
or create a DEB at this boundary.

Before `sonic_layer` applies the shared SWSS path filters, the validation action
places files beneath the base's checked directory links at their canonical paths.
For example, `/lib/x86_64-linux-gnu/libnl-3.so.200` becomes
`/usr/lib/x86_64-linux-gnu/libnl-3.so.200`, and `/var/run/redis` becomes
`/run/redis`. It checks the base links and target directory metadata, omits the
package directory headers for those links, and rewrites hardlink paths as needed.
File bytes, ownership, permissions, and symlink targets are preserved. Added
layers reject paths that cross any other symlink parent or replace one with a
directory. The base's `/bin`, `/sbin`, `/lib`, `/lib64`, and `/var/run` links
therefore keep their native behavior during ordinary OCI unpacking.

The final native image removes `/debs`, so the handoff carries installed data and
keeps source DEB hashes in the manifest. It does not add package archives to the
runtime filesystem.

## Checked APT content

The shared `apt_layer` implementation comes from
[infrastructure #27](https://github.com/securely1g/sonic-build-infra/pull/27),
published by [registry #45](https://github.com/securely1g/sonic-bazel-registry/pull/45).
[Infrastructure #29](https://github.com/securely1g/sonic-build-infra/pull/29),
registered by [#51](https://github.com/securely1g/sonic-bazel-registry/pull/51), adds a
separately declared policy input on maintained infrastructure master. The
selector and this image no longer use the retired inherited-package replacement
API. Both changes have landed; this image selects registered module
`0.0.15-8043a299756436464a8b58662921dfd50172a3ff` without a Git override.

Local builds and CI use the maintained SONiC registry `main` endpoint plus BCR.
`MODULE.bazel` selects the reviewed registered module version.

The root module uses Distroless `apt.install` with exact package versions and
dated Debian repositories. The shared `apt_inputs` repository rule derives each
candidate's public `:data` and `:control` labels from `apt.lock.json`. `apt_layer`
checks every candidate before selecting only packages absent from the installed,
retained Make, source and inherited runtime inventories. Standard Distroless `flatten`
assembles the selected archives; `oci_image` adds the layer.

For example, a locked older `libssl3t64` is retained as a candidate, but the
installed FIPS package supplies the final dependency instead. Selection fails
if that actual package cannot satisfy a required version. File checks still
reject replacement of inherited ELF files or unsafe paths.

`apt.lock.json` is the reviewed candidate/content manifest, with exact source
identities and optional checked data/control hashes. Its `depends_on` edges
identify candidate inputs; final `Depends`, `Pre-Depends`, `Provides` and
`Multi-Arch` checks use the original package controls and actual image inventory.
The six refreshed development packages record `inherited_requirements` where
an exact runtime dependency is supplied by the newer config-engine base rather
than a candidate in the lock. These records document the boundary; shared
selection independently validates the control relationships and versions.

The six development packages for Python, libc, libcap and expat use Debian's
`stable` suite at snapshot `20261008T000000Z`. Their source and content hashes
are unchanged. The original toolchain's July `trixie` inputs and inherited
runtime libraries remain intact. Identical `libcares2` archives published by
two suites share one active candidate key; both historical source records remain.

To update candidates, review their source/control identities and dependencies in
`apt.lock.json`, then regenerate `apt_inputs.MODULE.bazel`
with `python3 PATH_TO_INFRA/apt/export_inputs.py --lock
dockers/docker-syncd-vs/bazel/apt.lock.json --module
dockers/docker-syncd-vs/bazel/apt_inputs.MODULE.bazel` (one command).
The selector tests enforce that this generated fragment matches the lock. Audit the Bazel action graph
before building the public data/control targets or image boundaries. These
operations import existing Debian packages and must not create DEBs. Rebuild
both image variants and run their installed loader/runtime checks.

Runtime receipts retain the full validated package inventory. Debug selection
requires the exact runtime receipt and its Make manifest hash, preserving
runtime Make records even when those development packages are absent from the
debug handoff. Make preparation preserves all original Debian control fields,
including identity and `Multi-Arch`; old incomplete handoffs must be regenerated
from the original source DEBs.

The runtime Make handoff supplies FIPS OpenSSH, so the ordinary public Debian
candidate stays unselected. Debug inherits the same package identity and bytes;
the owner policy allows no replacements. Existing handoff and image checks bind
the original source, control and payload hashes and verify actual payload bytes.

`.bazelrc` uses the registry's maintained `main` URL. No local copy of a
Distroless patch is needed.

Native AMD64/ARM64 CI runs the package and image contract tests. The complete
image profile remains native AMD64. Dispatch `Bazel SWSS OCI` with `skip_vs=true`
to validate source layers and contracts without starting a full VS image build.
Each selected Bazel scope is checked for DEB-producing actions before execution.

Also update `runtime_package_state.json`'s lock hash after reviewing its exact
control/payload owners. Changes to those owners require a new package-state
review; a new hash alone does not establish generated-state compatibility.
The committed-state test checks that these bindings match the content manifest.

## Generated files and startup behavior

`runtime_package_state.json` records the verified native alternatives, command
and library links, syncd init links, and two copyright aliases needed when
Distroless selects the concrete providers for `pkg-config` and `libc-ares2`.
The aliases use byte-identical provider files.

`package_state_layer.py` checks the Dockerfile, APT lock, selected package
owners, Make dependency/script fields, and the syncd init file before writing
that layer. It resolves every recorded link against the base, APT, and Make
layers and rejects ELF replacement. Ordinary binary checksum changes can pass
when package relationships, scripts, and relevant state inputs are unchanged.

When these inputs change, compare a current native image and its package control
scripts, update the recorded entries and identities, and rerun the complete
image checks. The reference hashes in the file identify comparison evidence;
they do not attest a new source build.

The runtime OCI image installs `start.sh` at mode `0755`, the supervisor files at
mode `0644`, and uses `/usr/local/bin/supervisord` as its entrypoint with no command.
It keeps inherited base settings and sets `DEBIAN_FRONTEND=noninteractive`.
Make remains responsible for the `com.azure.sonic.manifest` JSON, including the
`+dbg` package version suffix.

## Debug coverage

The debug OCI image extends the runtime image and adds the checked debug tools
and source/Make symbol payloads. `validate_image.py` requires all runtime payloads
to remain byte-identical, including FIPS OpenSSH. Debug must inherit the runtime
package; deployed ELF changes fail validation.

`validate_native_packages.py` checks SONAME paths, native dependency names,
build IDs, DWARF presence, and debug-link checksums. It records the existing
symbol gaps for imported PI/BMv2/p4c, DASH SAI, libnl, and FIPS OpenSSH content
that has no matching package in the Make debug list. The retained FIPS OpenSSH
ELFs are stripped and have build IDs/debuglinks; moving them into runtime does
not add matching symbols. It also reports symbol files belonging
to the imported base for separate validation against the complete image.

Source/line lookup with the actual debug image and source bundle remains a
separate check. An image or source revision alone does not prove that symbols
match its deployed ELF files.

The inherited `libsonicdbcli` retains its matching detached symbols and DWZ data
from the filtered Common input. Native validation checks the base ELF, debuglink
CRC and both runtime/debug `.gnu_debugaltlink` identities against that supplement.

## Validation

Run the direct helper tests with:

```sh
for test in prepare_packages_test.py validate_native_packages_test.py make_integration_test.py; do
  python3 -B -m unittest discover -s dockers/docker-syncd-vs/bazel/tests -p "$test" -v
done
```

Tests that import the shared APT library run in the Bazel suite below.
The direct suite creates sample DEBs with native `dpkg-deb` and compiles a small
ELF with native tools. These commands run outside Bazel.

Prepare the registered manifests before the explicit Bazel contract suite:

```sh
make -f tools/bazel/prepare_manifests.mk \
  MANIFEST_METADATA=platform/vs/docker-syncd-vs.mk \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs CONFIGURED_ARCH=amd64 \
  INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y ENABLE_ASAN=n ENABLE_SYNCD_RPC=n DBG_IMAGE_MARK=dbg \
  PLATFORM_PATH=platform/vs DOCKERS_PATH=dockers
python3 -B dockers/docker-syncd-vs/bazel/ci.py --bazel bazel \
  --artifacts artifacts/syncd-contracts \
  --bazel-arg=--jobs=4 --bazel-arg=--local_resources=cpu=4 --bazel-arg=--local_resources=memory=10000
```

The CI helper audits and runs seven explicit owner tests plus the shared APT
selector suite, covering source/import receipts, OCI fixtures, package state
and manifests. Its source-hash record includes both
image and adapter BUILD files alongside the reviewed package locks. It retains the selected execution audit fields, required test
logs, the generated `MODULE.bazel.lock`, and the module graph in the job workspace.
Raw action JSON is temporary and is removed after the audit. The public workflow
uses the shared [`public_artifacts.py`](../../../tools/bazel/ci/public_artifacts.py)
selector to retain validated dependency records, input/source hashes, the target
list and execution-audit counts. Raw logs and test XML stay in the workspace;
failed jobs may retain only a partial status summary. The checked APT content lock
remains in Git, and the generated Bazel resolution lock remains ignored.
`.github/workflows/bazel-syncd-vs-oci.yml` runs this contract suite on native
AMD64 Trixie. It does not run a production OCI or installer build.

For the complete image, prepare the normal supported Make context and request:

```sh
make target/docker-syncd-vs.gz target/docker-syncd-vs-dbg.gz BUILD_WITH_BAZEL_WHEN_AVAILABLE=y
```

Use `bazel cquery --output=files` with the same build options to locate the OCI
labels, `:apt_deps`, `:debug_tools`, and the two APT selection JSON targets.
Pass those paths, both package handoff manifests, both Make manifests, the
managed base, `runtime_package_state.json`, and both Make archives to
`validate_image.py`. Also supply `--source-runtime-tar`, `--source-debug-tar`
and `--source-receipt` from the three source outputs in the owner Bazel package. Complete mode checks the base/runtime/debug ancestry, all
supplied layer payloads, generated files, settings, labels, archive tags, and
archive config identity. `--fixture` permits the smaller metadata-only fixtures
used by tests.

Then run `validate_native_packages.py` on the two package handoff manifests
with the same three source arguments and `--base target/docker-config-engine-trixie.oci`, and
validate the unpacked rootfs, dynamic loading, debugger source/line lookup, and
representative service behavior. Full VS validation also covers the installer,
boot, and forwarding. The ordinary full VS target includes P4RT's Bazel
`//p4rt_app:p4rt_deb` and `//p4rt_app:p4rt_dbg_deb` package path when P4RT is
enabled; keep that execution scope distinct from the contract tests above.

The current layer assembly does not execute Debian maintainer scripts, update
the dpkg database, or regenerate loader/Python caches. The checked generated
files cover the recorded runtime effects. Complete validation reports the
remaining database, cache, unpacked-rootfs, and runtime checks explicitly.

## Completion and cache evidence

Treat each validation result according to the outputs it actually exercised:

| Evidence | What it establishes |
| --- | --- |
| Contract suite | Input checks, lock/label consistency, fixture overlays, and failure behavior. |
| Both real OCI images and exported archives | Base/runtime/debug ancestry, declared payloads, image settings, labels and Make-compatible archive identity. |
| Unpacked images and native/runtime checks | Actual link application, SONAME/loading behavior, matching symbols and debugger lookup, and required service behavior. |
| Full VS validation | Installer construction, guest boot and forwarding for the supported profile. |

A successful contract suite does not complete the later rows. The package-manager
and generated-cache gaps above remain explicit even when structural validation
reports `complete` mode. Repeat image and runtime checks when imported package
hashes change; reference-image hashes are comparison evidence, not proof of a
fresh source build.

The shared Make bridge and slave cache configuration apply here too:
`SONIC_BAZEL_CACHE_SOURCE` supplies repository and action caches through
`/bazel_cache`; each builder keeps its output directory private. Package input
preparation preserves unchanged generations, and archive export preserves an
unchanged output's timestamp. To claim cache reuse, record a cold build and a
representative changed-input build with action evidence; an unchanged rerun alone
does not establish which work was reused. Changing the Make/Bazel selector must
invalidate the previous producer's output through the shared builder stamp.
