# Shared Bazel CI helpers

These helpers validate buildimage artifacts and preserve build evidence. The
shared container runner discovers the selected owner's Bazel CI targets;
package expectations remain in the owner declarations and tests.

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

The native VS job reuses `target/docker-orchagent.gz` produced as an installer
dependency. `build_vs_with_reused_orchagent.sh` captures that archive after the
successful VS build, requests the remaining artifacts, and verifies the retained
runtime archive against its Make metadata and the matching debug image. Missing,
changed or incompatible archives fail the job; the script does not fall back to
an unnoticed rebuild. Standalone Make archive targets keep their normal input
and builder-switch checks.

To measure the avoided request on the same revision, runner, configuration and
warmed caches, dispatch `Bazel OCI` with `skip_vs=false` and
`measure_orchagent_reuse=true`. This calibration deliberately executes the old
request once and requires the runtime archive to remain identical. The local
`artifacts/vs/orchagent-reuse-timing.json` reports its measured wall time, the new
validation cost and their difference. This measures the removed request, not a
paired full-job speedup. Normal PR runs omit the request. Existing source,
installed-image, debug-symbol and firmware boot checks remain required.
## Supplementary validation of an exact PR pair

Manual Bazel OCI runs can validate a same-repository PR head together with its
current selected base without changing the PR branch. Supply all five pair
inputs: `pr_number`, `expected_head`, `expected_base_ref`, `expected_base`, and
`expected_merge`. Leaving every pair input empty keeps the ordinary manual
checkout. Partial inputs fail. The `skip_vs` option retains its existing meaning.

First run the local planner, using the authorized GitHub CLI when one is required
by the caller. For example, PR33 was observed on October 29, 2026 with head
`c0abb978e81b204611c81a1c0de40904347f2829` and selected
`master@2dc503aaecfb0d3c317608aaeabc68a108e32b97`:

`GH` below names that authorized executable. Omit `--github-cli` to use the
built-in public API client.

```sh
python3 -B tools/bazel/ci/checkout_pair.py plan \
  --temporary-directory /tmp --output /tmp/pr33-pair-plan.json \
  --github-cli "$GH" --pr-number 33 \
  --expected-head c0abb978e81b204611c81a1c0de40904347f2829 \
  --expected-base-ref master \
  --expected-base 2dc503aaecfb0d3c317608aaeabc68a108e32b97
```

Set the dispatch's `expected_merge` to `result.merge_sha` from that plan and
set `skip_vs=false` for the full VS job. Select the validator checkout with
`bazel-oci.yml --ref <branch-or-tag>`; `expected_merge` is an input, not that
workflow ref. A published companion branch can validate its own pair
supplementarily before integration. Ready consumers use `--ref master` after
the validator lands on the maintained branch. The planner fetches the exact named
refs into a temporary bare repository and creates a deterministic local merge;
it does not check out source, push a branch, or run a build. PR33's hosted
`refs/pull/33/merge` still used base `9620e16baf9dbb3a67a93120d3619858acdcdb70`
at that observation, so the helper does not use hosted merge refs as input.

Each executing job verifies that the PR is open, both repositories are
`securely1g/sonic-buildimage`, the named head and base refs still match, and its
locally constructed merge equals the planned SHA. A conflict or moved input
fails before build execution. Fixed commit metadata makes the merge SHA stable;
the explicit planned SHA also rejects a different tree from another Git version.
Each pair dispatch has a unique concurrency group and disables cancellation, so
another dispatch cannot replace an active or queued run. The caller must check
for an active duplicate before submission. Ordinary PR and push concurrency
remains the same.

The helper extracts itself and `public_artifacts.py` from the verified validator
checkout revision (`GITHUB_SHA`) into a private temporary directory before
switching the source checkout.
Only checkout and receipt validation use that copy. Build helpers and sources
come from the requested merge. The receipt records both revisions, the source
parents and tree, and the validator file hashes. VS refreshes and records a
verified recursive submodule snapshot after exact checkout and before building.
The preparation step emits the completed receipt's SHA256; every collector
requires that digest before parsing the receipt.

Artifact collection verifies the receipt against the requested inputs, local
HEAD, parents, tree, and validator files, and validates the checkout-time
submodule snapshot's schema and binding.
It retains a sanitized checkout projection in the public summaries. The hosted
checks add the same projection to `bazel-repository-inputs`; VS compares its
immutable identity before consuming those downloads. Remote observations prove
the refs at checkout preparation time. Receipt collection does not claim that
the refs are still current after a long build or that every working file or
submodule HEAD remained unchanged. Legacy Make can change nested checkouts while
preparing packages; the existing declared-input checks and later babysitter
readback retain those roles.
If hosted checks fail after checkout, they may retain a receipt-only artifact;
incomplete source downloads are not passed to VS. Exact-mode direct installed
evidence and image uploads also include a verified checkout projection. That
projection identifies the checkout; the existing build validation and upload
artifact digest retain their separate semantic-validation and archive-integrity
roles. An installed-evidence artifact can contain only the checkout projection
after a failure; its presence does not establish installed validation. Consumers
must also check the job result and expected installed-evidence files.

These manual runs are supplementary validation. GitHub does not evaluate
`workflow_dispatch` job checks as PR or required branch-ruleset checks; see
[GitHub's required-check guidance](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks#checks-from-some-workflow-jobs-are-not-evaluated).
The run and artifact API `head_sha` identify the validator revision, while the
receipt identifies the tested source pair. The current image dashboard uses run
metadata for PR association, so a companion-branch run can appear under the
companion PR and a master run can remain unassociated. Receipt-aware dashboard
association is separate work on top of [PR41](https://github.com/securely1g/sonic-buildimage/pull/41).
Use a validator landed on the maintained branch for Ready consumers; an unmerged
validator is an unlanded external CI input under the existing readiness policy.

## Helper responsibilities

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
