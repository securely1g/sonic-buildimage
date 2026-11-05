# Source-built SNMP image (AMD64)

This adds the native AMD64 Trixie/FIPS VS profile to the shared Bazel image
selector. It consumes the existing source-built config-engine image directly,
`sonic-platform-common`'s wheel, and `sonic-snmpagent`'s wheel and install TAR.
It does not build Debian packages or import Make-produced project artifacts.

| Input | Existing Make path | Bazel path |
| --- | --- | --- |
| Platform Python APIs | `SONIC_PLATFORM_COMMON_PY3` | `@sonic_platform_common//:sonic_platform_common_wheel` |
| AgentX agent | `ASYNCSNMP_PY3` | `@sonic_snmpagent//:sonic_snmpagent_wheel` |
| `sysDescr_pass.py` | `python3 -m sonic_ax_impl install` | `@sonic_snmpagent//:snmp_install` |
| Common, py-common and config-engine | Inherited config-engine container | Existing public config-engine OCI target and its source dependency graph |
| External SNMP tools | Floating Debian repositories | Reviewed `apt.lock.json` runtime set |
| Third-party Python modules | Floating `pip install` | Owner-locked psutil/python-arptable, inherited YAML/natsort, pinned Debian hiredis/smbus |
| Debug OpenSSH client | `FIPS_OPENSSH_CLIENT` | Existing `@sonic_fips//openssh:openssh_client_pkg`, with its matching symbols |
| Final output | Docker archive | Reproducible `docker-snmp.gz` and `docker-snmp-dbg.gz`, exported through the shared archive rule |

The wheel producers are in their owning repositories. Platform-common remains a
separate runtime dependency of this Docker; the current SNMP source does not
import it or need it to build its wheel. The complete image still provides both.
No source is copied into this repository to rebuild either wheel.

The public image targets are `//dockers/docker-snmp:docker-snmp` and
`:docker-snmp-dbg`. Their `.gz` targets supply the existing installer archive
contract. `:all_debug_symbols` contains matching symbols for the inherited
project ELF files and the debug-only FIPS OpenSSH client. The two Python owners
produce pure Python wheels and have no ELF debug package.

## Build and validate

```sh
make -f tools/bazel/prepare_manifests.mk \
  'MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-snmp.mk' \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs \
  CONFIGURED_ARCH=amd64 INCLUDE_FIPS=y ENABLE_ASAN=n DBG_IMAGE_MARK=dbg DOCKERS_PATH=dockers
bazel build //dockers/docker-snmp:docker-snmp.gz //dockers/docker-snmp:docker-snmp-dbg.gz
```

Make supplies `SONIC_IMAGE_VERSION` through the declared `:image_version` build
setting. Direct callers can set `--//dockers/docker-snmp:image_version=VERSION`.
A changed version invalidates the OCI configuration, without rebuilding wheels.
Make-generated labels and normal/debug tags remain intact. The legacy Dockerfile
places `EXPOSE 161/udp 162/udp` only in its builder stage; its final `FROM $BASE`
inherits the base metadata. This migration preserves that actual final-stage
behavior rather than adding new published ports.

The new `Bazel SNMP image` workflow separates building from validation. It checks
installed wheel bytes, startup scripts and template inputs, AMD64 ELF linkage,
account and directory ownership, imported and source package metadata, matching
source debug symbols, preserved runtime ELF bytes in debug, and template
edit/restore invalidation with identical restored image digests. A later job
loads those exact archives and runs real SNMP GET/GETNEXT against the installed
SysName MIB through Common, private Redis, AgentX and snmpd. It also renders the
normal/chassis supervisor templates and service configuration.

The Redis fixture is a separate test-only TAR. It is mounted by CI and is never
an image layer. Both installed-image probes run as the declared unprivileged
SNMP account with no network access outside the container and no source checkout
mounted into the Python import path. Hardware-specific and multi-namespace MIB
behavior is not established by this fixture.

The legacy Make inputs, default selector, unsupported profiles and full VS
installer are unchanged. This PR's full-VS job is explicitly skipped because
its legacy producer scope includes DEBs; this source/TAR/OCI CI does not claim a
full installer pass. Existing hosted checks continue, except the three existing
kernel tests that create sample DEBs for their own packaging assertions.

## Imported package installation behavior

The reviewed snmpd post-install script creates `Debian-snmp`, uses `/bin/false`,
sets `/var/lib/snmp` ownership and changes `/etc/snmp/snmpd.conf`'s group.
The image declares these effects without running maintainer scripts. Stable
UID101/GID102 are checked against the inherited account files; an ID collision
fails assembly. The default config retains mode0640 and gets GID102. The SONiC
startup script still renders the final service config at runtime.

Runtime package records come from the actual selected controls. Debug adds its
own selected controls and the FIPS owner's source-package identity. Wheels keep
their real distribution metadata rather than invented Debian records. The debug
client retains the owner's `_ssh` GID104 and setgid mode; Vim alternatives use
the existing shared implementation. Services are managed by SONiC supervisor,
so the build does not enable or start Debian init/systemd services.

Platform-common `1.0.0` is published, and both source owners are merged. SNMP
uses `2.1.0-a27e093fac394f7871ec1b19bb12ca6739c29fa9` from registry
[#164](https://github.com/securely1g/sonic-bazel-registry/pull/164). That entry is
still unmerged, so this image PR remains Draft. The obsolete source archive
overrides have been removed.

Committed `.bazelrc` keeps the canonical registry `main` URL. While #164 is open,
this PR's CI explicitly selects its reviewed catalog
`74aedb27ffcaf831f3db7d6e62a175f5cdf867d5` for every image-root invocation. The
existing `review_bazel.sh` utility replaces the default registry endpoint for
that invocation, retains local download/action caches, and leaves tracked
configuration untouched. To reproduce this Draft validation locally:

```sh
git submodule update --init -- src/sonic-sysmgr/gnoi
export SONIC_BAZEL_REGISTRY_REVISION=74aedb27ffcaf831f3db7d6e62a175f5cdf867d5
python3 -B tools/bazel/ci/snmp_image.py build \
  --bazel "$PWD/tools/bazel/ci/review_bazel.sh" --artifacts artifacts/snmp
python3 -B tools/bazel/ci/snmp_image.py validate --artifacts artifacts/snmp
python3 -B tools/bazel/ci/snmp_image.py incremental \
  --bazel "$PWD/tools/bazel/ci/review_bazel.sh" --artifacts artifacts/snmp
```

Before this PR becomes Ready, remove the temporary CI selection after #164
lands, remove the retained branch-specific CI exclusions, and validate normal
required CI through canonical `main`. The current no-DEB scope remains in force;
this adoption does not authorize the excluded DEB-producing checks. Keep the
generated, ignored `MODULE.bazel.lock` and resolved source/registry evidence with
the CI artifacts. The commands above require the same native AMD64 Trixie
execution environment and verified compression source as the workflow.

## Refresh external inputs

`refresh/MODULE.bazel` declares the external runtime/debug roots and the private
test Redis process against the same reviewed Debian snapshots as the base.
In that directory, run `bazel build //:resolve`, then `python3 export_lock.py`.
Review and commit the canonical `apt.lock.json`; never commit generated
`MODULE.bazel.lock`. Normal image builds import the checked lock directly.
