# Reuse the native P4 packages

This path imports the existing PI, BMv2, p4c, DASH `libsai` and `libsai-dev`
packages. Bazel fetches them with upstream `http_file` rules and caches the
original DEB bytes in its repository cache. The import target contains no
package-producing action. Make remains the package producer when a new set is
needed.

## Build and reuse

For native AMD64 Trixie VS with DASH enabled, normal release builds select this
path when `BUILD_WITH_BAZEL_WHEN_AVAILABLE=y`. Existing consumers, including the
syncd OCI handoff in [PR #13](https://github.com/securely1g/sonic-buildimage/pull/13),
keep using the same files in `target/debs/trixie`.

To restore only these packages from a clean checkout:

```sh
python3 tools/bazel/p4/stage.py \
  --output-directory target/debs/trixie \
  --cache-directory /path/to/persistent/bazel-cache
```

The helper builds `//tools/bazel/p4:debs`, checks all five hashes, sizes and Debian
control fields, then copies the unchanged packages into place. Identical files
keep their modification times. The standard Make build uses the existing
`SONIC_BAZEL_CACHE_SOURCE` mount; `BAZEL_CONTAINER_CACHE_DIR` can override it.

P4 packages do not enter `SONIC_MAKE_DEBS` or `SONIC_DERIVED_DEBS` in this profile,
so neither native compilation nor `LOAD_CACHE`/`SAVE_CACHE` runs for them.
Installation dependencies remain intact. Other Make consumers retain the
package-lock identity in their own dependency keys.

Debug, profiling, ASAN, cross/QEMU, source-archive and other unsupported profiles
keep their native rules. Switching back after an import fails with a specific
per-package clean command, preserving the imported files until that command is
run. This prevents an existing release package from silently satisfying a
different build profile.

## Pins and package updates

`packages.lock.json` is the reviewed input lock. It pins download URLs, SHA256
digests, package metadata, DASH's revision and the accepted native recipe and
dependency files. The helper rejects changed recipes or Make overrides before
restoring packages. The source-file fingerprints describe the reviewed consumer
baseline; the release's provenance separately records the historical producer.

The [retained package release](https://github.com/securely1g/sonic-buildimage/releases/tag/p4-prebuilt-trixie-amd64-20261007)
contains the unchanged DEBs, provenance, source-recovery patch and license
notices. It preserves packages from the earlier local build. Its producer
checkout was not a landed master revision and used recorded local download
fixes; this is a historical binary import, not a clean source build of current
master. The selected tuple differs from PR #13's earlier reference tuple.

When sources, dependencies or the supported build configuration change:

1. Build and validate a new coherent package set with Make **outside Bazel**.
2. Publish it at new versioned artifact URLs with its provenance and notices.
3. Review and update the pins and accepted source fingerprints together.
4. Run the clean-checkout cache check and the relevant runtime consumer tests.

A warm agent restores packages from the repository cache. A cold agent fetches
the pinned release assets. If both are unavailable, the import fails; there is
no hidden fallback to native packaging or Make's package cache. A remote Bazel
action-cache server is not required.

## Validation

```sh
python3 -B -m unittest discover -s tools/bazel/p4/tests -p '*_test.py' -v
python3 -B tools/bazel/p4/verify_cache.py --artifacts /tmp/p4-cache-evidence
```

The integration verifier requires committed changes and exports fresh source
trees without outputs or generated locks. It checks a cold fetch, a separate
checkout and Bazel output base with repository downloads disabled, package
metadata and native ELF architecture, and an action graph with no package
generation. Missing cached bytes, corrupt bytes and incorrect pins must fail.
It only modifies its own temporary cache. These tests validate importing and
restoring existing packages; they do not recompile the native P4 chain.
