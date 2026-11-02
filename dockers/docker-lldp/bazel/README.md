# Source-built LLDP image

`//dockers/docker-lldp:docker-lldp` and `:docker-lldp-dbg` assemble native
AMD64/Trixie images on the source-built config-engine parent. Their `.gz`
targets preserve the existing Docker-load archive and Make manifest contracts.
The Make selector applies only to VS, AMD64, Trixie, FIPS, no ASAN and the
standard `dbg` marker; other configurations keep their existing prerequisites.

LLDP comes from `@lldpd//:runtime` and its matching debug output. DBSyncd
comes from `@sonic_dbsyncd//:sonic_dbsyncd_runtime`; Common's Python binding
and sonic-py-common are inherited from config-engine. Their owning targets
provide the reusable payloads; no Debian packages are generated. External target libraries
are selected from the checked `apt.lock.json` snapshot.

The existing Debian LLDP payload places its systemd unit under `lib/`.
The declared upstream BSD-tar action maps that payload into `usr/lib/` to
preserve the parent's merged-/usr symlink. Bytes, ownership, mode, links and
matching symbols are preserved. `_lldpd` owns the existing setuid `lldpcli`
contract; the debug image reserves `_ssh` group 104 because the source
OpenSSH owner's `ssh-agent` TAR already carries that group and setgid mode.

Build and validate in native AMD64 Debian Trixie:

```sh
python3 tools/bazel/ci/lldp_image.py build --artifacts artifacts/lldp
python3 tools/bazel/ci/lldp_image.py validate --artifacts artifacts/lldp
python3 tools/bazel/ci/lldp_image.py incremental --artifacts artifacts/lldp
```

The temporary `lldp_draft_bazel.sh` selects one reviewed registry catalog for
unmerged owner registrations. `.bazelrc` retains canonical `main`; remove
the temporary routing after those registrations land. The standalone YANG
module continues to use canonical `main`. Its image-root query uses the
same temporary image catalog as the other inherited CI jobs.

The selected catalog combines the existing config-engine source path with
[LLDP #173](https://github.com/securely1g/sonic-bazel-registry/pull/173),
[DBSyncd #178](https://github.com/securely1g/sonic-bazel-registry/pull/178),
and the inherited-source reproducibility fixes
[#170](https://github.com/securely1g/sonic-bazel-registry/pull/170),
[#171](https://github.com/securely1g/sonic-bazel-registry/pull/171) and
[#172](https://github.com/securely1g/sonic-bazel-registry/pull/172).
The DBSyncd graph also requires the ordinary-module compatibility fixes
[#174](https://github.com/securely1g/sonic-bazel-registry/pull/174) and
[#177](https://github.com/securely1g/sonic-bazel-registry/pull/177).
All catalog changes remain owned by those prerequisite PRs.

Run completed-archive checks on a Docker host:

```sh
python3 tools/bazel/ci/lldp_loaded.py --artifacts artifacts/lldp
```

That check creates only private interfaces and Redis inside isolated
containers. It runs the installed LLDP daemons and DBSyncd CLI, verifies
neighbor add/update/delete in APPL_DB, executes the shipped manager's
hostname control method, and checks real source/symbol pairs in the debug
variant. The private root controller retains the capabilities needed by the
installed privilege-separation contract: network administration/raw packets,
UID/GID changes, chroot, file ownership/access and process cleanup. The
setuid CLI keeps its owner mode, so this probe permits that UID transition.
The root filesystem stays read-only, network access is disabled, and Redis
listens only on the fixture's Unix socket and loopback TCP port. This is not
a full supervisor lifecycle or hardware test. The
incremental check edits a template, requires both image/archive outputs
to change while owners and parent remain unchanged, and restores exact
original hashes. Post-build validation is separate from image production.
