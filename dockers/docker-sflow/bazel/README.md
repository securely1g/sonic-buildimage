# Native AMD64 sFlow image

The sFlow image extends the source-built `docker-swss-layer-trixie` OCI layout.
The parent supplies SWSS's `sflowmgrd`, Common's C++ and Python interfaces,
configuration tools and their project runtime dependencies. The image consumes
these additional source owners directly:

| Owner | Runtime input | Matching symbols |
| --- | --- | --- |
| hsflowd | `@hsflowd//:runtime` | `@hsflowd//:runtime.debug_symbols` |
| sflowtool | `@sflowtool//:runtime` | `@sflowtool//:runtime.debug_symbols` |
| psample | `@libpsample//:runtime` | `@libpsample//:runtime.debug_symbols` |

The owners retain compilation, SONiC patches, native tests and package identity.
Their metadata targets describe the actual runtime TARs without producing DEBs.
The image adds locked external `dmidecode`, hiredis, libmnl and LSB dependencies.
The common APT selector checks the actual parent and retains compatible existing
libraries, including the source FIPS OpenSSL inputs. Debug tools use the existing
shared selection and source OpenSSH interfaces.

`BUILD.bazel` installs the four original Docker files and applies the Dockerfile's
crash-file argument to the owner's init script. Supervisor starts `sflowmgrd` and
`port_index_mapper.py`; the installed manager starts and stops hsflowd through
`service hsflowd`. That control path remains intact. The VPP startup script is
preserved, including its no-op behavior on VS platforms.

The runtime and debug targets are `:docker-sflow` and `:docker-sflow-dbg`.
Their `.gz` targets export the existing Docker tags and archive names for Make.
The debug image extends the exact runtime layout and adds matching source
symbols and tools. `:source_runtime`, `:source_debug_symbols`,
`:runtime_package_metadata` and `:all_debug_symbols` expose the corresponding
consumer evidence.

Prepare the existing Make manifests before a direct build:

```sh
make -f tools/bazel/prepare_manifests.mk \
  'MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk rules/docker-sflow.mk' \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs \
  CONFIGURED_ARCH=amd64 INCLUDE_FIPS=y ENABLE_ASAN=n DBG_IMAGE_MARK=dbg \
  DOCKERS_PATH=dockers
tools/bazel/ci/libteam_draft_bazel.sh build //dockers/docker-sflow:docker-sflow.gz \
  //dockers/docker-sflow:docker-sflow-dbg.gz
```

The Draft review uses `tools/bazel/ci/review_bazel.sh` with the immutable complete
catalog recorded by `libteam_draft_bazel.sh`. Ordinary `.bazelrc` keeps maintained
SONiC main and Bazel Central Registry. The compatibility entry point delegates to
the shared helper so inherited image jobs select the same catalog and preserve
their local caches. The selected iproute2 `.5` preserves the full compiler
flags through response files when the shared native dependency graph exceeds
the operating system's per-argument limit ([registry #192](https://github.com/securely1g/sonic-bazel-registry/pull/192)).
Remove the temporary catalog selection when prerequisites
land; do not add a second SONiC registry endpoint.

Make selects this producer only for native AMD64 VS/Trixie with FIPS enabled,
ASAN disabled and the normal `dbg` suffix. Other profiles preserve their existing
package/image producers. The change retains `INCLUDE_SFLOW`, capabilities,
shutdown ordering, host mounts and the `psample`/`sflowtool` host exports.
The selected Bazel image edge consumes manifests and declared source outputs;
it does not depend on the Make package cache or create DEBs.

The existing Bazel OCI workflow builds this image after the shared SWSS parent
in the AMD64 job. Build, payload checks, owner tests and no-op/source-edit checks
are separate steps. A dependent job loads those exact runtime/debug archives,
checks every source ELF and its loader closure, and exercises the original
supervisor, manager/init service, port mapper and private Redis. The UDP fixture
connects the installed hsflowd JSON input to the installed sflowtool decoder.
The VPP script's two configuration branches are checked without starting a VPP
dataplane. This scope does not claim ASIC or real kernel packet sampling,
or a completed VS installer build.

The package owners' debug outputs cover all eleven new native files, including
modules and utilities omitted from the legacy Docker debug stanza. Inherited
SWSS/Common symbols come from the parent's complete source graph; this path
does not copy the legacy stanza's unrelated `DOCKER_TEAMD` assignment.

To refresh external packages, resolve `refresh/MODULE.bazel` against its pinned
Debian snapshots, then run `refresh/export_lock.py`. Review the resulting lock
and rerun selection, loaded-image and source-edit checks. Keep generated
`MODULE.bazel.lock` as validation evidence, outside Git. Exact no-op/restoration
hashes and cross-root reproducibility are separate claims; inherited base ELF
reproducibility limitations must remain visible in the review evidence.
