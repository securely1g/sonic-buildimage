# Shared Bazel CI helpers

These helpers validate buildimage artifacts and preserve build evidence. The
shared container runner discovers the selected owner's Bazel CI targets;
package expectations remain in the owner declarations and tests.

[`.github/workflows/bazel-oci.yml`](../../../.github/workflows/bazel-oci.yml) is
the shared entrypoint for Bazel OCI container CI. It runs common AMD64/ARM64
archive checks, lightweight hosted regressions and the full VS integration
job. The active `vs_container` job builds inside a privileged SONiC slave,
then runs shared SWSS/Syncd static image checks and Docker-load validation in
that same worker. Build and validation share its Bazel server and caches.
The older `vs` job is disabled; its post-Make source-package suites and
cache-action reports remain in that legacy path and are not run by the active
job. Add checks for future
OCI containers in this workflow, keeping their target lists and package contracts with each container.
Keep checks that compile image inputs in the full build job so they can reuse
its action outputs.
The owning BUILD files sit beside the original container inputs:
[`dockers/docker-orchagent/BUILD.bazel`](../../../dockers/docker-orchagent/BUILD.bazel)
and [`platform/vs/docker-syncd-vs/BUILD.bazel`](../../../platform/vs/docker-syncd-vs/BUILD.bazel).
The shared runner accepts either package path; it does not require owners to
live under `dockers/`.
Manual runs default to `skip_vs=true`; same-repository PRs retain the full VS
job after hosted regressions and dependency resolution pass. Fork PRs and
manual `skip_vs=true` runs omit source-package/full-image validation; hosted
success alone does not establish those results.

The source-only [SWSS native closure workflow](../../../.github/workflows/bazel-swss-native.yml)
runs the existing full-package, Common-owned Rust and DASH/Protobuf tests on
native AMD64/Trixie with the libteam source interfaces selected by the buildimage
root. It retains both TARs, their checksums, every production native link command,
all 30 deployed ELF dependency lists and matching debug pairs, plus isolated
GDB source/line lookups for the C++ and Rust entry points. The targeted
edit probe changes one installed Python source through the declared SWSS patch,
requires only the runtime TAR to change, then restores both original hashes.
It does not claim cold-cache performance, C++ recompilation, complete daemon
execution or full-image validation. The Draft registry entry point is shared
with libteam until its prerequisite registrations land.
Public uploads contain explicit archives, hashes, validated receipts and the
shared helper's build/test summaries. Raw Bazel events, action environments and
test/command logs remain in the CI workspace.

For the selected SWSS revision, Make's `LIBFIB` declaration is inactive in the
source/link/ELF closure; the audit fails if later inputs introduce that usage.
FRR's libFIB header/recipe prerequisite remains independent; its actual loader
requirements are inspected separately. SAI metadata/Redis and DASH APIs
remain source-owner dependencies. `LIBSAIVS_DEV` supports the native mock-test
path; the production graph does not automatically install that development
package. `stpmgrd` uses its declared local IPC definitions rather than linking
an STP daemon. The old Protobuf variables in `rules/swss.mk` are defined only
for Bullseye; Trixie uses the selected owner's shared Protobuf ABI. The image
integration follow-up must replace stock `libteam5`/`libteamdctl0` runtime APT
payloads with the same source owners' TARs; the source check alone does not
prove that replacement. `libteam-utils` is a separate teamd image input, not an
extra SWSS runtime dependency.

The native VS Make build produces the runtime and debug archives together for
each registered Bazel container, using the same builder and Bazel invocation
for each pair. The normal installer still selects runtime containers;
`INSTALL_DEBUG_TOOLS=y` selects debug containers. Producing a debug archive does
not add it to the installer.

The active **VS installer in SONiC slave (AMD64)** job prepares the private
worker, builds `target/sonic-vs.bin` directly through `slave.mk`, and runs
`verify_vs_oci.sh` before loading the resulting images with the shared
`validate_runtime.py` validator. It checks the installer version and retains
the runtime/debug archive identities across validation. The private Docker
daemon belongs to this worker and is stopped after evidence publication.

The retained legacy wrapper [`build_vs.sh`](build_vs.sh) runs one
`make target/sonic-vs.bin` request
with the native AMD64 Trixie VS configuration, Bazel, FIPS and DASH enabled.
The **Build the VS installer (.bin)** workflow step ends
when Make finishes producing the ONIE installer and its runtime/debug archive pairs.
It does not request those archives again or run the post-build validators.
This target does not require KVM. BIOS/UEFI VM disk generation and guest boot
checks belong to `target/sonic-vs.img.gz` and are not exercised by this `.bin`
CI path. The separate VM target retains those checks.
The build records its starting Git revision; validation requires that revision
before checks and again before writing its timing summary.

[`validate_vs.sh`](validate_vs.sh) runs in the separate **Validate VS container
pairs and P4 debug symbols** step after a successful build, using the same
checkout and configuration. It first checks
both Orchagent and Syncd pairs before another builder can produce a missing
archive. The checks cover Linux/AMD64 configuration, Make manifests, Bazel
producer stamps and each debug image's runtime layers. They also check that
`target/sonic-vs.bin.dockers`, emitted from the installer's actual inputs,
selects runtime Orchagent and Syncd rather than debug archives for this normal
VS configuration. Installed-container validation remains a separate check of
the resulting container images.

For a separate `.img.gz` build, `vs_archive_reuse.py capture-pairs` accepts
`--installer sonic-vs.img.gz`; verification follows the image recorded in the
receipt. Older receipts without an image name retain their `.img.gz` meaning.

The validator prepares Bookworm through `Makefile.work` unless `NOBOOKWORM=1`,
then uses its existing `sonic-slave-run` entrypoint to run `verify_vs_oci.sh`
in Trixie. Make expands
`SONIC_BUILD_INSTRUCTION` for that command so it retains the selected build
configuration. The verifier queries Make for the resolved profile and existing
P4RT runtime/debug package names rather than hardcoding package versions.
It requires native AMD64/Trixie VS with Bazel, FIPS and DASH enabled, without
ASAN, Syncd RPC or cross/emulated execution, and checks that both runtime/debug
container pairs select their Bazel producers.

The shared verifier resolves the config-engine archive and the two existing
P4RT package prerequisites through Make, then runs the SWSS and Syncd image
checks and offline P4RT split-debug-symbol check in the same Trixie builder.
It does not request the Orchagent or Syncd Make archives again or add a package
definition. The legacy wrapper's Bookworm builder cleanup
is retained. The final pair check requires all four archives and their manifests
to remain unchanged. Missing, changed or incompatible outputs fail validation;
failures are not hidden by rebuilding an archive.

The P4 image test still declares the native Syncd OCI images and Bazel archive
outputs as inputs and compares them with the retained Make archives. Bazel may
restore those inputs or rebuild them on a cache miss in the validation builder.
The legacy build and validation steps share persistent action/download caches
with separate private output bases; the active direct-slave job also retains
its Bazel server. Splitting the workflow steps does not remove this work or
claim a speedup. The active image checks do not replace the source-package
suites retained in the disabled legacy job. Neither path's installer checks
establish BIOS/UEFI VM boot success.

Each container retains its owner's dependencies and Bazel options. Standalone
archive requests and other configurations retain their existing rules.
Validation stops on a failed prerequisite or check.

[`verify_vs_oci.sh`](verify_vs_oci.sh) runs the uncached SWSS and P4 image tests and offline
split-debug package check before the validation builder exits. It preserves
`artifacts/vs/swss-images/`, `swss-images.log`,
`artifacts/vs/syncd-p4-images/`, `syncd-p4-images.log`,
`p4rt-debug-verification.log` and `p4rt-debug-verification.json`. The archive
checker retains `runtime-debug-pairs.json` and `orchagent-reuse.json`; these
are validation receipts, not inputs needed to assemble the image.
Each family's test logs, XML and static receipt are copied before switching
Bazel configurations. The generated `*-installed-images-policy.json` files
bind the Docker checks to those outputs; `installed-images.json` records the
shared Docker-load result. The active workflow publishes these files in
`sonic-vs-image-validation-amd64`.
`runtime-debug-pairs-timing.json` (schema 2) records the revision and profile,
P4 validation time, pair validation time and their total. It does not include
the image build duration.
The successful image artifact contains `target/sonic-vs.bin` and the four
Orchagent/Syncd runtime/debug archives. The recorded installer selection is
`target/sonic-vs.bin.dockers`.
Do not parallelize top-level Make goals that share Docker state.

GitHub Actions reports build and validation as separate step durations. Compare
the complete job, including later source checks and artifact uploads, when
measuring an improvement. Moving validation out of the build step changes the
timing boundary, not the total work. The `.bin` installer step also has a distinct
dashboard measurement from historical `.img.gz` builds: omitting VM generation
and boot checks is a change in scope, not a same-scope build speedup.
Helper tests exercise dispatch, ordering, configuration and failure behavior
without Docker; full VS CI is needed to
validate the resulting image and measure end-to-end time.

- `container.py` runs an owner's `:ci_tests` test suite and builds its
  `:ci_artifacts` filegroup. Tests and exported files are declared in the
  container's `BUILD.bazel`; no Python CI configuration or callback is loaded.
  For example, for separate cold runs after preparing the matching Make
  manifests (use separate clean workspaces for these examples):

  ```sh
  python3 -B tools/bazel/ci/container.py \
    --container //dockers/docker-orchagent --source-file rules/docker-orchagent.mk --bazel bazel --artifacts artifacts/swss
  python3 -B tools/bazel/ci/container.py \
    --container //platform/vs/docker-syncd-vs --source-file platform/vs/docker-syncd-vs.mk \
    --bazel-arg=--config=syncd-dash --bazel bazel --artifacts artifacts/syncd-vs/bazel
  ```

  Start from native AMD64 Trixie without `MODULE.bazel.lock` and use an empty
  artifact directory. To validate after Make, pass `--reuse-build`: it requires
  the existing nonempty, ignored and untracked generated lock, records its
  initial hash, and retains the tested lock/graph afterward. It never deletes
  the lock, clears outputs or disables package/source checks. Use Make's exact
  feature flags (`--config=syncd-dash` for Syncd; SWSS always inherits the
  source-built config-engine image), both native platform flags,
  and the same cache/download options. In this mode, the Git cleanliness check
  covers the declared Bazel inputs and their containing submodules, allowing
  unrelated legacy Make submodules to create their normal build patches.
  Changes to validated inputs or their submodule commits still fail.
  In reuse mode the artifact group is built before any tests, using Bazel's
  execution log to record cache hits and executed actions by target and mnemonic.
  The raw log is private and deleted after extracting those counts; no command,
  environment or file-path fields are published. This distinguishes reuse of
  Make's production outputs from later test-only compilation. Cache misses are
  reported for inspection rather than hidden or treated as a test failure.
  Reuse Make's generated manifests rather
  than rerendering them with different defaults. The runner expands nested test
  suites, audits their actual targets and the artifact group before execution, runs uncached tests, collects
  configured logs/XML and package-check summaries, and exports the group's files
  under their declared basenames. Duplicate output filenames fail; an empty
  artifact group is valid for contract-only CI. Dependency resolution and source
  hashes are retained with success/failure receipts. `--bazel-arg=...` appends an
  explicit build option override. `--source-file PATH` adds a workspace-relative
  preparation input to the source evidence; the workflow supplies the same
  owning Make include that prepares the manifests.

  SWSS's actual source-layer validation is a Bazel test with declared archives,
  owner install files and binutils tools. Syncd's suite validates its existing
  Make/source package adapter. Both suites use the same manifest-test program
  and the shared OCI package/image regression tests. Adding a test or artifact
  requires changing the owning BUILD declaration, with no second list in the
  runner or public artifact test-count checks.
- `artifact_validation.py` streams tar contents to record ownership, modes, file
  hashes and little-endian ELF64 metadata, and checks matching split debug symbols.
  SWSS and syncd share its ELF inspection and debug-link checks; each consumer
  supplies its package inventory and platform policy. SWSS's inventory and
  Protobuf expectations are declared in its BUILD-generated `:package_policy`
  and interpreted by `tools/bazel/oci/package_contract.py`.
  `debug_archives` combines a runtime tar and its symbols tar, rejects payload
  collisions and foreign debug paths, checks the caller's ELF architecture,
  safely extracts the pair and verifies matching build IDs, DWARF and debug links.
- `build.py` builds a caller's filename-to-label mapping and collects one nonempty
  archive per label, with atomic publication, command logs, sizes and hashes.
  `collect_artifacts` exports all files from a declared CI filegroup. It checks
  every output before publication and records each file's size, hash and Bazel
  output path. Owner package checks run as Bazel tests with declared source inputs.
  A package test records every validated archive's basename and SHA256 in
  `validation.artifact_sha256`. The runner and public upload collector require
  the exported names and bytes to match. The source-layer test and artifact filegroup use identical build options
  so their inputs and exported bytes match.
- `command_log.py` captures command output and timings while keeping diagnostic
  messages separate from queried artifact paths. Callers supply the working
  directory and evidence directory. The shared runner sends raw action-graph
  stdout to a temporary private file, retains only stderr in the log and removes
  the raw graph after its execution audit, including on failure.
- `public_artifacts.py` prepares explicit public upload lists for the archive,
  SWSS source, VS and Syncd contract evidence. The three source/image artifact
  selections now run in the VS job; the successful VS summary also requires
  matching successful SWSS and Syncd summaries. Container jobs compare the expanded
  suite identities with successful build-event results instead of maintaining
  a second fixed test count. Successful jobs retain their expected
  build outputs and validated dependency JSON, plus summaries containing only
  declared status, count, revision and hash fields. Failed jobs may retain only a
  partial summary. The helper excludes raw command, Bazel event/execution and
  test logs/XML from uploads; these records remain in the job workspace.
  It rejects missing successful outputs, symlinked inputs, nonempty recorded
  environments, diagnostic command fields, credential-bearing URLs, common token
  formats and known host build or
  cache paths. The owning build step remains responsible for validating binary
  payloads. Update this helper's declared paths, test counts and receipt fields
  with the owning workflow when its outputs change. The helper does not run a
  build, and its direct tests use synthetic records only.
- `python_packages.py` exports the config-engine and py-common wheels and retains
  source-suite logs/XML, collected-test inventories and the installed-wheel
  receipt. It requires matching wheel hashes, native architecture and Python
  version, and fails when any required evidence is missing.
- `resolution.py` retains the generated module lock and resolved graph. Its
  explicit allowlist for known unused-extension failures is tied to the selected
  dependency versions and rejects incomplete graphs or unexpected failures.
- `verify_agent_cache.py` builds the shared OCI archive fixture across fresh
  checkouts and output bases, verifies disk-cache hits, then changes an input and
  requires a rebuild. It shares cache settings through `tools/bazel/build_helpers.py`.

Run the helper tests from the repository root:

```sh
python3 -B -m unittest discover -s tools/bazel/tests -p '*_test.py'
```

The ELF tests use `cc`, `readelf` and `objcopy`. The cache integration check uses
the repository's selected Bazel version and platform; its artifact directory
must be new:

```sh
python3 -B tools/bazel/ci/verify_agent_cache.py \
  --bazel bazel --artifacts artifacts/agent-cache
```

The workspace rc uses the [maintained registry selection](../README.md#registry-selection).
Resolution receipts retain the actual registry URLs from the generated lock
instead of inferring them from the default `.bazelrc`.

## Public build evidence

Container CI uses `public_artifacts.py` from the public upload work in
[Image12](https://github.com/securely1g/sonic-buildimage/pull/12). It uploads
explicit build outputs, checked dependency records, and summaries of build and
test results. Raw Bazel events, command lines, logs, and receipt diagnostics stay
out of public artifacts. A failed job publishes only a validated partial summary.
The selector fails closed for unsafe dependency data or missing successful outputs.
