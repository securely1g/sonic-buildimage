# Shared Bazel CI helpers

These helpers validate buildimage artifacts and preserve build evidence without
importing container-specific code.

[`.github/workflows/bazel-oci.yml`](../../../.github/workflows/bazel-oci.yml) is
the shared entrypoint for Bazel OCI container CI. It runs common AMD64/ARM64
archive checks, SWSS source checks, Syncd contract checks and the existing full
VS integration job. Add checks for future OCI containers as jobs in this
workflow, keeping their target lists and package contracts with each container.
Manual runs default to `skip_vs=true`; same-repository PRs retain the full VS
job after the SWSS source checks pass.

- `artifact_validation.py` streams tar contents to record ownership, modes, file
  hashes and little-endian ELF64 metadata, and checks matching split debug symbols.
  SWSS and syncd share its ELF inspection and debug-link checks; each consumer
  supplies its package inventory and platform policy. SWSS's inventory and
  protobuf expectations stay in `dockers/docker-orchagent/bazel/package_contract.py`.
  `debug_archives` combines a runtime tar and its symbols tar, rejects payload
  collisions and foreign debug paths, checks the caller's ELF architecture,
  safely extracts the pair and verifies matching build IDs, DWARF and debug links.
- `build.py` builds a caller's filename-to-label mapping and collects one nonempty
  archive per label, with atomic publication, command logs, sizes and hashes.
  `source_directory` resolves a target's owning source checkout for install-contract
  checks. Callers retain their target lists, platform options and package policy.
- `command_log.py` captures command output and timings while keeping diagnostic
  messages separate from queried artifact paths. Callers supply the working
  directory and evidence directory. Orchagent and syncd use the same runner;
  syncd sends raw action-graph stdout to a temporary private file, retaining
  only stderr in the log and removing the raw graph after its execution audit.
- `public_artifacts.py` prepares explicit public upload lists for the archive,
  SWSS source, VS and syncd contract jobs. Successful jobs retain their expected
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
