# Build the VS Syncd container with Bazel

`BUILD_WITH_BAZEL_WHEN_AVAILABLE=y` selects the registered Bazel runtime and
debug images without filtering platform, architecture or FIPS settings. For
example, `CONFIGURED_ARCH=arm64 INCLUDE_FIPS=n` selects the same Bazel targets
as AMD64 with FIPS enabled. Setting the global flag to `n` keeps Make/Docker.

The remaining guards still require Trixie, DASH enabled and the `dbg` image
marker, with no cross build, QEMU, ASAN or Syncd RPC; builds of the
`docker-sonic-vs` container also keep their existing producer. This selection
policy does not add a toolchain or establish image validation for every tuple.

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

The default registry configuration uses maintained `main` plus Bazel
Central Registry. `MODULE.bazel` selects the published versions of these
producer updates:

| Module | Published registration | Producer change |
| --- | --- | --- |
| Common | [registry #153](https://github.com/securely1g/sonic-bazel-registry/pull/153) | Data permissions, canonical database path and library-link modes |
| Sairedis | [registry #154](https://github.com/securely1g/sonic-bazel-registry/pull/154) | Syncd service links in the runtime TAR |
| FIPS | [registry #152](https://github.com/securely1g/sonic-bazel-registry/pull/152) | OpenSSH agent permissions/ownership and socket link |
| Build infrastructure | [registry #151](https://github.com/securely1g/sonic-bazel-registry/pull/151) | Compiler, Automake and OpenMPI alternatives TARs |

The registry recipes pin landed source archives and patches by checksum.
Generated resolution receipts record the selected module versions. Local
builds and CI use the same registry configuration.

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

[BUILD.bazel](../BUILD.bazel) lists the actual target bindings. The generated
`source_contract` reads each producer's own `runtime_packages.json`, the selected
`MODULE.bazel` and the exact runtime TAR. Producer metadata supplies package
identity and any producer-declared file checks; the image checks their payload hashes
and consumes the TARs' file modes and ownership directly.
The `syncd-dash` configuration selects both the DASH SDK and its generated
metadata for sairedis dependency analysis. SONiC owners use their
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

The checked source receipt binds runtime TAR hashes, resolved module
identities and installed files. Genuine locked Debian packages supply
installed controls and configuration scripts. Both APT
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

The OpenSSH producer supplies `ssh-agent` as root:`_ssh` (GID 104), mode
`2755`; the image preserves this metadata and the producer-owned service
links. The image configuration retains the `x /tmp/ssh-*` tmpfiles exclusion.
APT configuration runs the package scripts that create accounts and alternatives,
so the image needs no separate alternatives snapshot layer.

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

Contract tests exercise selection, source provenance, installed files, package
state and Make fallback behavior. Syncd CI also builds the actual installed-package
status and receipt from the producer TARs with `--config=syncd-dash`; the public
artifacts retain both outputs. Source-owner and base-image workflows validate
their supported native AMD64 and ARM64 configurations separately.
Full DASH Syncd image validation currently runs on native AMD64; native ARM64
source-producer checks do not establish an ARM64 DASH image.

[Real package-manager validation](../../../../tools/bazel/oci/README.md#validate-real-package-transactions)
runs `apt-get update` and installs additional Debian packages in disposable
containers, then checks that the original SONiC programs and source files remain
intact. Base/config-engine cover native AMD64 and ARM64; Syncd runtime/debug
remain AMD64. Retained image receipts and transaction logs must demonstrate
success for this revision; helper tests alone do not.

Installed-container validation is maintained separately in
[PR #20](https://github.com/securely1g/sonic-buildimage/pull/20). It uses the
exported runtime/debug images to resolve libraries, exercise CLI and startup
behavior, check GDB symbol lookup, and compile a small P4 program with the
installed driver before starting its local BMv2 listener. These probes are not
part of the image build or the native archive tests.
The full VS workflow retains the existing installer boot and login checks.
Strict guest service validation is maintained separately in
[PR #28](https://github.com/securely1g/sonic-buildimage/pull/28).

Packet forwarding requires a connected test topology and is a separate check.
Building an installer or passing helper tests does not establish forwarding or
guest service health.
