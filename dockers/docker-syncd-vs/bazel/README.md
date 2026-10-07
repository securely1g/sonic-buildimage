# docker-syncd-vs OCI integration

This opt-in builds the complete runtime and debug OCI images from the managed
config-engine base, checked Debian package content, and the existing Make package
handoff. PI, BMv2, p4c, DASH SAI, sairedis, and their Debian package producers
remain in Make. This is an image packaging boundary, not a native compiler port.

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

The input adapter is intentionally different from orchagent's source-built
runtime tars and collected `DebugSymbolsInfo`: syncd consumes Make's checked
package payload and its matching debug-package handoff. This assembly does not
compile PI, BMv2, p4c, DASH SAI, or syncd from source with Bazel.

## Shared OCI build and test code

The producer uses the same `oci_base_layout`, `sonic_layer`, `manifest_labels`,
`oci_image`, `sonic_docker_archive`, and Make bridge as SWSS. The owner BUILD file
supplies syncd's layer order, entrypoint, labels, and supported configuration.

Both paths use `tools/bazel/oci/oci_layout.py` to validate and read OCI metadata.
`tools/bazel/oci/oci_inventory.py` supplies shared layer inventory, whiteout and
parent-symlink and inherited ELF-link checks; syncd supplies its reviewed
merged-usr path adapter.
The shared `tools/bazel/ci/artifact_validation.py` supplies streamed file hashes,
archive metadata, ELF headers, build IDs, DWARF checks, and debug-link checksums.
Syncd adds its package ownership, overlay, SONAME, and preserved symbol-gap policy.
Its CI uses orchagent's shared `command_log` runner and module-resolution
collector, retaining command timings and failure receipts. The action-graph
audit keeps raw action environments in a temporary private file and removes it
before publication. Syncd retains its explicit contract-test targets; orchagent
retains its source-layer build targets and package checks. Its synthetic image
tests use `tools/bazel/tests/oci_base_fixture.py` alongside the SWSS archive tests.

The remaining syncd code checks the Make-produced package handoff, filters locked
APT inputs against the base and Make packages, and reconstructs the reviewed
package-generated state. SWSS consumes source-built tar layers and does not need
those package contracts.

## Make package handoff

`inputs.mk` uses the existing dependency-first `expand(...,RDEPENDS)` lists. It
also includes the two libnl development packages that the Dockerfile installs
explicitly. `prepare_packages.py` reads existing DEBs, checks package identity
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
for any package shared with runtime.

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

The shared implementation is [infrastructure #27](https://github.com/securely1g/sonic-build-infra/pull/27),
published by [registry #45](https://github.com/securely1g/sonic-bazel-registry/pull/45).
While these dependencies are unmerged, the Draft consumer pins the same source
commit explicitly. Remove its temporary `git_override` after the registry entry
lands and verify normal module resolution before marking this PR ready.

The root module imports separate runtime and debug package sets with
Distroless `apt.from_lock`. `apt.lock.json` is the canonical Distroless v2 lock:
it records direct and transitive packages, dated source repositories, source
DEB identities, and the reviewed data/control hashes and sizes. Normal builds
consume this lock directly and do not resolve those package sets again.

Distroless owns dependency closure, package imports, the public
`@syncd_vs_debian//:package_set` provider, and tar assembly. The shared
`@sonic_build_infra//apt:apt_layer.bzl` rule passes the provider to the owner
selector, then calls upstream `flatten` with its selected-input manifest.
There are no generated package-key lists or private repository-name adapters.

`select_apt_payloads.py` adapts syncd's Make manifest and OCI inventory to the
SONiC policy checks. These preserve base and Make packages, reject changed ELF
files and unsafe inherited links, deduplicate identical imports, and report
changed non-ELF paths. Its receipt binds the exact canonical lock, base manifest,
Make handoff, and actual extracted content hashes. The reviewed hashes in the
lock are checked even for packages retained from the base or Make handoff.

Runtime selection checks the managed config-engine base. Debug selection checks
the exact runtime OCI image and the debug Make handoff. This keeps Debian copies
from replacing FIPS and SONiC-patched libraries. Complete validation must confirm
that retained versions satisfy the added libraries and tools.

The public Distroless API is supplied by
[Distroless #1](https://github.com/securely1g/rules_distroless/pull/1) and
[registry #46](https://github.com/securely1g/sonic-bazel-registry/pull/46).
Until both registry entries land, reproduce Draft builds with
`./tools/bazel/ci/draft_bazel.sh` wherever these examples use `bazel`.
This explicit wrapper selects only the `codex/distroless-locked-apt` SONiC
registry plus BCR, and imports the existing build settings. CI uses the same
wrapper. The normal `.bazelrc` still selects `main`, which currently lacks the
new Distroless version. The wrapper preserves the reviewed Make cache settings
when `/bazel_cache` is mounted. Remove the Draft wrapper/rc, restore CI to
`BAZEL=bazel`, remove the infrastructure source override, and validate normal
resolution from `main` before marking the PR ready.

To refresh packages, use a separate clean checkout and artifact directory:

1. Replace this owner's two `apt.from_lock` declarations temporarily with the
   checked `apt.install` declarations in `apt-resolve.MODULE.bazel`. Adjust only
   the intended package roots or dated sources, then let Distroless resolve them.
2. Export the public hub lock, without reading private extension state:

   ```sh
   mkdir -p artifacts/apt-candidate
   bazel query @syncd_vs_debian//:lock.json
   bazel cquery @syncd_vs_debian//:lock.json --output=files > artifacts/apt-candidate/lock-path.txt
   cp "$(cat artifacts/apt-candidate/lock-path.txt)" artifacts/apt-candidate/apt.lock.json
   ```

3. Review the canonical dependency sets, source URLs, versions, checksums and
   closure. Keep only this owner's runtime/debug sets and their complete package
   closure. Preserve existing reviewed data/control hashes only when their source
   package identity is unchanged. For changed imports, audit the selected action
   graph before extracting the public provider's data/control files, then review
   and record their actual SHA-256 values and sizes in the canonical packages.
   Do not run DEB-producing actions as part of this refresh.
4. Publish the reviewed canonical lock and restore `apt.from_lock`. Rebuild both
   selected layers and validate their receipts against the intended base images.
   Re-run owner policy tests and the installed loader/runtime checks for any
   changed package or retained-version relationship.

Also update `runtime_package_state.json`'s lock hash after reviewing its exact
control/payload owners. Changes to those owners require a new package-state
review; a new hash alone does not establish generated-state compatibility.
The committed-state test checks that these bindings match the canonical lock.

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
and Make symbol payload. `validate_image.py` requires the Make runtime payload to
remain byte-identical. It permits the explicit FIPS OpenSSH package to replace
its own runtime OpenSSH ELF files, matching the existing FIPS debug dependency.
Other deployed ELF changes fail validation.

`validate_native_packages.py` checks SONAME paths, native dependency names,
build IDs, DWARF presence, and debug-link checksums. It records the existing
symbol gaps for imported PI/BMv2/p4c, DASH SAI, and libnl content that has no
matching package in the Make debug list. It also reports symbol files belonging
to the imported base for separate validation against the complete image.

Source/line lookup with the actual debug image and source bundle remains a
separate check. An image or source revision alone does not prove that symbols
match its deployed ELF files.

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

The CI helper audits and runs five explicit JSON, tar, OCI-fixture, package-state, and
manifest tests from the owner subpackage. Its source-hash record includes both
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
`validate_image.py`. Complete mode checks the base/runtime/debug ancestry, all
supplied layer payloads, generated files, settings, labels, archive tags, and
archive config identity. `--fixture` permits the smaller metadata-only fixtures
used by tests.

Then run `validate_native_packages.py` on the two package handoff manifests and
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
