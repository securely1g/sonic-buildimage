# Build the VS Syncd container with Bazel

The supported profile is native AMD64 Debian Trixie, VS with DASH and FIPS,
without ASAN or Syncd RPC. Other Make profiles retain their Dockerfile build.

Bazel builds the project runtime packages from source, starting with the
`docker-base-trixie` and `docker-config-engine-trixie` images. The four P4/DASH
runtime packages remain the cached binary inputs from
[PR #14](https://github.com/securely1g/sonic-buildimage/pull/14). Migrating those
four producers is explicitly deferred.

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

Use the existing Make interface after configuring native VS:

```sh
make target/docker-syncd-vs.gz target/docker-syncd-vs-dbg.gz \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y INCLUDE_VS_DASH_SAI=y INCLUDE_FIPS=y \
  ENABLE_ASAN=n ENABLE_SYNCD_RPC=n
```

Make prepares the original image manifests and four cached P4 package filenames.
It selects `--config=syncd-dash` for both Syncd archive targets. That configuration
enables the source owner's DASH daemon and supplies the
[locked SDK](../../../../tools/bazel/p4/sdk/README.md). Other containers keep their
own settings. Both the build and output query use the same configuration.

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
| FIPS OpenSSH client | `@sonic_fips//openssh:openssh_client_deb` |
| libnl development files | Source-owned headers, static libraries and link/pkg-config files matching the two Make development packages. |
| PI, BMv2, P4C and DASH SAI | The four unchanged cached runtime DEBs from `tools/bazel/p4/packages.lock.json`. The fifth cached DEB supplies SDK headers. |

[BUILD.bazel](../BUILD.bazel) lists the actual target bindings. The
[source declarations](../config/source_packages.json) record resolved module
versions, public runtime targets and required installed paths. Common, sairedis
and Syncd keep their existing binary TAR layers. OpenSSH and libnl replace
Debian packages, so their actual DEBs supply dependency controls and installation
scripts. Ordinary source layers do not claim Debian package identities or
satisfy APT dependencies. SONiC owners use their
full source commit; foreign packages use the upstream release and SONiC patch
version. Assembly checks the resolved module declarations and actual source ELF bytes.

The debug image extends the exact runtime image. Its source symbols come from
the original producer TAR dependencies, including the source-built base, while
runtime file checks use the matching TAR or real DEB payload. The cached
P4/DASH tuple has no matching debug artifacts in the reviewed release inventory;
those gaps remain explicit. Static development libraries retain their own
symbols. A successful build is not a FIPS certification claim.

The four imported P4 packages form one aggregate runtime TAR. Both variants
retain separate package receipts, with the debug receipt bound to the runtime
receipt and the same payload hash. A declared symlink keeps `payload.tar`
beside the debug receipt without assembling a second copy. The debug image
inherits this payload through its runtime base.

## APT and installed package state

Both containers use the common `apt_layer`, `sonic_layer`, `oci_image`,
`debug_symbols_layer` and `sonic_docker_archive` rules. Distroless provides package
imports and TAR composition. The shared `apt_install` rule installs original
Debian archives into a private root filesystem and executes their original
maintainer scripts and triggers. Common, sairedis and Syncd remain binary layers;
the inherited Python applications remain wheel layers. Only Debian replacements
such as FIPS OpenSSH and libnl need real package identities. PI, BMv2, P4C and
DASH SAI still use the approved pinned binary archives.

The APT lock uses the same Debian snapshot as the source-built base. For example,
`libc6-dev` must match the base's `libc6` version. The selector preserves
source-built FIPS libraries when they already satisfy a dependency. It fails if
the final package requirements cannot be satisfied.

APT and dpkg own the installed package database, configuration records,
alternatives and service state. For example, OpenSSH's original installation
script creates its `_ssh` group and gives `ssh-agent` that group's setgid mode;
Debian's Vim script registers and restores its editor alternatives. The image
build no longer copies these effects from an earlier image or invents dpkg
control records. Syncd's init scripts stay in its source-owner runtime layer.

`syncd-packages.json` under `/usr/share/sonic/bazel` records the actual installed
package state and source ELF identities. Runtime and debug each receive a report
bound to their own APT installation receipt and configured filesystem. The
validator checks package-state hashes, dependencies, startup files and source
ELF/debug pairs against the final image. The complete filesystems are converted
to OCI layers with upstream tools while preserving inherited image settings.

The original `Dockerfile.j2`, `start.sh`, `supervisord.conf`, `critical_processes`
and Make image manifest remain the behavioral reference.

To refresh APT, use `apt-resolve.MODULE.bazel` in a private preparation checkout,
review its package versions against the base, and regenerate
`apt_inputs.MODULE.bazel` with `sonic-build-infra/apt/export_inputs.py`. Generated
files identify their generator and must not be edited by hand.

## Validation

Hosted contract checks exercise selection, source provenance, installed files,
package state and Make fallback behavior. Source-owner and base-image workflows
validate their supported native AMD64 and ARM64 configurations separately.
The cached DASH tuple supports only AMD64; these checks do not establish an
ARM64 DASH image.

[Real package-manager validation](../../../../tools/bazel/oci/README.md#validate-real-package-transactions)
runs `apt-get update` and installs additional Debian packages in disposable
containers, then checks that the original SONiC programs and source files remain
intact. Base/config-engine cover native AMD64 and ARM64; Syncd runtime/debug
remain AMD64. Retained image receipts and transaction
logs must demonstrate success for this revision; helper tests alone do not.

Full validation must use the resulting runtime/debug images: resolve installed
libraries, run the CLI and startup checks, and verify matching debug symbols.
The full VS workflow also boots the installed disk and checks database, SWSS,
Syncd and BGP service/container health, critical processes and Redis. Its guest
receipt is bound to the uploaded disk image.

Packet forwarding requires a connected test topology and is a separate check.
Building an installer or passing helper tests does not establish forwarding or
guest service health. See [installed image validation](../../../../tools/bazel/oci/RUNTIME_VALIDATION.md)
and [guest validation](../../../../tools/ci/VS_GUEST_VALIDATION.md).

## Deferred producer work

Migrate PI, BMv2, P4C and DASH SAI to source builds, then validate their matching
runtime/debug outputs and supported architectures. Until then, keep the exact
cached tuple and report its symbol and architecture limits.
