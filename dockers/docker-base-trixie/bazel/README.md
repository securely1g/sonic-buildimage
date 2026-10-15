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
The shared alternatives initializer registers `editor`, `ex`, `rview`, `vi`,
and `view` with the real pinned `update-alternatives` tool, using priority 15
from Vim Tiny's installation script. It also registers Less as `pager` at
priority 77, preserving the inherited `more` fallback. Both the command links
and Debian's alternatives records enter the image, so package removal can clean
up or switch those links. The generated Vim Tiny ownership list includes the
additional `/usr/bin/vim` hard link. Syncd debug adds full Vim as a higher-priority
choice; Orchagent debug retains Vim Tiny.

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

Sairedis `0.0.1-befd501a41549c9fedea9a116d081a6ad542cbe3` also resolves directly from
maintained `main`, using its landed source commit without a draft override.

Common selects landed source `db8f451c76fdc1135cd40f585c218e7d589a7223` through
its reviewed registry #57 recipe. Its shared SWIG API comes from the refreshed
combined infrastructure recipe, which also retains required sysroot and Kerberos
source-build helpers until their individual proposals land.

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
dependency relationships before passing the status to the
[shared dpkg-state layer](../../../tools/bazel/oci/README.md#keep-dpkg-state-with-the-installed-files).
That layer writes `/var/lib/dpkg/status`, file ownership lists, payload checksums,
configuration-file hashes, scripts and trigger registrations. It reuses upstream
APT data/control targets and exact reviewed source-package controls; its file
lists describe the new source TARs, not the old Make binaries.

The images retain `base-dpkg-state.json` and `config-engine-dpkg-state.json`
under `/usr/share/sonic/bazel`, respectively. These receipts bind the generated
metadata to its inputs and installed bytes. No package maintainer script runs on
the build host. Initial configuration effects, such as Vim's alternatives and
the FIPS provider setup, remain declared image inputs; preserving `postinst`
does not prove that its initial work has run.

The source records use the actual target architecture, including SymCrypt, whose
legacy package declares `all` despite containing native libraries. Source control
captures retain the original version, architecture, archive SHA-256 and exact
script bytes. Only reviewed substitutions, such as the Python libyang package
qualifier for ARM64, are permitted. The ordinary build creates no DEBs.

The source contracts record their retained Make OCI manifest and status digest.
When changing a producer, update its version, source revision, installed paths
and verified control facts together. The image does not retain an old Debian
version as the claimed owner of overwritten source-built files.

`Bazel source base images` is configured to build both architectures, verify
source hashes in the assembled image, check all source ELF/debug pairs, and run
the installed config-engine image in a network-isolated container. It exercises cfggen
rendering, valid and invalid YANG data, Python imports, command scripts, dynamic
library loading, the legacy provider, and actual SymCrypt provider/engine hashes.
The owner's original suites remain in their separate producer workflows.

The workflow also runs real APT transactions in fresh native AMD64 and ARM64
containers for both images. It installs and upgrades a locked Debian `hello`,
upgrades the image's source-built socat to `1.8.0.3-1+deb13u1`, and checks removal
and purge of socat and Vim. These checks must leave a clean `dpkg --audit` and
`apt-get check`. They retain transaction logs and receipts; the new image checks
are required validation, not a claim that this revision has already passed CI.
This native base/config-engine coverage does not enable ARM64 Orchagent or Syncd.

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
