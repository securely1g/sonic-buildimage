# Source-built Trixie base images

These targets build `docker-base-trixie` and `docker-config-engine-trixie` from a
pinned Debian seed, reviewed Debian packages, and the public source-build targets
owned by SONiC components. For example, changing Common's C++ source rebuilds its
runtime TAR and the config-engine image layer; it does not rebuild Debian.

## Public targets

| Owner package | Target | Output |
| --- | --- | --- |
| `//dockers/docker-base-trixie` | `:docker-base-trixie` | OCI image |
| `//dockers/docker-config-engine-trixie` | `:docker-config-engine-trixie` | OCI image using the source-built base |
| Either owner | `:<image>.gz` | Make-compatible Docker archive |
| Either owner | `:<image>.debug_symbols` | Matching source ELF debug files under `/usr/lib/debug/.build-id` |
| Either owner | `:runtime_package_metadata` | Installed package versions, source revisions and native file hashes |

OCI means the standard container image layout consumed by `rules_oci`. A TAR is
the filesystem archive produced by a component's packaging target. These builds
create TARs, wheels and container images; they do not create DEB packages.

## Runtime component ownership

`docker-base-trixie` consumes the source packages for socat, iproute2 and SONiC's
OpenSSL, Kerberos and SymCrypt changes. OpenSSL's development files remain in this
base because Make installs `libssl-dev` here. Debian's separately packaged legacy
OpenSSL provider remains an imported APT dependency.

`docker-config-engine-trixie` adds Common's C++ library, database CLI and Python
binding; libnl; libyang and its Python binding; eventd; the Rust supervisor
listener; the config-engine, py-common, YANG management, containercfgd and Python
supervisor wheels; and the source-owned YANG models. The models package retains
its Python distribution metadata and both Python and CVL model directories, so
pip in Make-built child containers can satisfy config-engine's model dependency.
Eventd's separate rsyslog
plugin package is not an input to this image in Make and is not added here.
Libnl uses its precise runtime TARs, preserving `/etc/libnl-3` configuration and
CLI plugins while leaving development linker symlinks to the development TARs.

The shared image path normalizer places Common's database configuration under
`/run/redis/sonic-db`, preserving Debian's `/var/run -> /run` link. The original
component TARs remain graph inputs so this adaptation keeps their debug symbols.
CI rejects payload paths that traverse the preserved directory aliases.
Source-owned paths receive root ownership, and the database configuration uses
mode `0644`, so a developer checkout's uid or umask cannot leak into the image.

The base keeps Make's `vim-tiny` selection and hard link at `/usr/bin/vim`.
It declares the `editor`, `ex`, `rview`, `vi`, and `view` alternatives from the
pinned package's installation script, with each command resolving to
`/usr/bin/vim.tiny`. Debug images may replace these alternatives with full Vim.

Each native producer keeps its own runtime/debug split. The image's debug target
collects those providers through the base chain. SymCrypt's integrity-sealed core
uses build-ID lookup only: adding a debug-link section after sealing would change
the protected bytes. Its provider and engine use ordinary debug links and CRC
checks, as do the other native source packages.

The interpreter in the retained Make container was ordinary Debian Python,
not SONiC's host-image FIPS Python build. It remains a pinned Debian input here.
The five Python 3.13 packages use `3.13.5-2+deb13u4`; glibc uses
`2.41-12+deb13u3`, and Perl uses `5.40.1-6`. The Debian seed is the immutable
`trixie-20260713` manifest in `MODULE.bazel`; APT uses the shared July 2026
snapshots. Downstream exact-version development packages must use that same set.

Third-party Python dependencies use the one checked lock in
`src/sonic-config-engine/requirements_lock.txt`. This replaces the Make
Dockerfile's floating `pip install` selection with reviewed versions. Do not
claim byte-for-byte equivalence to a later floating Make build.

## Building before the dependency PRs merge

Distroless `0.9.4.sonic.5` resolves directly from the maintained registry.
Its published fixes provide the package headers, pkg-config files and concrete
package labels used by the source producers; no Distroless override is needed.

Normal registry URLs remain maintained `main` and BCR. `draft_sources.json`
temporarily names the exact reviewed registration commits or source archives
needed by this stacked image change. The preparation helper verifies each source
recipe, archive and overlay and asks Bazel's standard `http_archive` rule to apply
the original ordered patches. It does not maintain a second patch implementation.

```sh
python3 tools/bazel/ci/prepare_draft_sources.py \
  --manifest dockers/docker-base-trixie/bazel/draft_sources.json \
  --output "$PWD/target/bazel-draft-sources" \
  --receipt "$PWD/target/draft-sources.json" \
  --bazelrc-output .bazelrc.user

make -f tools/bazel/prepare_manifests.mk \
  'MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk' \
  BLDENV=trixie CONFIGURED_PLATFORM=vs CONFIGURED_ARCH=amd64 INCLUDE_FIPS=y \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y DOCKERS_PATH=dockers

bazel build //dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz
```

The generated, ignored `.bazelrc.user` sends the same exact source overrides to
builds, tests, queries and module-graph collection. The helper refuses to overwrite
a manually maintained user RC. Prepared paths must be visible from the build
environment: when using a SONiC slave container, run preparation inside that
container's mounted checkout. Fresh-checkout cache tests pass the recorded
override arguments explicitly because ignored files are not copied.

Remove a manifest entry when its version is available from registry `main`.
Do not replace the ordinary registry URLs with a private integration branch.
These temporary prerequisites keep the image PR in Draft until they land.

## Make selection and direct ARM64 validation

The existing `BUILD_WITH_BAZEL_WHEN_AVAILABLE=y` selector chooses these two
archives for native AMD64 VS/Trixie builds with FIPS enabled. Other Make profiles
retain their existing producer. Source-backed OCI targets also support direct
native ARM64 validation by selecting both target and host platform:

```sh
bazel build \
  --platforms=@sonic_build_infra//platforms:aarch64_trixie \
  --host_platform=@sonic_build_infra//platforms:aarch64_trixie \
  //dockers/docker-config-engine-trixie:docker-config-engine-trixie
```

Prepare Make manifests with `CONFIGURED_ARCH=arm64` for that build. This does not
enable a new ARM64 Make integration profile or turn the approved AMD64 cached
P4/DASH SDK into an ARM64 source build. That SDK remains a separate downstream
input to Syncd.

## Package state and validation

`installed_packages.py` preserves Debian control fields from the actual selected
packages, then replaces source-owned package entries using reviewed Make control
facts and the corresponding source TAR hashes. It checks required files and
records every native file, including plugins. It validates the final Debian
dependency relationships before publishing `/var/lib/dpkg/status` and a receipt
under `/usr/share/sonic/bazel`. No package maintainer script runs on the build host.
The source records use the actual target architecture, including SymCrypt, whose
legacy package declares `all` despite containing native libraries. This is an
image inventory; it is not a new DEB or a package-manager installation transaction.

The source contracts record their retained Make OCI manifest and status digest.
When changing a producer, update its version, source revision, installed paths
and verified control facts together. The image does not retain an old Debian
version as the claimed owner of overwritten source-built files.

`Bazel source base images` builds both architectures, verifies source hashes in
the assembled image, checks all source ELF/debug pairs, and runs the installed
config-engine image in a network-isolated container. It exercises cfggen
rendering, valid and invalid YANG data, Python imports, command scripts, dynamic
library loading, the legacy provider, and actual SymCrypt provider/engine hashes.
The owner's original suites remain in their separate producer workflows.

## Refreshing APT and Python inputs

Keep `apt.lock.json` checked in. `apt-resolve.MODULE.bazel` contains the image's
handwritten package roots for a clean preparation workspace using the shared
infrastructure snapshots. Resolve those roots with the upstream Distroless APT
extension; retain only these three dependency sets and their package closures
from its generated lock. Review versions and archive digests before replacing
the checked lock, then regenerate ordinary declarations with
`sonic-build-infra/apt/export_inputs.py --lock <lock> --module <apt_inputs.MODULE.bazel>`.
The imported `apt_inputs` repository generates the architecture-specific label
map from that same lock. There is no separately maintained checked label map.

For Python changes, edit the owning `requirements.in` and regenerate the shared
config-engine lock with the existing documented `uv pip compile` command. Keep
base-specific declarations in `dockers/docker-base-trixie/requirements.in`.
