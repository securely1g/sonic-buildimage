# P4RT worker evidence for normal PR checks

P4RT's SDK and image validation can exceed the disposable hosted runner's disk
capacity. For an already-selected P4RT job on a same-repository pull request,
`p4rt_native` and `p4rt_runtime` now verify small public receipts from the existing
operator-controlled source-image worker. They retain their normal check names and
dependencies. A missing or invalid receipt fails the native check; it does not
skip validation or dispatch a worker automatically.

The first normal run records an immutable request containing its run ID, original
attempt, PR head R, base B, actual checkout merge M and tree, selected owner,
registry, trusted controller and contract digest. Git's raw commit object proves
M has exactly B and R as parents even with a shallow checkout. Evidence for R
alone cannot satisfy this request. Later attempts reuse the original request;
expired, duplicate or missing requests require a new normal workflow run.

An operator reviews that request and its source before dispatching `source_image:
p4rt` on the contract's controller, with `source_revision: M`, the selected
registry and `source_ci_request` set to the request artifact ID. Admission still
requires an idle allowed worker, the existing resource and isolation checks, and
a fresh capacity assessment of the shared filesystem. The existing 50 GiB
preflight minimum alone is not a sufficient P4RT space budget. Preserve worker
caches and account for concurrent jobs, intermediate images and receipt exports.
There is no automatic assignment to a self-hosted worker from a PR event.

The controller verifies the request's API provenance and current PR R/B, then
runs the existing seven native phases, all 59 original owner tests, ten SDK roots
and 31 image audit roots. After private-daemon cleanup it packages only allowlisted
JSON, at most 2 MiB per file and 16 MiB in total. The consumer also requires the
latest worker attempt, all required phases, checkout cleanup and the entire
worker run to finish successfully. Duplicate eligible artifacts fail closed.
It reuses the full SDK, source TAR, test, image, debug, incremental restoration and
loaded runtime validators; payload hashes remain CI-reported rather than
independently re-downloaded.

After worker success, use GitHub's ordinary **Re-run failed jobs** on the original
normal run. GitHub also reruns its dependent jobs, so `p4rt_runtime` and the existing
aggregate follow their unchanged `needs`/`if` conditions. They must each finish
normally; no check status is synthesized through an API.

Fork pull requests and non-PR events retain their existing hosted native path.
This bridge does not solve hosted capacity for those events. Tokens are used only
for read-only artifact and run metadata; checkout does not persist credentials.
Artifact transport uses the pinned official download action, not a custom archive
parser. Private environment, paths and logs remain outside the public envelope.

References: [rerun failed jobs and dependent jobs](https://docs.github.com/en/rest/actions/workflow-runs#re-run-failed-jobs-from-a-workflow-run),
[artifact name and provenance API](https://docs.github.com/en/rest/actions/artifacts#list-artifacts-for-a-repository).
