# One-line SWSS incremental image benchmark

On 2026-09-30, a fresh one-line `orchagent/main.cpp` startup-log edit took
**173.329 seconds (2m 53.3s)** through publication of the completed VS ONIE
installer and artifact SHA256 hashes. The preceding Bazel-runtime/native-Make
path took **1,122.868 seconds (18m 42.9s)** at the same end-to-end boundary:
**84.6% less elapsed time, or 6.48× faster**. This is one successful measured
trial with warm caches and retained native predecessors.

## Measured breakdown

| Stage | Seconds |
| --- | ---: |
| Compile changed SWSS source | 18.541 |
| Link orchagent | 8.283 |
| Strip/package runtime, OCI, archive export/compression, service metadata | 12.653 |
| Import and package changed SWSS Docker layers | 32.119 |
| Merge cached Docker store parts | 13.396 |
| Assemble payload ZIP | 11.886 |
| Assemble ONIE installer | 14.596 |
| Worker/JVM startup, analysis, input/cache checks, scheduling and finalization | 50.550 |
| Source edit, publication, checksums and harness overhead | 11.304 |
| **Continuous total** | **173.329** |

Rows are rounded. The action-processing intervals come from the Bazel profile,
matched to actual executed spawns. A 0.058-second OCI/metadata overlap is counted
once. Subprocess times are narrower than these intervals and are not additional
elapsed time. The shared worker/Bazel row is the measured invocation minus the
union of those intervals; it is not attributed to individual build stages.

Bazel recorded **5,325 action-cache hits and 19 successful executed spawns**:
one C++ compile, one link, runtime/container packaging, one SWSS store import,
one store merge, ZIP and ONIE packaging. The service-metadata projection ran,
but its content was unchanged. **Host finalization and all 27 other service
imports were cached.** Bazel's executed-action count also includes one internal,
unconditional workspace-status action.

The old approximately **9m 06.367s native image stage** became **57.606s** of
metadata/import/store/host/payload action processing. These stage boundaries
differ: the old stage included native setup and input verification, while the
new subtotal excludes overhead shared by the whole Bazel invocation. Use the
18m 42.9s to 2m 53.3s total-to-total comparison for the end-to-end speedup.

## Scope and cache preparation

The target is `//tools/bazel/image/vs:sonic-vs.bin`: unsigned amd64, Debian
Trixie, Docker 28.5.2 with `overlay2`. A matching pre-container host snapshot,
rendered configuration/services, other service archives, config-engine base and
Scapy wheel must already exist and be prepared as declared inputs. Their source
builds are outside this graph and measurement. See [input preparation](README.md).

Both measurements used 8 CPU quota and 8 Bazel jobs. The final graph also sets
`--local_resources=cpu=8`. Its unified privileged native worker used 24 GiB;
the old runtime-only worker used 20 GiB and the old native stage used 24 GiB.
The workers differ, but the audited GCC 14.2.0-19/binutils 2.44 tools, compiler
arguments, linker arguments/response file, and consumed toolchain inputs match.
An appended PATH entry changed action environment keys, so cache population was
performed before timing. These actions use the pinned worker's system tools;
remote execution was not tested and is disabled for the privileged actions.

Worker image ID:
`sha256:a9bc88834ce59cd378af64c4d423e2e875cfa4e94b3af1d595bc7a301f876009`.

| Preparatory invocation, excluded from incremental total | Seconds |
| --- | ---: |
| Initial successful cache population | 669.302 |
| Following unchanged invocation | 43.820 |
| Cache population after DockerFS root-metadata correction | 416.606 |
| Final unchanged invocation before measured edit | 45.149 |

The final unchanged invocation executed zero spawns and had 5,344 cache hits.
Preparatory runs already had compiler and predecessor caches; they are not cold
build timings. An earlier measured trial failed independent validation because
the store archive omitted native root-directory metadata. That trial and its
failed receipt were preserved; the implementation was fixed and this result was
remeasured with a new unique source marker, retaining the verification checks.

## Repeating the measurement

1. Prepare the inputs described in the README and use a dedicated SWSS worktree
   based on `ec16673dbd9ddbdb98a9bd2c341fae8c7a37d1fc`. Keep the buildimage
   source, generated input bundle, worker image, compiler options, mount paths
   and Bazel output root unchanged throughout warmup and measurement.
2. Build the final target once to populate caches, then build it unchanged and
   verify there are no executed spawns. Do not clear caches between invocations.
3. Start a monotonic timer before changing the single startup-log line in
   `orchagent/main.cpp`. Add a fresh unique marker so no previously compiled
   benchmark ELF can satisfy the build. Invoke the final target with the same
   command/resources and retain a profile, execution log and build-event log.
4. Resolve the successful installer from the build-event output, copy it and the
   runtime Docker archive into a fresh results directory, and hash those copies
   plus the produced `fs.squashfs`, `dockerfs.tar.gz` and `fs.zip`. Stop the timer
   after publication and all five SHA256 hashes. Include worker/JVM startup in
   this timer. Input preparation, cache population and artifact validation are
   measured separately.
5. Match executed-spawn output paths to profile action-processing events; merge
   overlapping intervals before adding stages. Confirm one SWSS compile/link,
   one changed-service import and no host-finalization action. An action-cache
   hit count or successful target alone does not prove these boundaries.

The reusable launcher can collect the required Bazel evidence. Substitute
absolute paths within the explicitly selected build area, ensure its writable
paths are accessible to worker UID/GID 1000, and use a fresh result directory for
each invocation:

```sh
python3 tools/bazel/image/run.py \
  --workspace /absolute/build-area/sonic-buildimage \
  --mount-root /absolute/build-area \
  --worker-spec /absolute/build-area/sonic-buildimage/target/bazel-image-inputs/execution-environment.json \
  --output-user-root /absolute/build-area/bazel-state \
  --repository-cache /absolute/repository-cache \
  -- build \
  --override_module=sonic-swss=/absolute/build-area/sonic-swss-benchmark \
  --profile=/absolute/build-area/results/profile.json.gz --noslim_profile \
  --experimental_profile_include_target_label \
  --experimental_profile_include_primary_output \
  --build_event_json_file=/absolute/build-area/results/bep.jsonl \
  --execution_log_json_file=/absolute/build-area/results/execution.json \
  //tools/bazel/image/vs:sonic-vs.bin
```

The launcher provides the 8-CPU/24-GiB worker and batch JVM. If the worker lacks
the default `/usr/local/bin/bazel`, pass `--bazel` with the absolute executable
path inside the mounted build area. Keep any required repository overrides,
distdir or Java trust-store startup options identical across runs. The example
collects build evidence; the continuous timer and publication steps above are
also required for the reported end-to-end boundary.

## Validation and artifact identity

All 34 metadata, host, store and ONIE packaging unit checks passed. Independent
validation of this measured output took 88.547 seconds, outside the build timer:
it checked the unique changed orchagent bytes through the runtime archive and
effective Docker overlay graph, fully decoded SquashFS, checked ZIP CRC/member
hashes and boot/platform content, and verified the ONIE checksum and embedded
payload byte chain. Separate live Docker store fixtures checked restore,
dynamic-loader resolution, save/reload and whiteout semantics.

The exact measured installer subsequently passed native ONIE installation and
booted a fresh isolated VS guest. Live verification passed with the expected
SWSS image ID, orchagent SHA256 and unique startup marker. Database, SWSS and
syncd were active with zero container restarts; all 15 required SWSS processes
(including countersyncd) and syncd were running. All five Redis databases
responded. All 32 ports were present in configuration, application, state and
ASIC data, with kernel interfaces, state `ok`, matching bidirectional VID/RID
mappings, `PortConfigDone` count 32 and `PortInitDone` present.

This used QEMU q35/TCG with 4 guest vCPUs and 10 GiB RAM, inside an isolated
8-CPU/16-GiB worker. Installation and boot validation are outside the build
timer. An initial readiness probe ran before first-boot process/port startup
completed and failed; its receipt was preserved, and the unchanged verifier
passed after initialization. `show version` reports the retained host stamp
`SONiC.bazel.0-6e1eb6baa`; the changed runtime is identified by its image/binary
hashes and marker.

The inherited `watchdog-control.service` failure and virtio `NETDEV WATCHDOG`
TX timeouts on the isolated data NICs were recorded. This is a boot and service
check, without a forwarding test. The existing T0 remained unchanged, with
zero restarts and all eight BGP sessions established at its postcheck.

Measured marker: `cacheable e2e benchmark 20260930-e7b549c56f1e`.

Installer size: **2,693,531,631 bytes**. SHA256:
`18c12d130a38712f1bc5e2680f55c226335b57be53cc0bbb1cddf206df964094`.

The retained evidence includes `incremental-02-end-to-end-timing.json`,
`incremental-02-breakdown.json`, `independent-measurement-audit-02.json`,
`artifact-validation-02.json`, the source patch, raw profile/execution/build-event
logs, compiler-equivalence audit and the prior native baseline receipts. Large
input bundles, cache directories and generated images are not committed.
