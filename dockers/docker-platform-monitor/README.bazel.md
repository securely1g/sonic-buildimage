# PMON Bazel images

The native AMD64 Trixie VS/FIPS profile builds PMON from the existing
config-engine base, source-built lm-sensors and Platform Daemons, the
platform-common wheel, and the locked PMON Python and Debian runtime sets.

Prepare the parent image manifests using the existing Make metadata entry
point (this command builds no containers):

```sh
make -f tools/bazel/prepare_manifests.mk \
  'MANIFEST_METADATA=rules/docker-base-trixie.mk rules/docker-config-engine-trixie.mk rules/docker-platform-monitor.mk' \
  BUILD_WITH_BAZEL_WHEN_AVAILABLE=y BLDENV=trixie CONFIGURED_PLATFORM=vs \
  CONFIGURED_ARCH=amd64 INCLUDE_FIPS=y DBG_IMAGE_MARK=dbg DOCKERS_PATH=dockers
bazel build //dockers/docker-platform-monitor:docker-platform-monitor.gz \
  //dockers/docker-platform-monitor:docker-platform-monitor-dbg.gz
bazel test //dockers/docker-platform-monitor/bazel:smartcmd_compatibility_test
```

The archives keep the installer tags `docker-platform-monitor:latest` and
`docker-platform-monitor-dbg:latest`. The debug image extends the exact runtime
image with its source symbols, debug tools and the source-built OpenSSH client.
The original SmartCmd vendor executable is static i386. The selector accepts
only its declared path and SHA-256 on AMD64, and checks that it has neither an
interpreter nor a shared-library dependency. Other ELF32 payloads retain the
existing rejection.

The `rules/docker-platform-monitor.mk` bridge selects these archives with
`BUILD_WITH_BAZEL_WHEN_AVAILABLE=y` for native AMD64 Trixie VS, `INCLUDE_FIPS=y`,
`DBG_IMAGE_MARK=dbg`, and ASAN/PDDF disabled. Existing Make installation,
container mounts and service selection remain in the original rules.

The Python closure checks producer metadata and the tracked requirements,
lock and resolver-input record. To update that record using the declared uv
version:

```sh
bazel run //dockers/docker-platform-monitor/bazel/python:update -- \
  --uv "$(command -v uv)"
```

Platform Daemons is supplied by
[its source PR](https://github.com/securely1g/sonic-platform-daemons/pull/1).
The module is registered by [registry PR #436](https://github.com/securely1g/sonic-bazel-registry/pull/436).
Until that entry lands, add these options to the Bazel commands above:

```text
--registry=https://raw.githubusercontent.com/songqing-sq/sonic-bazel-registry/200fafec114b6028f9f6cd6d6a399f4328e7f238
--registry=https://bcr.bazel.build
```

These options select the reviewed dependency recipe and keep the existing
cache configuration. No local source paths belong in the published module.
