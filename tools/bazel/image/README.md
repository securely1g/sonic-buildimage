# VS installer assembly

`//tools/bazel/image/vs:sonic-vs.bin` assembles an unsigned SONiC VS ONIE
installer for AMD64 Debian Trixie. Its inputs are a native host snapshot,
rendered host configuration, installer scripts, and **every installed and local
service archive**.
The existing Make and Bazel component producers continue to build those
archives. Orchagent uses the same archive boundary as every other service.

The public selector remains `BUILD_WITH_BAZEL_WHEN_AVAILABLE`. With `y`,
`make target/sonic-vs.bin` assembles already prepared inputs through Bazel and
publishes the installer atomically. With `n`, it uses the native Make installer
path. The default, component selection rules, and KVM image target are unchanged.

## Optional containerd store

After preparing the native inputs **and the finalized-host handoff below**, select the smaller content-only store:

```sh
make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y \
  ENABLE_SBOM=n SONIC_ENABLE_IMAGE_SIGNATURE=n SECURE_UPGRADE_MODE=no_sign \
  ENABLE_ASAN=n SONIC_INSTALL_DEBUG_TOOLS=n INSTALL_DEBUG_TOOLS=n \
  target/sonic-vs.bin
```

`USE_CONTAINER_IMAGE_STORE_AS_BACKEND` defaults to `n` and requires the Bazel VS
assembly path. Setting it to `n` returns to the classic Docker store. The
publisher records the selected backend and updates only the generated graph and
its receipt; it preserves the prepared archives, hashes and both source
identities. Bundles lacking the native producer evidence are rejected.

The opt-in converts every declared service archive with Skopeo, preserving image
configuration and tags while compressing layers with zstd. A private containerd
imports them with `ctr images import --no-unpack`. The cached base store contains
the other service images; Orchagent is added to a private restored copy. An
inventory without Orchagent uses one store action. No component source is built
by this assembly graph.

The installer ships complete shared layer blobs and image metadata without
unpacked snapshots. Docker unpacks an image when its first container is created;
later starts reuse those snapshots. This reduces installer size at the cost of
first-use time on the device. Host service metadata and local-package plugins
are prepared offline; the selected host output configures Docker to use the
private containerd store. See [the target runtime](containerd_runtime/README.md)
for extraction, startup and validation details.

The opt-in store uses zstd while retaining SONiC's `dockerfs.tar.gz` filename.
The installer uses its shipped GNU tar and decoder, including ownership,
permissions, extended attributes and ACLs. The default store keeps gzip and its
existing extraction path.

Full guest boot, concurrent cold-start readiness and forwarding are not
established by these assembly tests. OS-upgrade package migration is deferred to
[#74](https://github.com/securely1g/sonic-buildimage/issues/74): its existing
chroot helper does not start the selected containerd runtime.

## Declared inputs and execution

Start with an initialized recursive checkout and the normal native build
prerequisites. Run the existing outer Make commands so its distribution phases
and component producer selection remain in effect:

```sh
make init
make configure PLATFORM=vs PLATFORM_ARCH=amd64
make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y \
  ENABLE_SBOM=n SONIC_ENABLE_IMAGE_SIGNATURE=n SECURE_UPGRADE_MODE=no_sign \
  ENABLE_ASAN=n SONIC_INSTALL_DEBUG_TOOLS=n INSTALL_DEBUG_TOOLS=n \
  bazel-vs-native-inputs
```

The last command must run by itself. It builds the normal installer
prerequisites, including all installed and local service archives, and captures the host immediately
before container loading. It writes `target/bazel-native/provenance.json`, the
snapshot, rendered host inputs, and the evaluated image inventory. It does not
publish a finished installer. `target/bazel-native` must be absent before a new
capture; existing inputs are never silently adopted or overwritten.

The capture supports native AMD64 Trixie VS ONIE with unsigned, non-ASAN,
non-debug configuration. Multi-ASIC KVM, cross builds, reduced-size images,
Kubernetes, remote service packages, custom organization hooks, installer
post-build hooks, and SBOM generation are outside this assembly path. Leave
those features disabled and use `SECURE_UPGRADE_MODE=no_sign`.
The final Make command also rejects unsupported feature options before publishing
the prepared image.

Build the [execution worker](worker/README.md) and write its explicit immutable
identity to `target/bazel-image-worker.json`. Then freeze the producer outputs:

```sh
python3 tools/bazel/image/prepare_inputs.py \
  --native-directory target/bazel-native \
  --installer-source "$PWD" \
  --execution-environment target/bazel-image-worker.json \
  --output target/bazel-image-inputs

make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y \
  ENABLE_SBOM=n SONIC_ENABLE_IMAGE_SIGNATURE=n SECURE_UPGRADE_MODE=no_sign \
  ENABLE_ASAN=n SONIC_INSTALL_DEBUG_TOOLS=n INSTALL_DEBUG_TOOLS=n \
  target/sonic-vs.bin
```

Preparation requires the producer receipt, matching live producer source
identity, matching configuration, and the size/SHA256 of every declared file.
It rejects missing or extra service archives, source-target substitutions,
linked inputs, and an unsupported worker. It copies all archives into the input
bundle and creates the optional VS Bazel package only after preparation succeeds.
The input output directory must be fresh. Historical bundles without the required
producer evidence cannot enter this production path.

The prepared bundle records two identities separately:

- `native_source_identity` and `native_config_flags` identify the snapshot and
  service archive producer, including its requested build selector, image
  version, and epoch.
- `assembler_source_identity` identifies `--installer-source`; its installer
  scripts, configuration, and generated manifests are frozen with per-file
  hashes. Source identity and source-file hashes are rechecked after staging.
  A newer assembler does
  not relabel an older producer's snapshot or image version.

For the containerd backend, the native preparation job also finalizes the host.
The CI workflow first downloads the `unprivileged-repository-inputs` artifact,
which contains the reviewed GitLab, GNU and zlib build-tool source archives
selected by the module graph. Verify and carry those source archives into the
final handoff so a fresh runner does not depend on a warmed download cache:

```sh
python3 tools/bazel/gzip/source_archive.py \
  --archives-dir artifacts/unprivileged/repository-inputs \
  --output target/bazel-image-inputs/source-downloads
python3 tools/bazel/image/ci_unprivileged.py prepare-host --workspace "$PWD"
```

The CI helper rechecks each archive's pinned filename and SHA256 or SHA512
integrity before both host preparation and final assembly, and supplies the
verified locations to Bazel with `--distdir`.


This privileged preparation builds only the host target, then exports
`target/bazel-image-inputs/prepared-host/`: the SquashFS, boot and platform
archives, plus a receipt binding them to the source/configuration, worker,
implementation files, service metadata and local package bytes. Changed host
inputs require a new handoff. Final assembly verifies the receipt and every
output hash; it cannot silently fall back to privileged host finalization.

### Run Make inside an unprivileged CI runner

Use the prepared execution image as the runner container, with a writable
checkout/input bundle and Bazel output directory owned by UID/GID 1000. Keep
native Make production and host finalization in the separate preparation job.
The runner must have the same immutable execution image recorded by preparation.
For example, from the host after preparing the inputs:

```sh
worker=$(python3 -c 'import json; print(json.load(open("target/bazel-image-inputs/execution-environment.json"))["worker_image"])')
docker run --rm --init --user 1000:1000 --cap-drop=ALL \
  --security-opt=no-new-privileges:true -e HOME=/var/sonic-builder \
  --mount "type=bind,src=$PWD,dst=$PWD" \
  --mount "type=bind,src=$PWD/target/bazel-image-inputs/execution-environment.json,dst=/run/sonic-image-worker.json,readonly" \
  -w "$PWD" "$worker" \
  make BUILD_WITH_BAZEL_WHEN_AVAILABLE=y USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y \
    ENABLE_SBOM=n SONIC_ENABLE_IMAGE_SIGNATURE=n SECURE_UPGRADE_MODE=no_sign \
    ENABLE_ASAN=n SONIC_INSTALL_DEBUG_TOOLS=n INSTALL_DEBUG_TOOLS=n \
    target/sonic-vs.bin
```

No Docker socket, KVM device, capability additions, privileged mode or relaxed
seccomp profile is passed. Inside this runner, Make invokes Bazel directly;
it does not launch Docker or run native prerequisites. It rejects root,
nonzero effective/permitted/bounding capabilities, missing no-new-privileges,
a missing seccomp filter, exposed runtime sockets or a mismatched worker identity.
Bazel uses process-wrapper sandboxes because the runner cannot create privileged
Linux namespaces. Containerd only imports compressed content and metadata;
it does not mount layers or run service containers. The stored daemon files
have root ownership in the archive while image layer metadata remains intact.
The output is the ordinary executable `target/sonic-vs.bin`.

The new `Bazel VS unprivileged assembly` workflow transfers checksummed native
inputs and the exact execution image between separate preparation and assembly
jobs. It runs the public Make command inside the restricted runner, records
Docker configuration and in-container privilege evidence, and checks the final
installer's payload byte chain. Its hosted storage test also restores the
rootless-produced store in a separate privileged runtime container; running
containers is validation and is not part of installer assembly.

The public build command consumes this prepared bundle; it does not run native
production or regenerate inputs automatically. The build's successful Bazel
events identify the final output and its digest before publication to
`target/sonic-vs.bin`, with executable mode `0755`. A failed replacement leaves
the previous published installer intact. Switching back to native Make forces
a native rebuild when the retained installer came from Bazel.

## Assembly and cache boundaries

```mermaid
flowchart LR
    Make[Existing component and native Make graph] --> Archives[All service archives]
    Make --> Snapshot[Pre-container host snapshot]
    Make --> Config[Captured host scripts and configuration]
    Archives --> Metadata[Service metadata]
    Archives --> Imports[Independent private Docker imports]
    Snapshot --> Host[Final host filesystem and boot files]
    Config --> Host
    Metadata --> Host
    Archives -->|Local service packages| Host
    Imports --> Store[Classic overlay2 Docker store archive]
    Host --> Payload[Payload ZIP]
    Store --> Payload
    Payload --> Installer[ONIE installer]
    Scripts[Declared installer scripts] --> Installer
```

The host action depends on builtin service metadata and the captured host
configuration. Local service packages are full host inputs because installation
can copy files into the host. Each service archive has a separate import action
using a fresh private Docker daemon. Store assembly deduplicates shared layers
and reuses compressed layer fragments. Host finalization, store assembly, ZIP
creation, and ONIE wrapping have separate Bazel action boundaries.

The default store format is Docker 28.5.2 classic `overlay2`. Worker identity is
an explicit action input. Native host finalization and the default Docker import require local
privileged execution; the prepared-host containerd final graph runs unprivileged; neither consumes the host Docker socket. Filesystem
metadata that cannot be preserved by the store collector fails explicitly.

For direct investigation or repeated Bazel requests, use the launcher:

```sh
python3 tools/bazel/image/run.py \
  --workspace "$PWD" --mount-root "$PWD" \
  --worker-spec "$PWD/target/bazel-image-inputs/execution-environment.json" \
  --output-user-root "$PWD/target/bazel-image-state" \
  --persistent-worker sonic-vs-assembly \
  -- build //tools/bazel/image/vs:sonic-vs.bin
```

An optional `--repository-cache /absolute/cache/path` shares repository
downloads. The named worker retains its Bazel server and output state. Use the
same arguments with `--worker-action stop` and omit the trailing Bazel command
to stop it. Direct Bazel requests produce Bazel outputs; the public Make command
owns publication to `target/sonic-vs.bin`.

Launcher locks and ownership receipts live in a private sibling directory named
`.<output-directory>.sonic-image-worker`, owned by the invoking host account.
The parent directory must be writable by that account. This keeps lifecycle
updates working when Bazel changes its own output directory permissions. A live
worker recorded by an older launcher must be stopped with that launcher before
using the new control-state location.

### SWSS example: snapshot to finalized host

The retained AMD64 VS build showed these host filesystem changes:

| Host file | Prepared snapshot | Finalized host |
| --- | --- | --- |
| `/usr/lib/systemd/system/swss.service` | Absent | Unit calling `/usr/local/bin/swss.sh` to start, wait and stop |
| `/usr/bin/swss.sh` | Absent | Docker control script using `docker-orchagent:latest` |
| `/usr/local/bin/swss.sh` | Absent | SWSS lifecycle and dependent-service handling |
| `/var/lib/sonic-package-manager/packages.json` | Absent | Registers `swss` with repository `docker-orchagent` |
| `/etc/sonic/warm-reboot_order` | Absent | Shutdown order placing `swss` before `syncd` |
| `/usr/bin/swssloglevel` | Native executable | Wrapper running `docker exec ... swssloglevel` |

The [finalization template](../../../files/build_templates/sonic_debian_extension.j2#L1186)
installs the units and scripts from declared sources; image manifests determine
shutdown order. The host `libswsscommon` library was already present and unchanged.

## Validation scope

The existing OCI archive CI job runs the focused assembly, backend selection, OCI conversion, input handoff,
source-identity, worker-lifecycle, publication, and archive tests on AMD64 and
ARM64 runners. These are Python/Make/Git fixtures; ARM64 execution does not imply
ARM64 installer support. The ONIE tests exercise payload checksums and shell
extraction with synthetic payloads.

The focused archive tests alone do not perform a complete native-producer-to-ONIE
build. The separate unprivileged workflow covers that producer/assembly handoff;
neither workflow boots the installer or validates forwarding. The existing component tests and
native KVM build retain their own coverage. Real assembly validation must retain
its producer and assembler identities, input hashes, worker identity, and output
evidence separately from any future guest-boot results.

The optional backend also has a separate privileged AMD64 acceptance job. It
restores a populated store and verifies first-use unpacking, shared layers,
Docker metadata, image updates, removal, save/load and daemon restart. Its small
images exercise storage behavior; they do not establish SONiC guest readiness.

Inside the runner, `BAZEL_IMAGE_ARGS` accepts additional Bazel build flags. CI
sets `--remote_cache= --disk_cache=` with a fresh output root to demonstrate
that final assembly actions actually execute under the restricted account.
