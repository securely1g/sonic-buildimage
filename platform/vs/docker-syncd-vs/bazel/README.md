# Build the VS Syncd container with Bazel

The supported profile is native AMD64 Debian Trixie, VS with DASH and FIPS,
without ASAN or Syncd RPC. Other Make profiles retain their Dockerfile build.

Bazel builds the project runtime packages from source, starting with the
`docker-base-trixie` and `docker-config-engine-trixie` images. PI, BMv2, P4C and
DASH SAI use their existing Autotools or CMake builds under Bazel and
produce runtime/debug TARs directly. Existing Make package recipes remain
available and unchanged.

Autoconf, Automake, Libtool, Make, M4 and pkg-config are pinned Debian execution
tools supplied by `sonic-build-infra`. The foreign builds use its declared GCC
toolchain and tool runtimes. The selected `rules_foreign_cc` patch preserves
ordinary, system and quote header search paths when invoking Make/configure;
vendor headers therefore retain their intended warning and lookup behavior.
The shared tool launcher stays a static C binary under GCC and Clang. DASH
metadata generation uses the declared spelling tool from the pinned runtime.

## Build

During review, first prepare the exact source dependencies listed in the
[base image manifest](../../../../dockers/docker-base-trixie/bazel/draft_sources.json):

```sh
python3 tools/bazel/ci/prepare_draft_sources.py \
  --manifest dockers/docker-base-trixie/bazel/draft_sources.json \
  --output "$PWD/target/bazel-draft-sources" \
  --receipt artifacts/draft-sources.json --bazelrc-output .bazelrc.user
```

Run that command inside the build environment that runs Bazel. For example,
prepare it inside the Trixie SONiC builder before a full Make build. The generated
`.bazelrc.user` points to those private source directories. Registry settings
remain `main` and the Bazel Central Registry; archive and patch integrity are
verified by the preparation helper and Bazel.

The manifest records the reviewed shared tool-runtime, foreign-rule and DASH
source registrations together. Update their versions in `MODULE.bazel` and
their source receipts together before regenerating the private overrides.

Use the existing Make interface after configuring native VS:

```sh
make target/docker-syncd-vs.gz target/docker-syncd-vs-dbg.gz \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y \
  ENABLE_ASAN=n ENABLE_SYNCD_RPC=n
```

Make prepares the original image manifests and selects `--config=syncd-dash`
for both Syncd archive targets. That configuration enables the source owner's
DASH daemon and supplies `@sonic_dash//:sdk`. No P4 DEB file is an OCI prerequisite.
Other containers keep their own settings. Both the build and output query use
the same configuration.

After those inputs exist, the direct targets are:

```sh
python3 tools/bazel/oci/native_build.py build --config=syncd-dash \
  //platform/vs/docker-syncd-vs:docker-syncd-vs.gz \
  //platform/vs/docker-syncd-vs:docker-syncd-vs-dbg.gz
```

## Runtime and debug inputs

| Input | Producer |
| --- | --- |
| Base filesystem, FIPS libraries, Python runtime and common service tools | The source-built `docker-config-engine-trixie` graph; ordinary Debian packages remain pinned APT inputs. |
| Common and its Rust CLI library | `@sonic_swss_common//:libswsscommon_pkg` binary layer |
| sairedis and SAI metadata | `@sonic_sairedis//lib:libsairedis_pkg` and `//meta:libsaimetadata_pkg` binary layers |
| VS SAI | `@sonic_sairedis//vslib:libsaivs_pkg` binary layer |
| Syncd and the DASH-enabled daemon | `@sonic_sairedis//syncd:syncd_pkg` binary layer |
| FIPS OpenSSH client | `@sonic_fips//openssh:openssh_client_pkg` |
| libnl development files | Source-owned headers, static libraries and link/pkg-config files matching the two Make development packages. |
| PI | `@p4lang_pi//:runtime` |
| BMv2 | `@p4lang_bmv2//:runtime` |
| P4C | `@p4c//:p4c_pkg` |
| DASH SAI | `@sonic_dash//:runtime` |

[BUILD.bazel](../BUILD.bazel) lists the actual target bindings. The
[path policy](../config/source_paths.json) records installed paths, file modes
and ownership. The `source_contract` target combines it with each producer's
generated `runtime_packages.json`, selected `MODULE.bazel` and runtime TAR. It
checks the generated payload hash against the exact TAR used by this image,
then records source provenance. Genuine locked Debian baseline packages supply
installed package controls and configuration scripts. The `syncd-dash`
configuration selects both the DASH SDK and its generated metadata for
sairedis dependency analysis. SONiC owners use their
full source commit; foreign packages use the upstream release and SONiC patch
version. Assembly checks the resolved module declarations and actual source ELF bytes.

The debug image extends the exact runtime image. Its source symbols come from
the same Bazel dependency graph, including the source-built base and all four
P4/DASH producers. The package policy requires their runtime/debug ELF pairs;
there is no exemption for missing P4 symbols. Static development libraries
retain their own symbols. A successful build is not a FIPS certification claim.

On AMD64, `p4testgen` also links the producer's declared retained Z3 static
library. Those Z3 objects have no DWARF; matching symbols cover the rebuilt
P4C/testgen code. The other shipped P4C programs do not link Z3, and ARM64
disables testgen.

PI's `pi_CLI_dummy` launcher imports `main` from `libpicli.so.0`; it has no
source objects of its own. Validation checks the launcher's build ID and
debug link, its imported function and required library, and the provider's
matching DWARF. Installed GDB must resolve that function to `main.c` and fail
the same source lookup when the packaged symbols are unavailable.

The checked source receipt binds the runtime TAR hashes, resolved module
identities, original TAR hashes and normalized installed files. Both APT
selection and installed package metadata consume that receipt directly.
The runtime never depends on a debug TAR; the debug layer adds paired symbols
to the exact runtime image.

## APT and installed package state

Both containers use the common `apt_layer`, `sonic_layer`, `oci_image`,
`debug_symbols_layer` and `sonic_docker_archive` rules. Distroless provides package
imports and TAR composition. The shared `apt_install` rule installs original
Debian archives into a private root filesystem and executes their original
maintainer scripts and triggers. Project runtime and debug TARs are applied
afterwards with separate source provenance; they do not create Debian package
records or satisfy APT dependencies. No Debian package is produced.

The APT lock uses the same Debian snapshot as the source-built base. For example,
`libc6-dev` must match the base's `libc6` version. The selector preserves
source-built FIPS libraries when they already satisfy a dependency. It fails if
the final package requirements cannot be satisfied.

The installed package metadata helper verifies the real installed Debian
controls and checked source TAR receipts. It records source provenance separately
without changing `/var/lib/dpkg/status`. The debug selector checks the exact
runtime inventory.

`syncd-packages.json` under `/usr/share/sonic/bazel` records the actual installed
package state and source ELF identities. Runtime and debug each receive a report
bound to their own APT installation receipt and configured filesystem. The
validator checks package-state hashes, dependencies, startup files and source
ELF/debug pairs against the final image. The complete filesystems are converted
to OCI layers with upstream tools while preserving inherited image settings.

The original `Dockerfile.j2`, `start.sh`, `supervisord.conf`, `critical_processes`
and Make image manifest remain the behavioral reference.

The checked-in `apt.lock.json` is the only APT package list consumed by a build.
`apt.import_lock` reads its package URLs, checksums, architectures and dependency
edges during repository setup and exposes the standard Distroless package targets.
To refresh it, use `apt-resolve.MODULE.bazel` in a private preparation checkout
and review the resulting lock against the base. No expanded import file needs
to be checked in or regenerated.

## Validation

Hosted contract checks exercise selection, source provenance, installed files,
package state and Make fallback behavior. Source-owner and base-image workflows
validate their supported native AMD64 and ARM64 configurations separately.
The image profile remains AMD64-only; native ARM64 source-producer checks do
not establish an ARM64 DASH image.

[Real package-manager validation](../../../../tools/bazel/oci/README.md#validate-real-package-transactions)
runs `apt-get update` and installs additional Debian packages in disposable
containers, then checks that the original SONiC programs and source files remain
intact. Base/config-engine cover native AMD64 and ARM64; Syncd runtime/debug
remain AMD64. Retained image receipts and transaction
logs must demonstrate success for this revision; helper tests alone do not.

Full validation must use the resulting runtime/debug images: resolve installed
libraries, run the CLI and startup checks, and verify matching debug symbols.
The P4 smoke check compiles a small program through the installed `p4c` driver
and its installed configuration, starts `simple_switch_grpc` with the resulting
JSON, and checks that its local gRPC listener opens. This checks installation
and startup; it does not exercise P4Runtime requests or packet forwarding.
The full VS workflow also boots the installed disk and checks database, SWSS,
Syncd and BGP service/container health, critical processes and Redis. Its guest
receipt is bound to the uploaded disk image.

Packet forwarding requires a connected test topology and is a separate check.
Building an installer or passing helper tests does not establish forwarding or
guest service health. See [installed image validation](../../../../tools/bazel/oci/RUNTIME_VALIDATION.md)
and [guest validation](../../../../tools/ci/VS_GUEST_VALIDATION.md).
