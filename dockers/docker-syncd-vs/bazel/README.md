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

## Shared OCI build and test code

The producer uses the same `oci_base_layout`, `sonic_layer`, `manifest_labels`,
`oci_image`, `sonic_docker_archive`, and Make bridge as SWSS. The owner BUILD file
supplies syncd's layer order, entrypoint, labels, and supported configuration.

Both paths use `tools/bazel/oci/oci_layout.py` to validate and read OCI metadata.
The shared `tools/bazel/ci/artifact_validation.py` supplies streamed file hashes,
archive metadata, ELF headers, build IDs, DWARF checks, and debug-link checksums.
Syncd adds its package ownership, overlay, SONAME, and preserved symbol-gap policy.
Its CI uses the shared module-resolution collector, and its synthetic image tests
use `tools/bazel/tests/oci_base_fixture.py` alongside the SWSS archive tests.

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

The root module declares separate runtime and debug APT sets using the same dated
Trixie sources as SWSS. `apt.lock.json` records the direct and transitive package
keys, reviewed source URLs and DEB hashes, and the exact data/control outputs
consumed by this image. `apt_packages.bzl` is a derived label list.

With Bazel 8.5.1, the selected Distroless extension uses its facts API and does
not consume its older `apt.lock` tag. The image therefore consumes existing
Distroless `data` and `control` targets by their checked keys. It compares the
current public package-set output with that list and validates the content
hashes before assembly. The source DEB hash and URL identify the reviewed import;
the data/control hashes enforce the actual files this image consumes.

`@sonic_build_infra//apt:apt_layer.bzl` and the shared `sonic_apt` library
implement the reusable APT behavior. `select_apt_payloads.py` only adapts syncd's
Make manifest and existing OCI inventory to that API. The shared implementation:

1. Retain packages already supplied by the OCI base or the Make handoff.
2. Reject a selected APT payload that changes a base ELF file.
3. Include a byte-identical package once when two suites supply it, while keeping
   both source records in the lock.
4. Record selected packages, retained versions, duplicate sources, and changed
   non-ELF base paths for review.

Runtime selection checks the managed config-engine base. Debug selection checks
the exact runtime OCI image and the debug Make handoff. This keeps Debian copies
from replacing FIPS and SONiC-patched libraries. Complete validation must confirm
that retained versions satisfy the added libraries and tools.

To prepare a lock update, use a new artifact directory:

```sh
bazel run @sonic_build_infra//apt:refresh -- \
  --lock "$PWD/dockers/docker-syncd-vs/bazel/apt.lock.json" \
  --output "$PWD/dockers/docker-syncd-vs/bazel/apt_packages.bzl" \
  --architecture amd64 --label-prefix SYNCD_ \
  --dependency-set runtime=syncd_vs_debian \
  --dependency-set debug=syncd_vs_debug_debian \
  --bazel bazel --artifacts "$PWD/artifacts/syncd-apt-candidate"
```

The helper checks the resolved rule version, package URLs and hashes, audits the
selected action graph, and builds only the existing data/control targets. It
stops if that graph contains a DEB output or packaging wrapper. A changed lock is
written as a candidate in the artifact directory. Review it, then rerun with a
new artifact directory and `--update` to publish the checked lock and label list.
`//dockers/docker-syncd-vs:apt_lock_check` checks the derived list through the
shared lock reader. Package lists, lock data, and the native package-state
contract remain image-owned; reusable APT code and its unit tests live in
`sonic-build-infra/apt` and are published through `sonic-bazel-registry`.

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

The CI helper audits and runs seven explicit JSON, tar, OCI-fixture, lock, and
manifest tests. It retains the selected execution audit fields, required test
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
