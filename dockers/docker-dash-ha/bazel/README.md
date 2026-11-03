# Source-built DASH HA images on AMD64

The runtime image retains `docker-swss-layer-trixie` as its parent. It adds the
HA owner's `dash_ha_pkg` TAR and the unchanged Docker Supervisor configuration.
The native install manifest contains `hamgrd`, `swbusd`, `swbus-cli` and
`wait_for_loopback.py`; the producer's separate `container` utility is not an
installed HA package file.

| Runtime input | Declared producer |
| --- | --- |
| HA programs, loopback script and package identity | `@sonic_dash_ha//:dash_ha_pkg` and `:runtime_packages.json` |
| Reusable Common Rust/native library | Common's own targets, selected once through `sonic-swss-common` |
| Original HA compatibility protobuf schema | `sonic-dash-api` owner interface; HA retains both original Rust generators |
| SWSS programs, patched libteam/libteamdctl, SAI metadata and DASH API | `//dockers/docker-swss-layer-trixie:source_runtime`, inherited from that image |
| Config-engine, Common Python/native bindings, YANG and utilities | Source-owned outputs in the inherited config-engine image |
| FIPS OpenSSL/Kerberos/SymCrypt, iproute2, socat and libnl | Source-owned outputs in the inherited base image |
| External Debian runtime libraries and debug tools | Reviewed AMD64 APT lock and shared checked `apt_layer` selection |
| FIPS OpenSSH client in the debug image | `@sonic_fips//openssh:openssh_client_pkg` with matching symbols |

The debug image extends the exact runtime layers. It adds the inherited Make
debug tool set, the source-built FIPS OpenSSH client, the `_ssh` group used by
`ssh-agent`, and matching symbols for the HA and inherited native payloads.
The owner metadata and the actual selected source MODULE files are inputs to
the installed package records. No DEB package producer is added or invoked.

The APT lock uses the same immutable Debian snapshots as the source parent.
Its runtime roots are the HA ELF dependencies `libc6` and `libgcc-s1`, which
the parent normally already satisfies. The debug and private Redis roots reuse
the reviewed MACsec image selection; all original package hashes and dependency
edges are retained. `refresh/MODULE.bazel` and `refresh/export_lock.py` describe
the standard Distroless regeneration. The test Redis archive never enters the
production image.

## Build and validate

While prerequisites are under review, the existing shared review launcher uses
one complete immutable SONiC registry catalog plus BCR. The ordinary `.bazelrc`
continues to use maintained main. The review catalog selects HA source PR #1 and
registry #203, shared Rust #198, API #202, Common #201, infra #195, FIPS #197,
Distroless #200 and the existing native parent dependencies. It does not change
previously published version directories.

Run these commands from the repository in a native AMD64/Trixie environment:

```sh
python3 -B tools/bazel/ci/dash_ha_image.py build \
  --bazel tools/bazel/ci/libteam_draft_bazel.sh --artifacts artifacts/dash-ha
python3 -B tools/bazel/ci/dash_ha_image.py validate --artifacts artifacts/dash-ha
python3 -B tools/bazel/ci/dash_ha_image.py incremental \
  --bazel tools/bazel/ci/libteam_draft_bazel.sh --artifacts artifacts/dash-ha
```

The build audits the complete configured action graph before execution. The
separate validation compares source/configuration bytes, inherited files,
package records, AMD64 ELFs, debug IDs, tags and OCI/archive contents. The
incremental check verifies unchanged output bytes, edits the real Supervisor
input, checks that both images change while source/parent outputs stay fixed,
and restores the exact original archives.

With Docker available, run the installed check against the exported artifacts:

```sh
python3 -B tools/bazel/ci/dash_ha_loaded.py --artifacts artifacts/dash-ha
```

The disposable container has its own network namespace, private Redis and a
dummy `Loopback0`. It runs the original Supervisor configuration with `DEV=dpu0`,
checks service/listener states and stable program PIDs, then changes the real
PMON Redis input. The installed `hamgrd` must write the correct latched
`DPU_RESET_INFO|DPU0` record. A second real `swbusd` exercises software peer
routing with positive and unreachable-peer checks. No physical DPU, ASIC
forwarding or HA set/scope programming completion is fabricated.

The existing `bazel-oci.yml` AMD64 job builds and verifies these artifacts; its
dependent Docker job checks installed behavior. Make retains the legacy
packages, include flag, config/localtime mounts, and warm/fast shutdown ordering
after SWSS and before Syncd. The selected source path is limited to native
AMD64/Trixie VS with FIPS enabled and ASAN disabled. Other profiles keep their
existing producers. Full VS installer integration remains a separate gate;
passing these image checks does not establish installer or hardware behavior.
