# AMD64 MUX source image

This integration uses the source-built Trixie config-engine parent from
buildimage PR #133 and the Linkmgrd producer from source PR #1 / registry PR #183.
The [MUX image workflow](../../../.github/workflows/bazel-mux-image.yml) runs
the image, incremental and installed-service acceptance checks separately.

The selected profile is native AMD64 VS, Trixie, FIPS enabled, ASAN disabled,
`INCLUDE_MUX=y`, and the standard `dbg` suffix. Other profiles keep the existing
Make producer. The original install selection, config-engine dependency,
read-only mounts, and warm/fast shutdown ordering before SWSS are retained.

Linkmgrd's owning repository supplies the daemon, compressed changelog, matching
Build-ID symbols and runtime package metadata. The parent supplies Common,
Python bindings, libnl, supervisor, the Rust exit listener and rsyslog. Only the
four required Boost 1.83 runtime packages are added from pinned APT inputs;
no project DEB is used as a substitute for its source producer.

The runtime exports `//dockers/docker-mux:docker-mux.gz`; the debug variant is
`//dockers/docker-mux:docker-mux-dbg.gz`. They preserve the original entrypoint,
supervisor configuration, critical-process file and Dockerfile template copied
by the legacy `*.j2` instruction. Normal builds retain the maintained registry
and BCR; Draft CI uses one immutable review catalog through the shared wrapper.

Build, static validation, incremental checks and loaded-service validation are
separate commands in `tools/bazel/ci/mux_image.py` and `mux_loaded.py`. The build
audits selected actions before execution and rejects DEB outputs. Validation
binds receipts to the committed checkout, checks the actual loaded image config
and layer digests, preserves parent and owner file hashes/permissions, and checks
source/debug ELF pairs. No-op and restored config builds must produce identical
archive hashes; a supervisor-config edit must leave parent/source outputs alone.

The installed probe starts the original entrypoint and Linkmgrd through
supervisor, with a private Redis instance and isolated virtual MUX port. It
requires Linkmgrd's actual state publication, reads it through the installed
SONiC database CLI, and checks positive/negative debug-symbol lookup. The probe
uses neither host networking nor host services and does not simulate a physical
MUX response or claim dual-ToR hardware validation.

The APT lock refresh module under `refresh/` is AMD64-only. It resolves existing
Debian inputs and does not produce DEBs. Existing FIPS core reproducibility gaps
remain tracked separately; cache reuse is not proof of a cache-free core build.

The normal `.bazelrc` keeps the maintained SONiC registry and BCR. Until the
owner registrations land, image CI uses the shared `review_bazel.sh` helper
with the immutable catalog declared by `SONIC_BAZEL_REGISTRY_REVISION` in that
workflow as its single SONiC endpoint.
The same catalog is selected for inherited image-root Rust and archive checks;
the standalone YANG package keeps its independent maintained inputs. This
review selection preserves configured local repository and disk caches.
