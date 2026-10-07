# Kernel bundle handoff

The kernel bundle producer connects the existing source-owned
`@sonic_linux_kernel//:kernel_packages` target to the current Make VS build.
It supports native Linux AMD64 execution, Debian Trixie, AMD64 VS, unsigned
kernel `6.12.41-1`, and ABI `6.12.41+deb13-sonic-amd64`. External platform
patches, cross builds, emulation, signing, and separate debug-package delivery
remain outside this interface.

The producer uses this separate external-consumer workspace, matching
`sonic-linux-kernel/tools/bazel/cache-consumer`. It binds an explicit local
`sonic-linux-kernel` module override to the buildimage kernel gitlink. The
kernel checkout and relevant handoff inputs must match their recorded Git
commits. The producer creates a new private state directory for the consumer
workspace, Bazel outputs, and repository downloads. Disk caching is disabled;
a supplied remote cache is read-only unless `--upload-local-results` is set.
The source launcher limits the worker to four CPUs, 12 GiB memory, one Bazel
job, and four Kbuild jobs.

## Prepare and create a bundle

Run from a clean buildimage checkout with `src/sonic-linux-kernel` initialized
at its gitlink. `plan` checks source inputs and prints the exact invocation; it
creates no state and does not invoke Bazel. `build` uses the same arguments and
runs Bazel package creation.

```sh
python3 -B tools/bazel/ci/kernel.py plan \
  --state-dir /path/to/new/kernel-state \
  --draft-registry \
  --remote-cache http://127.0.0.1:8080

python3 -B tools/bazel/ci/kernel.py build \
  --state-dir /path/to/new/kernel-state \
  --draft-registry \
  --remote-cache http://127.0.0.1:8080
```

Use a new state path for each invocation. The producer writes the verified
bundle to `<state-dir>/bundle`. The separate `stage` command copies it to the
Make input directory in a checkout at the same source commit. Optional
`--ca-bundle` and `--java-trust-store` inputs supply public trust material for
fetching; the plan records their hashes.

For a source/cache experiment, the producer can add `--upload-local-results`
when writing to its dedicated empty remote cache. A second invocation uses a
new state directory and the default read-only mode. A cache miss can run the
source action and create DEBs. The source repository's `verify_cache.py`
checks that the producer compiled, the consumer received a remote cache hit,
and all five Bazel outputs have identical hashes.

## Bundle format

The Make input directory contains exactly six files:

| File | Purpose |
| --- | --- |
| `linux-headers-6.12.41+deb13-common-sonic_6.12.41-1_all.deb` | Common kernel headers; primary Make copy target |
| `linux-headers-6.12.41+deb13-sonic-amd64_6.12.41-1_amd64.deb` | AMD64 kernel headers |
| `linux-image-6.12.41+deb13-sonic-amd64-unsigned_6.12.41-1_amd64.deb` | Unsigned kernel and modules |
| `linux-kbuild-6.12.41+deb13_6.12.41-1_amd64.deb` | Kernel build tools |
| `kernel-packages.json` | Source-owned schema 1 manifest with source inventory, archive and tool identities, package metadata, sizes, and hashes |
| `kernel-provenance.json` | Handoff schema 2 record binding the buildimage commit, kernel gitlink, module and launcher versions, target, manifest hash, and inspected resolution evidence |

The verifier checks the exact file set and package control fields, hashes,
source inventory and archive pins, the tool identity's input hashes, and the
current source provenance. The producer also checks the launcher's invocation,
the complete resolved graph, and the locked `sonic-build-infra` source
metadata. The generated module graph, lock, source metadata, build logs and
execution records remain in the private state directory. The bundle is an
integrity and provenance handoff from a trusted producer; it is not signed.

To verify or stage existing packages without invoking Bazel:

```sh
python3 -B tools/bazel/ci/kernel.py verify --bundle /path/to/kernel-state/bundle
python3 -B tools/bazel/ci/kernel.py stage --bundle /path/to/kernel-state/bundle
python3 -B tools/bazel/ci/kernel.py verify --bundle target/bazel-kernel-inputs
```

`stage` copies regular files into `target/bazel-kernel-inputs`, verifies the
copy, and refuses an existing destination. Bundles are tied to the exact
buildimage commit and kernel gitlink; a later commit requires a new producer
invocation, which may reuse the unchanged kernel action from cache.

## Make and installer interface

Select the bundle explicitly:

```sh
make target/sonic-vs.bin BUILD_WITH_BAZEL_WHEN_AVAILABLE=y \
  SONIC_BAZEL_KERNEL_PACKAGES=target/bazel-kernel-inputs
```

The kernel selector is independent of the container selector.
`Makefile.work` forwards it to the Trixie slave phase. The kernel rule verifies
the bundle each time the mode is selected, registers the common-headers
package in `SONIC_COPY_DEBS`, and lets the existing copy recipe import the
three derived packages into `target/debs/trixie`. The native package cache is
bypassed for this package set. Direct unsupported selections fail during Make
parsing. The full Make target follows the current VS dependency graph,
including other package and container builds.

Installer assembly should depend on the normal Make package targets and read
this verified directory when it needs kernel provenance. It does not need the
producer's Bazel output paths or state directory. Its filesystem, initramfs,
boot, module-load, and forwarding checks remain separate from bundle checks.

## Current dependency state

The template uses the source module's base version with a local override; it
does not claim that the kernel gitlink has a published registry entry. The
selected tools version,
`0.0.15-3a3d42932877e303385fd0b6503d1c413fc86bab`, is currently an unlanded
candidate in [registry #39](https://github.com/securely1g/sonic-bazel-registry/pull/39).
The committed registry default is `main`, which cannot resolve that candidate.
`--draft-registry` replaces the single SONiC endpoint with
`codex/kernel-build-tools-current` for explicit Draft validation. It leaves
Bazel Central Registry as the separate third-party registry.

Before review readiness, refresh the tools source and registration to exact
landed commits, register the landed kernel source on `main`, and replace the
local source mode with a verified registered consumer. Revalidate source and
fresh-cache behavior after the final pins. The coarse source action rebuilds
the whole kernel after an input change, and the Debian recipe's temporary
signing keys still limit independent cold byte reproducibility.
