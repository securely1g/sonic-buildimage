# Direct Make execution for the VS image

The standard Ubuntu 22.04 hosted VS job uses a plain, pinned Debian Trixie
container. It runs the existing `make -f slave.mk` recipes there and calls Bazel directly for the
registered producers. It neither builds nor launches a sonic-slave image.
Docker is still required to build and inspect service containers, and the
unchanged `sonic-vs.img.gz` recipe still installs and boots a KVM guest.

`bootstrap.sh` must run as root in that disposable container. `VS_UID` and
`VS_GID` identify the host checkout owner; later steps use the `vsbuild` account
and `/home/vsbuild`. The workspace must have the same absolute path on the host
and in the container so native Docker bind mounts address the same files.
An additional `/sonic` symlink supports retained P4RT source recipes that use
that literal path. No host packages are changed.

## Setup inputs

- `apt-packages.txt` contains execution/image tools and native development or
  test dependencies omitted from the retained source packages' controls.
- `debian-sources.txt` identifies the Debian source recipes whose `Build-Depends`
  supply the rest of the native build environment. This installs their build
  tools, not their runtime binaries as substitutes for SONiC source outputs.
  The SONiC Make rules still select the actual source versions and patches.
- `requirements.txt` supplies Python build/test tools for legacy Make recipes.
  Explicit versions match the maintained native recipe. Its unversioned entries
  retain the existing builder's behavior; the installed versions are recorded.
- `rules/config` and `rules/sonic-fips.mk` supply the FIPS Go package URLs and
  versions. The existing Trixie recipe supplies only its explicit Rust tool
  versions; its Dockerfile commands are not evaluated or executed.
- `src/sonic-build-hooks`, `scripts/generate_buildinfo_config.sh`,
  `scripts/versions_manager.py`, and `scripts/build_mirror_config.sh` preserve
  the existing package-version capture and hook setup. Generated hook data still
  uses `sonic-slave-trixie/buildinfo` because `slave.mk` and service-image rules
  consume that location. A directory name does not start a builder container.

Current Debian libnl3 and Net-SNMP source records have build conflicts with
packages needed by other SONiC components. They are not used as blanket
`apt-get build-dep` inputs: their compiler dependencies are explicit in
`apt-packages.txt`, matching the retained SONiC source recipes. No `--force`,
APT dependency override, or new package definition is introduced.

## Runtime contract

The workflow supplies the host Docker socket, `/dev/kvm`, and privileges for
filesystem mounts/chroots. It sets `SONIC_CONFIG_USE_NATIVE_DOCKERD_FOR_BUILD=y`
and the Docker tag/lock variables normally passed by Makefile.work. Bootstrap
adds the build user to the socket and KVM groups and permits its existing sudo
operations. Docker service builds and native package builds retain their
original Make rules.

The host matches the standalone kernel CI environment's unprivileged user
namespace support. Before Make runs, the workflow checks namespace creation
and the embedded Bazel Linux sandbox helper as `vsbuild`, including isolated
network execution. A failed probe stops the job without relaxing the kernel's
required `linux-sandbox` strategy.

The workflow also mounts `target/bazel-kernel-state` at `/k` and sets
`SONIC_BAZEL_KERNEL_STATE_ROOT=/k`. This shortens the automatic kernel scratch
directories enough for Speakup's 256-byte filename buffer, while preserving
fresh directories, cleanup after success, and receipts in the host checkout.
The kernel source, patches, package outputs and provenance stay unchanged.

Later `docker exec` invocations must carry:

```text
HOME=/home/vsbuild
CARGO_HOME=/usr/.cargo
RUSTUP_HOME=/usr/.cargo
PATH=/usr/local/go/bin:/usr/local/bin:/usr/.cargo/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
```

`docker exec` does not read `/etc/profile.d` automatically. Set `BUILD_WORKDIR`
consistently on all direct Make calls. Configure first with `PLATFORM=vs`,
`PLATFORM_ARCH=amd64`, and `TARGET_BOOTLOADER=grub`; preserve the workflow's
FIPS/DASH and native architecture flags for the actual image build.

## Validation evidence

Bootstrap retains the selected native tool versions, complete installed Debian
and Python package versions, and setup-input hashes under
`artifacts/vs/native-bootstrap`. Those receipts describe the build environment;
only a successful full image build and its existing validation establish that
the whole selected source closure is supported. A successful dependency solver
or bootstrap by itself is not full VS image validation.
