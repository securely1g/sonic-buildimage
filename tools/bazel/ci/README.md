# Shared Bazel CI helpers

These helpers validate buildimage artifacts and preserve build evidence. The
shared container runner discovers the selected owner's Bazel CI targets;
package expectations remain in the owner declarations and tests.

For native builders behind a trusted TLS proxy, explicitly pass a CA-only PEM
bundle containing the public roots needed by the build. For example, when the
host's combined trust bundle is already configured:

```sh
export SONIC_BUILD_SLAVE_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
make configure PLATFORM=vs
make target/sonic-vs.bin
```

Keep this setting for every Make invocation that prepares a builder. Make
validates and stages individual anchors in the native Bookworm/Trixie builder
contexts only; runtime images and the shared build-hook source tree receive no
certificates. Other native distributions do not opt in to this setting.
The bundle must contain only CA certificates, with no keys, leaf certificates,
or other material, and must be at most 8 MiB. Its SHA256 participates in the
generated builder identity. Changing the bundle rebuilds the affected builder;
unsetting the variable removes its generated trust configuration and staging on
the next preparation. With no opt-in, the normal builder trust configuration is
unchanged. Do not publish the local trust bundle or derived builder images as
public build artifacts.

The templates install the anchors with `update-ca-certificates` before later
package and Git downloads. Debian's merged system store serves apt, Git, curl and
wget without changing inherited TLS options or the packaged wget configuration.
The completed builders also select that store explicitly for Git, curl and
Python/requests/pip. Debian's package hooks populate its Java store; Bazel's
bundled JVM selects it through an optional startup rc, including when Make
mounts its cache rc or disables that mount. These final client settings follow
the expensive builder setup so earlier Docker layers can remain reusable;
they precede optional vendor configuration.

[`.github/workflows/bazel-oci.yml`](../../../.github/workflows/bazel-oci.yml) is
the shared entrypoint for Bazel OCI container CI. It runs common AMD64/ARM64
archive checks, lightweight hosted regressions and the full VS integration
job. SWSS and Syncd source-package checks run after Make in that VS job, using
its checkout, Trixie builder, platform/feature flags and persistent Bazel action
cache. The private builder output bases remain separate; cached outputs can
be restored without recompiling the same source packages. Add checks for future
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

The native VS Make build produces the runtime and debug archives together for
each registered Bazel container, using the same builder and Bazel invocation
for each pair. The normal installer still selects runtime containers;
`INSTALL_DEBUG_TOOLS=y` selects debug containers. Producing a debug archive does
not add it to the installer.

[`build_vs.sh`](build_vs.sh) runs one `make target/sonic-vs.img.gz` request
with the native AMD64 Trixie VS configuration, Bazel, FIPS and DASH enabled.
The **Build the VS image** workflow step ends
when Make finishes producing the image and its runtime/debug archive pairs.
It does not request those archives again or run the post-build validators.
The installer's existing BIOS/UEFI build and boot checks remain inside Make.
The build records its starting Git revision; validation requires that revision
before checks and again before writing its timing summary.

[`validate_vs.sh`](validate_vs.sh) runs in the separate **Validate VS container
pairs and P4 debug symbols** step after a successful build, using the same
checkout and configuration. It first checks
both Orchagent and Syncd pairs before another builder can produce a missing
archive. The checks cover Linux/AMD64 configuration, Make manifests, Bazel
producer stamps and each debug image's runtime layers. They also check that
`target/sonic-vs.img.gz.dockers`, emitted from the installer's actual inputs,
selects runtime Orchagent and Syncd rather than debug archives for this normal
VS configuration. Installed-image validation remains a separate check of the
resulting image.

The validator prepares Bookworm through `Makefile.work` unless `NOBOOKWORM=1`,
then uses its existing `sonic-slave-run` entrypoint to run `verify_vs_oci.sh`
in Trixie. Make expands
`SONIC_BUILD_INSTRUCTION` for that command so it retains the selected build
configuration. The verifier queries Make for the resolved profile and existing
P4RT runtime/debug package names rather than hardcoding package versions.
It requires native AMD64/Trixie VS with Bazel, FIPS and DASH enabled, without
ASAN, Syncd RPC or cross/emulated execution, and checks that both runtime/debug
container pairs select their Bazel producers.

The verifier builds the two existing P4RT package prerequisites serially through
`slave.mk`, then runs the Syncd P4 image/package checks and offline P4RT
split-debug-symbol check in the same Trixie builder. It does not request Make
container archives again or add a package definition. Bookworm builder cleanup
is retained. The final pair check requires all four archives and their manifests
to remain unchanged. Missing, changed or incompatible outputs fail validation;
failures are not hidden by rebuilding an archive.

The P4 image test still declares the native Syncd OCI images and Bazel archive
outputs as inputs and compares them with the retained Make archives. Bazel may
restore those inputs or rebuild them on a cache miss in the validation builder.
The build and validation steps share persistent action/download caches; their
private builder output bases are separate. Splitting the workflow steps does
not remove this work or claim a speedup. SWSS and Syncd source-package checks
continue in their existing later steps, and installed-container and firmware
boot checks retain their coverage.

Each container retains its owner's dependencies and Bazel options. Standalone
archive requests and other configurations retain their existing rules.
Validation stops on a failed prerequisite or check.

[`verify_vs_oci.sh`](verify_vs_oci.sh) runs the uncached P4 image test and offline
split-debug package check before the validation builder exits. It preserves
`artifacts/vs/syncd-p4-images/`, `syncd-p4-images.log`,
`p4rt-debug-verification.log` and `p4rt-debug-verification.json`. The archive
checker retains `runtime-debug-pairs.json` and `orchagent-reuse.json`; these
are validation receipts, not inputs needed to assemble the image.
`runtime-debug-pairs-timing.json` (schema 2) records the revision and profile,
P4 validation time, pair validation time and their total. It does not include
the image build duration.
Do not parallelize top-level Make goals that share Docker state.

GitHub Actions reports build and validation as separate step durations. Compare
the complete job, including later source checks and artifact uploads, when
measuring an improvement. Moving validation out of the build step changes the
timing boundary, not the total work. Helper tests exercise dispatch, ordering,
configuration and failure behavior without Docker; full VS CI is needed to
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
  feature flags (currently `--//dockers/docker-orchagent:source_config_engine=true`
  for FIPS SWSS, `--config=syncd-dash` for Syncd), both native platform flags,
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
