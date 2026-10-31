# Native P4 source cache

The native recipes still build PI 0.1.3-2, BMv2 1.15.0-9 and P4C 1.2.4.2-3,
apply the existing SONiC patches, and produce the same Debian package targets.
An empty `P4LANG_SOURCE_DIR` uses the normal OBS downloader. Setting it selects
the exact source inputs in [source-cache.json](source-cache.json).

Stage all nine files in a separate directory: each package needs its original
`.dsc`, `.orig.tar.gz`, and `.debian.tar.xz`. Obtain the files from the declared
OpenSUSE source repository or a retained source cache with the recorded
provenance. The historical GitHub release publishes provenance, not these source
archives. Previously built `src/p4lang` directories can contain repacked archives
and modified descriptors; use retained input files and verify their hashes.

Verify before copying staged inputs into a persistent cache:

```sh
python3 -B src/p4lang/source_cache.py verify --source-dir /path/to/staged-inputs
```

This read-only command checks all nine regular files, sizes and SHA256 hashes,
and emits a JSON receipt. It rejects missing files, symlinks and mismatched
bytes. Copy only the nine verified files named by the manifest into the source
cache, then run the same verifier against the destination. The native runner
job uses `/dpkg_cache/source-archives/p4lang`, backed by the host's package-cache
directory. Cache priming is an operator step; the job does not silently download
replacement files when verification fails.

For ordinary native builds, export `P4LANG_SOURCE_DIR` to the directory visible
inside the build container before invoking `slave.mk`. The package recipe
verifies its selected inputs before copying them and verifies the working copies
again before extraction. The source cache stays unchanged. The source selection,
manifest, helper and descriptor patch participate in package-cache inputs.

The P4C descriptor and archive were already inconsistent in SONiC's
`files/build/versions/default/versions-web`: the descriptor names an archive
with SHA256 `312e94e556d702823e70a3ac0999e17f1de5f1cdf6b444dab2058f562c8313df`,
while the archive pin selects MD5 `98b03592b7a6cf4ed2150463fd757e0e`.
The selected archive's SHA256 is
`a41d616add63924a098edaab446809f543ffadfc2e40356f9b73986f324c668a`, matching
the [official OpenSUSE metadata](https://download.opensuse.org/download/repositories/home%3A/p4lang/xUbuntu_21.10/p4lang-p4c_1.2.4.2.orig.tar.gz.mirrorlist).
After verifying the original descriptor and both archives, the local path
repairs only the orig archive's SHA1, SHA256 and MD5/size rows in a working copy.
All other descriptor fields and the Debian archive checksums remain unchanged.
Extraction requires strong checksums. No equivalence to the missing `312e94…`
archive is claimed.

To exercise source preparation without package compilation, run in a disposable
checkout inside the native build environment:

```sh
make -C src/p4lang prepare-pi-source prepare-bmv2-source prepare-p4c-source \
  P4LANG_SOURCE_DIR=/path/to/verified-inputs \
  P4LANG_PI_VERSION=0.1.3 P4LANG_PI_VERSION_FULL=0.1.3-2 \
  P4LANG_BMV2_VERSION=1.15.0 P4LANG_BMV2_VERSION_FULL=1.15.0-9 \
  P4LANG_P4C_VERSION=1.2.4.2 P4LANG_P4C_VERSION_FULL=1.2.4.2-3
```

These targets replace their extracted source directories. They use the package
recipes' preparation path but do not apply the subsequent SONiC patches or build
DEBs. Focused tests run with `python3 -B -m unittest discover -s tools/ci/tests
-p p4lang_source_cache_test.py`. Full native package and installer validation
remains separate from source verification and extraction.
