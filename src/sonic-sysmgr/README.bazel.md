# Sysmgr source packages with Bazel

This module builds `rebootbackend` and `librebootgnoi` from the same sources and
gNOI gitlink as the existing Automake build. Its runtime and matching debug TARs,
plus package metadata derived from the existing Debian recipe, are the inputs
for a separate `docker-sysmgr` integration. The selected native
configuration is AMD64 on Debian Trixie, without sanitizers. Existing Make,
Debian packaging and sanitizer recipes remain available.

## Prepare the build inputs

Initialize the owner's protobuf inputs from the repository root. Use the
maintained registry's generic GitHub reader to prepare the locked Cosmos 4.0.2
tools before running Bazel. It verifies every selected file's size and hash,
reuses its download cache, and supplies Bazel's standard `--distdir`. This keeps
the selected tool versions and bytes while avoiding a cold `cosmo.zip` download.

```bash
set -euo pipefail
git submodule update --init -- src/sonic-sysmgr/gnoi
inputs=$(mktemp -d "${TMPDIR:-/tmp}/sysmgr-github-inputs.XXXXXX")
transport="https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/0f459395f8852b5c89e9895afeeea01b95b281ea/ci"
curl --disable --fail --silent --show-error --proto '=https' --connect-timeout 20 --max-time 60 \
  --output "$inputs/github_sources.py" "$transport/github_sources.py"
curl --disable --fail --silent --show-error --proto '=https' --connect-timeout 20 --max-time 60 \
  --output "$inputs/cosmos-4.0.2.json" "$transport/github_sources/cosmos-4.0.2.json"
printf '%s  %s\n' \
  9c1ad2a9548608d06e49de0c9c119ae19d97ab95cca2505fc1e7e233e62e9b3d "$inputs/github_sources.py" \
  1579ad34288945a28a2d77f96f4764b8910dd3d856d688971e46e63e58119af5 "$inputs/cosmos-4.0.2.json" \
  | sha256sum --check --strict
python3 -B "$inputs/github_sources.py" \
  --manifest "$inputs/cosmos-4.0.2.json" \
  --distdir "$inputs/distdir" \
  --cache-dir "$HOME/.cache/sonic-github-sources" \
  --receipt "$inputs/github-sources.json"
printf 'common --distdir=%s\n' "$inputs/distdir" > "$inputs/downloads.bazelrc"
```

Stop if preparation fails. The manifest includes both architecture aliases as
downloaded inputs; this module's validation remains AMD64. The first preparation
may download the upstream GitHub release ZIP (about 403 MB); later preparations
reuse verified files or the cached ZIP. `--distdir` supplies those inputs; it is
not a general network restriction.

## Build the source packages

The module's `.bazelrc` selects the maintained SONiC registry and Bazel Central
Registry. Add the prepared input directory through the same explicit startup RC
used by CI:

```sh
cd src/sonic-sysmgr
bazel --bazelrc="$inputs/downloads.bazelrc" build -c opt \
  //:sysmgr_pkg //:sysmgr_debug_pkg //:runtime_packages.json
```

Bazel generates the ignored `MODULE.bazel.lock`; CI retains it as evidence.

| Public target | Installed contract |
| --- | --- |
| `//:sysmgr_pkg` | `/usr/bin/rebootbackend`, `/usr/lib/x86_64-linux-gnu/librebootgnoi.so.0.0.0`, and its SONAME/linker symlinks |
| `//:sysmgr_debug_pkg` | Matching build-ID files under `/usr/lib/debug/.build-id` |
| `//:runtime_packages.json` | `sysmgr` version, architecture, generated dependencies and the exact runtime TAR hash |

No DEB target or Make package cache participates in these source builds. Common
and Protobuf libraries come from their owners' public Bazel targets. The D-Bus
generator and its private libraries come from the shared `build_tools` package
set. The generated header uses a stable filename so temporary sandbox paths do
not change its include guard.

Package metadata uses the original `debian/control`, `debian/changelog` and
`debian/compat`. Shared-library dependencies come from the actual Sysmgr ELF
files, Common/Protobuf owner metadata and locked target D-Bus libraries. The
Protobuf runtime uses Debian Trixie's patched source and identifies itself as
`libprotobuf32t64`. This generates metadata for OCI composition, without a DEB.

## Validate the installed outputs

The `Bazel Sysmgr source packages` workflow uses native Trixie, a declared compiler
and strict Linux sandboxing. Its test container permits nested user/mount/PID
namespaces. The private Redis server and mock D-Bus service live inside those
namespaces and use a private `/run` directory.

After preparing the inputs above, run the same driver from the repository root
in that environment. Its existing startup argument reaches the initial queries,
the action audit, and all build/test commands, while retaining the home cache RC:

```sh
python3 -B src/sonic-sysmgr/bazel/ci.py \
  --startup-arg="--bazelrc=$inputs/downloads.bazelrc" \
  --artifacts artifacts/sysmgr
```

The driver audits every selected action for DEB production, compares production
C++ sources with Automake, checks the pinned gNOI checkout, and runs three tests:

- `//tests:original_unit_tests` runs all 52 original GTests with private Redis
  and the existing mock interface. Its real GTest XML is retained.
- `//rebootbackend:gnoi_runtime_test` checks the generated messages' JSON and
  protobuf serialization against the selected source-built runtime.
- `//tests:installed_sysmgr_test` checks the extracted TAR's paths, ownership,
  modes, symlinks, ELF machine/SONAME, library closure, build IDs and debug-link
  CRCs. Offline GDB must find source lines with the matching symbols and fail
  that lookup without them. The installed daemon then handles real notification
  messages through private Redis and a mock service generated from `reboot.xml`.
  It also verifies recipe and payload hashes, package identity and generated
  Common, Protobuf and D-Bus dependencies.

CI retains both TARs, package metadata, hashes, the generated dependency lock, action audit, source
provenance, original XML and private-service logs. This source-only PR does not
schedule the full VS installer job, whose DEB-producing scope is separate.

## Maintain the source definitions

Keep production C++ files and headers in `rebootbackend/BUILD.bazel` alongside
their source. The shared `reboot_core` target lets the executable and original
tests use the same logic. Add new original test cases to the existing C++ suite
and update the explicit expected count in its runner and CI evidence check.
Update the three vendored protobuf inputs in `vendored_protos.bzl` when the
Automake contract changes. Normal builds use these maintained definitions
directly; the source inventory comparison runs in validation.
