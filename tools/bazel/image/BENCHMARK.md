# One-line SWSS incremental image benchmarks

## Latest result, October 1, 2026 UTC

A fresh one-line SWSS change took **131.710 seconds (2m 11.7s)**
through completed installer/runtime publication and five artifact SHA256 hashes.
This is **24.0% less time** than the previous
173.329-second batch-worker result. **The two-minute target was not reached;
this run finished 11.710 seconds above it.**

Worker/Bazel/cache/scheduling overhead decreased from **50.550 to 1.496
seconds (97.0% less)**. The first persistent-worker trial
with the original serial publication operations took **147.388 seconds**.
This follow-up also uses 1 MiB ONIE tar stream/copy buffers and three read-only
checksum workers after the output copies finish. Individual compile and packing
times also varied on the shared host; the entire difference between the two
persistent-worker trials is not attributed solely to these two changes.

| Stage | Seconds |
| --- | ---: |
| Compile changed SWSS source | 23.532 |
| Link orchagent | 10.966 |
| Runtime packaging, OCI/archive and service metadata | 14.219 |
| Import and package changed SWSS Docker layers | 30.942 |
| Merge cached Docker store parts | 15.562 |
| Assemble payload ZIP | 10.572 |
| Assemble ONIE installer | 13.638 |
| Worker/Bazel/cache/scheduling overhead | 1.496 |
| Source edit, publication, checksums and harness | 10.783 |
| **Continuous total** | **131.710** |

The action-processing intervals match all **19 actual executed spawns** and
charge the 0.080-second overlap once. One translation unit was compiled, one
orchagent linked, and one changed SWSS service imported. The finalized host
filesystem and the other 27 service imports were reused. Bazel reported two
action-cache hits; most prior cache lookups were bypassed by its retained
in-memory graph. The new marker is `persistent e2e benchmark 20261001-3bc02fd889fc`.

The final unchanged invocation took **1.739 seconds** with zero
executed spawns. The same JVM (PID 62) remained throughout warmup and measurement.
The pinned image, compiler/linker, source override, output root, UID-1000 account,
8-CPU quota, 24-GiB memory cap, eight jobs and CPU resource budget of eight match
the first persistent-worker trial. The existing T0 and previously boot-validated
standalone VS guest remained running. This is one trial per variant on a shared
16-CPU, approximately 49-GiB host, using prepared native OS/service predecessors.

The ONIE buffer change preserves tar headers, padding and checksums. A dedicated
regression compares exact default-buffer and 1 MiB-buffer bytes, including a
large payload and GNU long-path record. The unchanged-source cache refresh took
**31.968 seconds** and reproduced the first persistent trial's installer
SHA256 exactly. Earlier, the output-mode/cache changes reproduced the previously
boot-validated installer bytes, as documented below. Worker/JVM startup and these
preparatory runs are outside the warm source-edit timer.

All **58 unit checks** passed across the five Bazel test targets, including the
nine installer checks and 23 launcher checks. The unchanged independent verifier
passed on this exact measured output in **99.215 seconds**, outside
the timer: changed ELF/runtime provenance, full SquashFS decoding, ZIP CRC and
member bytes, boot/platform files, ONIE checksum and embedded payload, and the
effective native overlay2 SWSS filesystem. **No new guest boot or forwarding
coverage is claimed for this fresh-marker artifact.** Historical boot coverage
below applies to its identified artifact.

Installer size: **2,693,531,631 bytes**. SHA256:
`e2ee93784c0c426ae213be07836473884e45a16524876c513c8ffc29279d1287`.

### Reproducing the persistent loop and publication

Use [persistent launcher mode](README.md#persistent-developer-loop) with the same
worker name, output root, source overrides and startup flags throughout. Perform
a cache-population build, then an unchanged build with zero executed spawns.
Start the continuous timer immediately before replacing one SWSS startup-log
line with a fresh marker. Build the full installer target and retain profiles,
build events and execution logs. Keep worker/JVM startup outside this warm loop
and report it separately; process reuse is the intended optimization.

The benchmark publication is separate from `run.py`. Resolve the built installer
from the successful Bazel build events and the other outputs from actual action
arguments. Copy **both** the installer and runtime archive to a fresh publication
directory with `shutil.copy2`. After both copies finish, read and SHA256-hash those
two published files plus the produced `fs.squashfs`, `dockerfs.tar.gz`, and
`fs.zip` independently, with at most **three threads**. Do not reuse a digest when
files share content. Stop the timer only after all five hashes complete.
Publication and these checksums consumed **9.972 seconds** in this run.

The following Python fragment shows the exact publication operations; its path
variables are resolved from that invocation's build events/actions:

```python
from concurrent.futures import ThreadPoolExecutor
import hashlib
import shutil

publication_directory.mkdir(exist_ok=False)
files = {}
for name, source in (("sonic-vs.bin", installer),
                     ("docker-orchagent.gz", runtime_archive)):
    files[name] = publication_directory / name
    shutil.copy2(source, files[name])
files.update({"fs.squashfs": host_squashfs,
              "dockerfs.tar.gz": docker_store,
              "fs.zip": payload_zip})

def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

with ThreadPoolExecutor(max_workers=3) as pool:
    hashes = dict(zip(files, pool.map(sha256, files.values())))
# Stop the continuous timer here; retain sizes, paths and all five digests.
```

The original serial publication result remains documented below. The second
trial keeps the same output/checksum scope but changes its execution order.
Small fixtures verify both modes return the same five digests, perform two
copies, obey the concurrency bound and propagate checksum failures.

With residual overhead below 1.5 seconds, further progress now depends on the
actual work: the changed service import alone took **30.942 seconds**,
and full store/ZIP/ONIE packing took **39.772 seconds**.
Reducing those stages is a separate optimization from retaining the Bazel server.

Retained evidence includes `incremental-02-end-to-end-timing.json`,
`incremental-02-breakdown.json`, `incremental-02-independent-measurement-audit.json`,
`artifact-validation-02.json`, the unique source patch, raw profile/execution/build
logs, warmup byte-equivalence receipts and preserved first-trial reports. Large
native inputs, caches and installers are not committed.

## First persistent-worker trial with serial publication

Reusing the isolated worker and Bazel server reduced worker/Bazel/cache overhead
from **50.550 to 1.715 seconds (96.6% less)**. A fresh one-line
SWSS startup-log change took **147.388 seconds (2m 27.4s)** through
publication and five artifact checksums, compared with **173.329 seconds** in the
prior batch-worker trial: **15.0% less total time**.
The two-minute target was **not reached** in this trial. Executed image actions
and publication now account for almost all elapsed time.

| Stage | Seconds |
| --- | ---: |
| Compile changed SWSS source | 19.775 |
| Link orchagent | 11.173 |
| Runtime packaging, OCI/archive and service metadata | 14.035 |
| Import and package changed SWSS Docker layers | 32.161 |
| Merge cached Docker store parts | 18.605 |
| Assemble payload ZIP | 15.255 |
| Assemble ONIE installer | 16.778 |
| Worker/Bazel/cache/scheduling overhead | 1.715 |
| Source edit, publication, checksums and harness | 17.890 |
| **Continuous total** | **147.388** |

These are action-processing intervals matched to actual executed spawns;
overlapping OCI/metadata work is charged once. The residual overhead definition
and continuous timer boundary are unchanged from the preceding trial. Both
published files were copied and all five artifacts were hashed sequentially,
using the same harness operations as that baseline.

The final unchanged invocation took **1.928 seconds** and executed zero spawns.
The measured edit used the same retained JVM (PID 62 before/after), immutable
worker image, UID-1000 `lgh` account and `/var/lgh` home, Bazel output root,
source override, 8-CPU quota, 24-GiB memory cap, eight jobs and local CPU budget
of eight. The worker uses a private bridge network and no host Docker socket.
The existing T0 and the previously boot-validated standalone VS guest remained
running in the background on the shared 16-CPU, approximately 49-GiB host.

Bazel retained its analysis graph and a bounded 200,000-entry file-digest cache.
It reported **2 action-cache hits and 19 executed spawns**; in-memory graph reuse
avoided most of the 5,325 lookups reported in the previous batch invocation.
Exactly one source file was compiled, one orchagent was linked, and one changed
SWSS service was imported. The host filesystem and other 27 service imports
were reused. The new source marker is `persistent e2e benchmark 20261001-b8f540a9aa75`.

The three large archive actions now set Bazel's normal 0555 output mode before
exiting. This preserves a computed digest across Bazel's output-finalization
step; hashing and dependency invalidation remain enabled. The unchanged-source
warmup rebuilt these three actions plus an 81ms Sairedis configuration action in
88.497 seconds. Its installer remained byte-for-byte identical to the previously
boot-validated artifact (`18c12d130a38712f1bc5e2680f55c226335b57be53cc0bbb1cddf206df964094`).
That warmup and worker/JVM startup are excluded from the warm incremental timer.

All **57 unit checks** passed (34 existing image checks plus 23 new launcher
checks). The fresh-marker installer passed the same independent ELF, runtime-layer,
native overlay2, full SquashFS, ZIP and ONIE byte-chain checks in **100.278
seconds**, outside the build timer.
No new boot or forwarding test is claimed for that fresh-marker artifact.
The identical warmup artifact retains the prior documented boot evidence.

Installer size: **2,693,531,631 bytes**. SHA256:
`1e7de0768f42e82a955eb09a3895af3b4bab570ec31d9ea44c7930e07b24907a`.

Reproduce the persistent mode using [the launcher instructions](README.md#persistent-developer-loop).
Run a preparatory build and a second unchanged zero-spawn build with the same
worker name/output root/startup flags, then apply a fresh one-line marker. Keep a
continuous monotonic timer through final output copies and the same five SHA256
hashes. Preserve worker startup and cache/analysis population separately. The
warm OS/service predecessor scope remains the same as described below.

Retained evidence includes `incremental-01-end-to-end-timing.json`,
`incremental-01-breakdown.json`, `incremental-01-independent-measurement-audit.json`,
`warmup-01-independent-audit.json`, the actual source patch, profile, execution
and build-event logs. The elapsed times are one trial on a shared host.

## Previous batch-worker benchmark and boot validation

On 2026-09-30, a fresh one-line `orchagent/main.cpp` startup-log edit took
**173.329 seconds (2m 53.3s)** through publication of the completed VS ONIE
installer and artifact SHA256 hashes. The preceding Bazel-runtime/native-Make
path took **1,122.868 seconds (18m 42.9s)** at the same end-to-end boundary:
**84.6% less elapsed time, or 6.48× faster**. This is one successful measured
trial with warm caches and retained native predecessors.

### Measured breakdown

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

### Scope and cache preparation

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

### Repeating the measurement

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

### Validation and artifact identity

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
