# Source-built Trixie base images

These targets build `docker-base-trixie` and `docker-config-engine-trixie` from a
pinned Debian seed, reviewed Debian packages, and the public source-build targets
owned by SONiC components. For example, changing Common's C++ source rebuilds its
runtime package and the config-engine image layer; unchanged Debian inputs remain cached.

## Public targets

| Owner package | Target | Output |
| --- | --- | --- |
| `//dockers/docker-base-trixie` | `:docker-base-trixie` | OCI image |
| `//dockers/docker-config-engine-trixie` | `:docker-config-engine-trixie` | OCI image using the source-built base |
| Either owner | `:<image>.gz` | Make-compatible Docker archive |
| Either owner | `:<image>.debug_symbols` | Matching source ELF debug files under `/usr/lib/debug/.build-id` |
| Either owner | `:runtime_package_metadata` | Installed package versions, source revisions and native file hashes |

OCI means the standard container image layout consumed by `rules_oci`. A TAR is
the filesystem archive produced by a component's packaging target. All source
components, including FIPS OpenSSL and the distro-library replacements, supply
runtime/debug TARs directly. Python packages keep their wheel layers. Only
existing, checksum-pinned Debian archives are installed by APT; this image graph
creates no DEBs. Their genuine package records describe the installed Debian
baseline, while separate provenance identifies the source TARs overlaid on it.

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
Libnl uses its original runtime package selections, preserving `/etc/libnl-3`
configuration and CLI plugins while leaving development linker symlinks to the
development packages.
The real Debian `libnl-cli-3-200` baseline stays installed because packages such
as `libteam5` depend on it. Its ten unused plugins under `libnl/cli` are excluded
through standard dpkg path filters before installation. The source TAR provides
all ten plugins under its compiled `libnl-3/cli` directory. Provenance and image
checks require the source plugins and reject the excluded Debian copies,
including after a later APT transaction.

The shared image path normalizer places Common's database configuration under
`/run/redis/sonic-db`, preserving Debian's `/var/run -> /run` link. The original
component TARs remain graph inputs so this adaptation keeps their debug symbols.
CI rejects payload paths that traverse the preserved directory aliases.
Source-owned paths receive root ownership, and the database configuration uses
mode `0644`, so a developer checkout's uid or umask cannot leak into the image.

The base keeps Make's `vim-tiny` selection. Its original installation script
registers the editor commands, and Less registers `pager`, preserving the
inherited `more` fallback. Debian owns those alternative records and updates
them when packages are installed or removed.

The image also provides `/usr/bin/vim -> vi`. This compatibility link follows
Debian's selected editor instead of retaining an old executable through Make's
hard link after an upgrade. It is image configuration, not a fabricated Vim Tiny
package entry. Removing Vim Tiny leaves no executable behind through that link.
Syncd debug installs full Vim, which manages its own `vim` command; removing it
normally removes that command and restores `vi` to Vim Tiny. Orchagent debug
retains Vim Tiny.

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

This draft selects the shared installer for existing Debian inputs and the
source owners' public runtime TAR targets. It does not depend on their DEB
packaging branches. Exact source commits, archive digests and registry recipes
are recorded in `draft_sources.json`; review and refresh those inputs together
when a prerequisite changes.

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

python3 tools/bazel/oci/native_build.py build //dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz
```

The generated, ignored `.bazelrc.user` sends the same exact source overrides to
builds, tests, queries and module-graph collection. The helper refuses to overwrite
a manually maintained user RC. Prepared paths must be visible from the build
environment: when using a SONiC slave container, run preparation inside that
container's mounted checkout. Fresh-checkout cache tests pass the recorded
override arguments explicitly because ignored files are not copied.

The native launcher runs installation actions in the digest-pinned Trixie build
container and mounts declared source overrides read-only. It requires a usable
local Docker daemon. CI uses the same environment directly on native workers;
package scripts run in a separate filesystem and network namespace.

Remove a manifest entry when its version is available from registry `main`.
Do not replace the ordinary registry URLs with a private integration branch.
These temporary prerequisites keep the image PR in Draft until they land.

## Make selection and direct ARM64 validation

The existing `BUILD_WITH_BAZEL_WHEN_AVAILABLE=y` selector chooses these two
archives for native AMD64 VS/Trixie builds with FIPS enabled. Other Make profiles
retain their existing producer. Source-backed OCI targets also support direct
native ARM64 validation by selecting both target and host platform:

```sh
python3 tools/bazel/oci/native_build.py build \
  --platforms=@sonic_build_infra//platforms:aarch64_trixie \
  --host_platform=@sonic_build_infra//platforms:aarch64_trixie \
  //dockers/docker-config-engine-trixie:docker-config-engine-trixie
```

Prepare Make manifests with `CONFIGURED_ARCH=arm64` for that build. This does not
enable a new ARM64 Make integration profile or turn the approved AMD64 cached
P4/DASH SDK into an ARM64 source build. That SDK remains a separate downstream
input to Syncd.

## Package state and validation

The shared `rules_distroless` `apt_install` rule installs selected, original
Debian archives in a private filesystem. APT and dpkg run their original
configuration scripts and triggers. For example, the certificate package creates
`/etc/ca-certificates.conf`, and Vim registers its editor alternatives. These
real package records remain unchanged when source TARs replace selected files.

`installed_source_metadata.py` permits a source TAR to replace only files owned
by its same-named Debian baseline package. It verifies the exact source bytes, symlink targets, modes
and numeric ownership. Undeclared collisions and changes to package-manager
state fail the build. The source declarations contain provenance and required
paths, not copied package controls.

The image retains `base-packages.json` or `config-engine-packages.json` under
`/usr/share/sonic/bazel`, bound to the exact installation receipt and filesystem.
These receipts distinguish the actual Debian baseline version from the source
revision/version. APT dependency resolution uses the genuine baseline records;
it does not treat a newer source TAR as a fictitious installed package version.
Ordinary SONiC programs without Debian packages remain outside dpkg's database.

Each image generates standard APT preferences from the same declarations. A
`Pin-Priority: -1` entry prevents normal APT version selection from replacing
source-overlaid packages. It is not a guarantee against explicit removal,
direct `dpkg` commands or administrator overrides. Supported transactions add
unrelated packages while preserving source files. The build retains Debian's
configuration effects and uses the source graph's matching debug TARs.

Upstream `umoci repack` converts the configured filesystem into a delta against
its actual OCI base, including deletions, while preserving inherited image
settings. Matching source debug symbols remain separate TAR outputs. See
[the shared installation workflow](../../../tools/bazel/oci/README.md#keep-dpkg-state-with-the-installed-files).

`Bazel source base images` is configured to build both architectures, verify
source hashes in the assembled image, check all source ELF/debug pairs, and run
the installed config-engine image in a network-isolated container. It exercises cfggen
rendering, valid and invalid YANG data, Python imports, command scripts, dynamic
library loading, the legacy provider, and actual SymCrypt provider/engine hashes.
The owner's original suites remain in their separate producer workflows.

The workflow checks initial configuration, `dpkg --audit`, and `apt-get check`
on native AMD64 and ARM64. It then installs `qrencode` and its `libqrencode4`
dependency with APT, executes the new command, and checks that existing package
versions and source files did not change. Config-engine's commands, Python
imports, YANG validation and FIPS provider checks run again in that same container.
The goal is to let users install extra packages; managing every SONiC component
with `apt remove` or `apt upgrade` is outside this change.
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

## Configure the foreign Debian packages first

`//dockers/docker-base-trixie:configured_debian` now installs the existing
`base_debian` lock selection with `rules_distroless`'s `apt_install`. For example,
CA configuration and editor alternatives come from the actual Debian installation
scripts. `configured_debian_receipt` records the original archive hashes, installed
versions, and completed APT/dpkg checks.

This manual target is a complete configured Debian filesystem TAR. It is an
intermediate: it does not contain the source-built SONiC replacements, Python
wheels, or SONiC configuration. The production target overlays the verified source-owned runtime TARs, then
adds Python wheels, provenance, APT preferences and SONiC configuration.
Config-engine repeats these stages on the completed source-built base.

The input is the pinned official Debian image's one existing gzip layer, copied
verbatim after OCI digest and platform validation. Multiple-layer bases are
rejected; they must first be materialized with their whiteouts applied. The new
stage's complete output replaces the private filesystem before `umoci repack`
computes the OCI delta; this preserves removed package files as OCI whiteouts.

The `Configure Debian packages` CI job runs on native AMD64 and ARM64 in the
same digest-pinned Trixie build container as the shared rule's integration test.
It grants Bubblewrap `SYS_ADMIN` and `NET_ADMIN` for namespace setup, then drops
those capabilities before package scripts run. It audits the action graph for
DEB generation, checks initial configuration before any repair transaction, and
runs the additional-package install probe using the same image validator. No source packages or test DEBs are created by this job.

Common resolves from registry `main` at its existing source version; it no longer
needs a temporary override. The reviewed draft dependencies in `draft_sources.json` supply the upstream
installer and source TAR producers. Shared infrastructure resolves from its
landed registry version; source DEB production tools are not selected.
