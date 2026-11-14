# Assemble an AMD64 VS image from existing outputs

The `vs-reuse` task in the **Bazel OCI** workflow assembles `target/sonic-vs.bin`
on the `sonic-slave-container-k3s` pool. It consumes original packages and
container archives. It does not generate DEBs, call Bazel, probe the remote
cache, or depend on persistent worker caches. Native package/container producers
continue to use their separately qualified remote Bazel cache.

This task requires a reviewed inventory of original files and successful
canonical P4RT, Syncd and Gearbox evidence for the current PR head, tested base
and tested merge. Missing inputs or mismatched provenance stop the task; it
does not fall back to package compilation. The current native admission
companion is [PR #201](https://github.com/securely1g/sonic-buildimage/pull/201),
at `1e423956afe2cb706c239c8be93c1000bbc7a479`.

## Dispatch inputs

Run the workflow from the same immutable source head supplied as
`source_revision`, with:

- `source_image`: `vs-reuse`
- `skip_vs`: `false`
- `source_revision`: the PR head used by the original native requests
- `source_merge`: their tested merge commit, whose second parent is that head
- `reuse_inventory_revision`: an immutable commit containing
  `tools/bazel/ci/vs_reuse_inputs.json`
- `reuse_inventory_sha256`: the reviewed SHA256 of that file

The inventory is frozen separately after the source commit and original
artifacts exist. This avoids embedding a future source commit in its own
contents. Its assembly commit/tree must match the real merge checkout; the
canonical PR head remains a separate field. Existing native requests cannot
be relabeled after the head, base, merge or controller changes.

Each of the 42 migrated container archives also has a `bazel_archive_builds`
entry, keyed by its `target/...gz` destination. The entry records its original
`artifact_id`, exact file `sha256`, and reviewed `build_step` name. The build
step must have completed successfully before that artifact's successful upload
step in the authenticated producer job. Review the pinned producer workflow to
confirm that the named step actually builds the archive with Bazel.

After checking the imported archive bytes, the metadata-only Make target calls
the existing container-method stamp rules for those selected archives. This
restores the `bazel` stamps omitted from image artifact uploads; it does not
rebuild archives or replace their source, native or content validation.

The explicit task is restricted to the operator account on the maintained
fork. It does not activate the legacy full-image route for migration PRs.

## What happens

1. Check out the maintained caller, tested merge and pinned dependency contexts.
   Verify their actual identities and recursively initialized submodules.
2. Admit original GitHub artifact metadata. Check free temporary storage for all
   extracted artifact members, selected destination copies and the existing
   90 GiB assembly reserve. Upstream streams ZIPs into extraction, so this
   budget does not include an extra full ZIP copy.
3. Use the pinned upstream `actions/download-artifact` implementation for
   transport, digest verification and extraction, grouped by original producer
   run. The maintained materializer checks individual file hashes and preserves
   original provenance before copying into absent destinations.
4. Prepare the source with existing build hooks and reused packages. Re-capture
   the configured Make inputs after materialization; fresh-checkout empty
   globs are not accepted as a complete dependency closure.
5. Verify the original kernel packages through the existing kernel verifier,
   then run Make for the image in the owned private Docker scope.
6. Run the post-build archive, installed version and installed image checks as
   separate measured validation stages, then close the owned Docker scope.
   Successful jobs upload the image and structured receipts.

The supervisor records setup, assembly and validation timings separately.
Complete Make logs remain private; failure receipts expose bounded stage names,
source locations and log size/hash facts.

The job has a three-hour limit. Its actual GitHub start time determines an
aggregate download deadline, a two-hour supervisor body, the existing owned
cleanup window and a publishing reserve. Admission refuses to start assembly
without that full window. A second `vs-reuse` dispatch queues instead of
automatically cancelling an active assembly.

Passing metadata or fixture checks does not establish full-image success.
Acceptance requires the actual assembled image, its post-build checks and
successful owned cleanup.
