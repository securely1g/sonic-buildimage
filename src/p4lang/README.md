# Recover the native P4 sources

The normal Make recipes download PI, BMv2 and p4c from the Open Build Service
(OBS). When those downloads are unavailable, `P4LANG_SOURCE_DIR` can select a
retained, checksum-verified source set. Compilation, SONiC source patches,
Debian packaging, output names and cross-build options use the existing recipes.
An empty `P4LANG_SOURCE_DIR` keeps the OBS download path.

## Supported source set

| Component | Upstream version | Debian version |
| --- | --- | --- |
| PI | 0.1.3 | 0.1.3-2 |
| BMv2 | 1.15.0 | 1.15.0-9 |
| p4c | 1.2.4.2 | 1.2.4.2-3 |

For each component, the input directory must contain its original `.dsc`,
`<package>_<upstream-version>.orig.tar.gz`, and
`<package>_<debian-version>.debian.tar.xz`. For example, PI requires
`p4lang-pi_0.1.3-2.dsc`, `p4lang-pi_0.1.3.orig.tar.gz`, and
`p4lang-pi_0.1.3-2.debian.tar.xz`.

The Makefile pins all nine SHA-256 checksums. Missing files, different bytes and
unsupported local-source versions fail before extraction or package compilation.
There is no automatic fallback from a rejected local source set to a download.
Use a separate input directory: the recipe copies files into its working
directory and adapts only that copy.

### The p4c archive is an explicit variant

The retained regional p4c archive has SHA-256
`a41d616add63924a098edaab446809f543ffadfc2e40356f9b73986f324c668a`
and contains 173,758,549 bytes. It differs from the archive described by the
original `.dsc`; equivalence between those archives has not been established.

The local path first verifies the original descriptor and both input archives.
It then applies `p4lang-p4c-regional-source.dsc.patch` to update the descriptor's
checksums and size, and runs `dpkg-source --require-strong-checksums`.
Already adapted descriptors and other same-version archive variants are rejected.
The normal OBS path does not apply this descriptor patch.

These inputs and the original recovery patch are recorded in the
[retained package release](https://github.com/securely1g/sonic-buildimage/releases/tag/p4-prebuilt-trixie-amd64-20261007).
That release publishes package binaries and provenance, not the source archives.
Obtain the source files separately and match the exact recorded hashes.

## Verify and extract without building packages

Run this inside the native build environment, with the source directory mounted
there. Use a clean worktree; these targets replace the corresponding extracted
source directories.

```sh
export P4LANG_SOURCE_DIR=/absolute/path/to/retained-p4-sources
make -C src/p4lang \
    prepare-pi-source prepare-bmv2-source prepare-p4c-source \
    P4LANG_PI_VERSION=0.1.3 P4LANG_PI_VERSION_FULL=0.1.3-2 \
    P4LANG_BMV2_VERSION=1.15.0 P4LANG_BMV2_VERSION_FULL=1.15.0-9 \
    P4LANG_P4C_VERSION=1.2.4.2 P4LANG_P4C_VERSION_FULL=1.2.4.2-3
```

These source-only targets share the package recipes' extraction code. They do
not apply the later SONiC source patches, compile code or produce DEBs.
For a normal native package build, export `P4LANG_SOURCE_DIR` in the build
container before invoking the existing package targets. Host environment
variables and paths are not automatically forwarded into Docker.

Make's package-cache flags include the selected source directory. Its tracked
source inputs include the Makefile's pinned checksums and the descriptor patch.
Changing those inputs therefore changes the native package-cache key.

## Validation and downstream use

Run the isolated Make regression tests with:

```sh
python3 -m unittest discover -s src/p4lang/tests -p 'test_*.py' -v
```

CI runs these tests on native AMD64 and ARM64 hosts. They exercise real Make and
checksum rejection with intercepted download, extraction and compilation tools;
they do not establish full package builds on either architecture. Real retained
archive extraction is recorded separately in the PR validation.

[Buildimage PR #14](https://github.com/securely1g/sonic-buildimage/pull/14) imports
historical packages produced using this recovery source set. Landing these
recipes does not retroactively change those packages' producer revision. A new
build from landed sources and refreshed artifact provenance are still needed
before claiming that the imported packages were produced from merged code.
