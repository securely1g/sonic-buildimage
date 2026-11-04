# Source-built DASH engine image

The native AMD64/Trixie path builds `docker-dash-engine.gz` from the existing
source base and the public PI, BMv2 and nnpy runtime TARs. It installs the original
`start.sh` unchanged. That script raises `eth1` and `eth2`, then starts
`simple_switch_grpc --interface 0@eth1 --interface 1@eth2 --log-console --no-p4`.
The original Dockerfile's Supervisor startup is commented out, so this image
keeps the direct entrypoint. Its explicitly installed net-tools, Supervisor,
rsyslog and dependent-startup packages remain available.

The canonical APT lock retains the PI/BMv2 owners' declared package dependencies,
including their development/compiler packages, and the Python modules imported
by their installed clients. The native nnpy extension is built from upstream
source against the same nanomsg ABI used by BMv2. Its upstream egg metadata and
license are retained; it is not represented as a Debian package.

The historical `p4lang/behavioral-model:latest` base digest was not retained in the
PR64 evidence. This path preserves the checked-in startup and verified installed
interfaces. It does not claim byte-for-byte parity with every incidental tool in
that mutable historical image.

## Build and verification

Use native Debian Trixie/AMD64 with Bazel 8.5.1. The review catalog is immutable:

```sh
export SONIC_BAZEL_REGISTRY_REVISION=3ddd218247fac3e6cd619ba7d0fa5973ca01d46e
python3 -B tools/bazel/ci/dash_engine_image.py build --artifacts artifacts/dash-engine
python3 -B tools/bazel/ci/dash_engine_image.py validate --artifacts artifacts/dash-engine
python3 -B tools/bazel/ci/dash_engine_image.py incremental --artifacts artifacts/dash-engine
python3 -B tools/bazel/ci/dash_engine_loaded.py --artifacts artifacts/dash-engine
```

`build` first renders only Make manifests and audits the configured graph for
DEB producers. Normal Make selects the Bazel archive only for VS/AMD64/Trixie,
FIPS and DASH enabled, without ASAN, RPC or emulation. Other profiles retain the
existing Docker path. The debug archive is a separate validation export; this
change adds no debug installer family to the legacy Make configuration.

The static check binds both image stages to their exact parents, source TARs,
APT selections and installed package state. It preserves inherited account rows,
file metadata and runtime ELFs, and verifies every project ELF's matching build
ID, DWARF and debuglink (with the existing sealed SymCrypt contract). The debug
image adds the source OpenSSH client, matching symbols and full Vim.

The loaded check uses a disposable Docker network namespace and two private
interfaces. It runs the unchanged startup script, verifies stable installed
processes, P4Runtime 1.3.0 controller arbitration and the selected PI owner's
no-pipeline rejection. It also exercises the installed nanomsg event/debugger
clients, native loader closure and positive/negative GDB lookup. These are
software installation checks, not a full VS boot, forwarding or hardware test.

The incremental check changes the tracked startup script, requires both image
archives to change while source-owner outputs and the parent stay identical,
and restores exact baseline archive bytes. That verifies image invalidation and
cache restoration; it does not claim a fresh rebuild of every native producer.

CI runs these checks in the existing `bazel-oci.yml`. Public artifacts include all
18 declared payloads and selected receipts; command environments, execution logs
and local cache paths stay private. The source base's inherited ARM64 checks stay
enabled. The fixed `dash-engine` worker selector runs the same four phases with the
reviewed resource profile and source/catalog fences.

## Review dependencies

The image review base carries the shared source/config-engine infrastructure and
exact exporter fix from buildimage PR159 commit
`019e48a7de6e1429781af837801ab8136b10a613`. The exporter resolves external outputs
through the successful build's recorded execution root. Image composition uses
PI's existing owner and BMv2 PR211, nnpy PR217, Common PR201, libnl PR181,
infrastructure PR209, FIPS PR197, Distroless PR200 and iproute2 PR192 through the
complete review catalog. Prerequisite readiness and final image acceptance are
tracked separately; no merge or full VS acceptance is implied by catalog
publication.

To refresh the APT lock, use the existing rules_distroless `apt.install` roots in
`bazel/apt-resolve.MODULE.bazel` with the same snapshot sources from `apt.lock.json`.
Bazel versions using extension facts retain the standard resolved `lock_content`
in `MODULE.bazel.lock` under the generated `dash_engine_debian` repository; both
engine repositories must contain identical lock content. Commit the canonical
APT lock, not Bazel's generated module lock.
