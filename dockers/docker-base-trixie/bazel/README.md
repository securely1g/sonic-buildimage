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

## Image assembly structure

The base, config-engine and VS Syncd images use the same structure:

1. Select Debian packages with `apt_layer`, then use `sonic_layer` to remove
   duplicate paths and filter package-state files.
2. Add the source owners' runtime TARs and Python wheels.
3. Add configuration and package metadata, in an explicit order in `oci_image`.
4. Collect matching source debug symbols and export the Make-compatible archive.

Each image's `config/BUILD.bazel` owns its configuration files and Make-generated
labels. The base also needs accounts, certificates and Vim command links there;
config-engine and Syncd inherit that setup. The shared account rule reads the
actual base account files and adds only Redis (UID 100, GID 101); Syncd adds its
`_ssh` group (GID 104). It rejects conflicting names and IDs. Keep package selection, source
targets and the ordered image layers visible in the image's main `BUILD.bazel`.
The previous configuration target names remain aliases for existing consumers.

For example, to add a base configuration file, export the file in the image's
main `BUILD.bazel` and add its destination and mode in `config/BUILD.bazel`.
Adding a source runtime package still belongs in the main image definition and
its owner's package-to-TAR mapping in `BUILD.bazel`. The image's
`bazel/source_paths.json` lists only required paths. `sonic_layer` removes
repeated directory entries from Debian archives before applying the file filter;
Docker rejects repeated paths within a layer.

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

The reviewed dependencies add direct APT-lock imports, shared account and
installed-package helpers, and producer-owned runtime metadata. Their exact
versions and source recipes are listed in `draft_sources.json`. The image
consumes their existing runtime and debug TARs; it does not produce DEBs.

Normal registry URLs remain maintained `main` and BCR. `draft_sources.json`
temporarily names the exact reviewed registration commits or source archives
needed by this stacked image change. The preparation helper verifies each source
recipe, archive and overlay and asks Bazel's standard `http_archive` rule to apply
the original ordered patches. It does not maintain a second patch implementation.

`libyang-python@3.1.0.sonic.4` resolves directly from maintained registry `main`
after [registry #102](https://github.com/securely1g/sonic-bazel-registry/pull/102)
landed. Its source, ordered patches and runtime metadata are unchanged; no
libyang-Python source override is needed. Other unmerged recipes remain explicit
in `draft_sources.json`.

Sairedis `0.0.1-1a6eb685de84d73b5939d269af1dc3e1fa71ff50` and FIPS
`0.0.2-df34b541eb08d1a6a4b039e69c4d716c84566061` also resolve directly from
maintained registry `main`, following registry [#105](https://github.com/securely1g/sonic-bazel-registry/pull/105)
and [#106](https://github.com/securely1g/sonic-bazel-registry/pull/106). Their source
archives, patches and producer-owned runtime metadata match the reviewed recipes;
no source override is prepared for either module.

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
enable a new ARM64 Make integration profile. Syncd's DASH profile and its full
image validation remain AMD64-only; its source producers have their own native
architecture checks.

## Package state and validation

The shared `@sonic_build_infra//oci:installed_packages.bzl` rule preserves
Debian control fields from the selected APT packages and reads source package
identity from each producer's generated `runtime_packages.json` target. Its
existing Debian control and changelog supply package fields and versions; the
shared rule derives library dependencies from the actual runtime TARs and
declared target libraries. `BUILD.bazel` maps Debian package names to those TARs.
The image supplies required installed paths. The in-tree eventd and Rust
supervisor targets follow the same rule; their metadata JSON is no longer
maintained by hand.

The helper binds each record to its actual TAR label and selected owner module,
checks required files and records native file hashes and ownership. It validates
the final Debian dependency relationships before writing `/var/lib/dpkg/status`
and the receipt under `/usr/share/sonic/bazel`. Architecture comes from the build.
For sources in this workspace, the receipt says `workspace`; CI records the exact
checkout separately. No frozen image-side SHA claims to identify current source.

No package maintainer script runs on the build host. These records describe the
image inventory; they do not create a DEB or perform a package-manager transaction.

`Bazel source base images` builds both architectures, verifies source hashes in
the assembled image, checks all source ELF/debug pairs, and runs the installed
config-engine image in a network-isolated container. It exercises cfggen
rendering, valid and invalid YANG data, Python imports, command scripts, dynamic
library loading, the legacy provider, and actual SymCrypt provider/engine hashes.
The owner's original suites remain in their separate producer workflows.

## Refreshing APT and Python inputs

Keep one reviewed `apt.lock.json` for the base, config-engine and base-debug
package sets. `apt-resolve.MODULE.bazel` contains the small handwritten package
requests used only to refresh that lock. Resolve those requests with Distroless,
then review versions, architecture, dependencies and archive hashes.

Normal builds call `apt.import_lock(lock = "//dockers/docker-base-trixie/bazel:apt.lock.json")`.
Distroless imports the exact locked archives and exposes every package's public
payload and control targets without resolving current repository indexes. The
shared `apt_inputs` repository derives BUILD labels from the same lock. Neither
an expanded `apt_inputs.MODULE.bazel` nor a second hand-maintained package list
is needed.

For Python changes, edit the owning `requirements.in` and regenerate the shared
config-engine lock with the existing documented `uv pip compile` command. Keep
base-specific declarations in `dockers/docker-base-trixie/requirements.in`.

`MODULE.bazel` records the selected infrastructure, Distroless and FIPS versions.
The root infrastructure selection keeps older consumer declarations from choosing
an obsolete compiler or execution-tool runtime. FIPS owns the GNU tool runtime
files required by SymCrypt and the OpenSSL, OpenSSH, Kerberos and SymCrypt
runtime/debug TAR contracts.

During review, `draft_sources.json` records every selected registration still
awaiting landing, including shared infrastructure and producer package metadata.
Its integrity-checked overrides apply to all build, test and query commands.
Remove each override only after that exact compatible registration is available
from the maintained registry `main` endpoint.
